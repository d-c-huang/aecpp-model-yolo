"""Durable observed totals and read-only dashboard data, without Tk or workers."""
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import statistics
import time

from learning_totals import summarize_learning

METRICS=('video_count','completed_count','partial_count','subtitle_reference_count',
         'visual_clip_count','visual_frame_count','skipped_count')


def read_project(root):
    root=Path(root).resolve()
    def rows(name,table,raw=False):
        path=root/name
        if not path.is_file():return []
        db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=.15)
        try:
            db.row_factory=sqlite3.Row
            return [dict(r) if raw else json.loads(r['data']) for r in db.execute('SELECT '+('*' if raw else 'data')+' FROM '+table)]
        finally:db.close()
    return {'downloads':rows('online_jobs.sqlite','online_jobs'),'learning':rows('auto_learning.sqlite','jobs'),
            'candidates':rows('discovery.sqlite','candidates'),'movies':rows('annotations.sqlite','movies',True)}


def project_totals(state):
    return summarize_learning(state['downloads'],state['learning'],state['candidates'],movies=state['movies'])


def record_point(root,totals,*,now=None):
    if totals.get('ready') is not True:return False
    payload={key:totals.get(key,0) for key in METRICS}
    if any(type(v) is not int or v<0 for v in payload.values()):raise ValueError('成果统计数值无效')
    now=time.time() if now is None else now
    if not isinstance(now,(int,float)) or not math.isfinite(now) or now<0:raise ValueError('统计时间无效')
    root=Path(root).resolve();root.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(root/'learning_metrics.sqlite',timeout=.2)
    try:
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS points(id INTEGER PRIMARY KEY, observed REAL NOT NULL,data TEXT NOT NULL)')
            db.execute('BEGIN IMMEDIATE')
            last=db.execute('SELECT observed,data FROM points ORDER BY id DESC LIMIT 1').fetchone()
            if last and json.loads(last[1])==payload and now-last[0]<60:return False
            if last:now=max(now,last[0])
            db.execute('INSERT INTO points(observed,data) VALUES(?,?)',(now,json.dumps(payload)))
            db.execute('DELETE FROM points WHERE id <= (SELECT MAX(id)-10000 FROM points)')
        return True
    finally:db.close()


def read_history(root,limit=240):
    path=Path(root).resolve()/'learning_metrics.sqlite'
    if not path.is_file():return []
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=.15)
    try:
        rows=list(reversed(db.execute('SELECT observed,data FROM points ORDER BY id DESC LIMIT 10000').fetchall()))
        limit=max(2,min(10000,int(limit)))
        if len(rows)>limit:
            rows=[rows[round(i*(len(rows)-1)/(limit-1))] for i in range(limit)]
        return [{'time':r[0],**json.loads(r[1])} for r in rows]
    finally:db.close()


def record_project_point(root):
    return record_point(root,project_totals(read_project(root)))


def visual_event(root,event,rhythm=None):
    """Publish only bounded local frame evidence, never URLs or image blobs."""
    root=Path(root).resolve()
    frames=event.get('frames')
    if not isinstance(frames,list) or not 2<=len(frames)<=12:return None
    result={key:event[key] for key in ('clip_index','clip_count','model','start_ms','end_ms','cache_hit','performance') if key in event}
    result['frames']=[]
    for frame in frames:
        path=Path(frame['path']).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size>1024*1024:
            raise ValueError('实时证据帧缺失、越界或过大')
        result['frames'].append({'path':path.relative_to(root).as_posix(),'time_ms':frame['time_ms'],
                                 'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    if isinstance(event.get('observation'),dict):
        result['observation']={key:event['observation'][key] for key in ('text','evidence','action','emotion','camera','confidence')
                               if key in event['observation']}
    if rhythm:
        cuts=rhythm.get('cut_times_ms',[])
        boundaries=[0,*cuts,rhythm['duration_ms']]
        intervals=[(b-a)/1000 for a,b in zip(boundaries,boundaries[1:]) if .5<=(b-a)/1000<=60]
        result['rhythm']={'duration_ms':rhythm['duration_ms'],'method':rhythm['method'],'cut_count':len(cuts),
            'cut_times_ms':cuts[::max(1,math.ceil(len(cuts)/1000))],
            'median_seconds':statistics.median(intervals) if len(intervals)>=3 else None}
    else:
        cuts=event.get('cut_times_ms',[])
        result['rhythm']={'duration_ms':result.get('end_ms',0),'method':'luma-cut-candidates',
                          'cut_count':len(cuts),'cut_times_ms':cuts[:1000],'median_seconds':None}
    return result


def completed_visual(root,job,reference=None):
    live=job.get('live_visual')
    if live and live.get('frames') and not reference:return live
    path=(reference or {}).get('reference_path') or (job.get('model_analysis') or {}).get('visual_reference_path')
    if not path:return None
    from video_visual_learning import load_visual_reference
    note=load_visual_reference(root,path)
    if reference and reference.get('source_hash') and reference['source_hash']!=note['source_hash']:
        raise ValueError('该学习参考已更新，请重新生成以查看当前证据')
    if not note['clips']:return None
    clip=next((c for c in note['clips'] if c['id']==reference['clip_id']),None) if reference else note['clips'][0]
    if not clip:raise ValueError('学习片段已更新，请重新生成')
    frames=[{**f,'path':str(Path(root)/f['path'])} for f in clip['frames']]
    return visual_event(root,{'frames':frames,'start_ms':clip['start_ms'],'end_ms':clip['end_ms'],
        'clip_index':note['clips'].index(clip)+1,'clip_count':len(note['clips']),'model':note['model'],'observation':clip,
        'cut_times_ms':clip['cut_times_ms']},note.get('rhythm'))


def dashboard_snapshot(root,selected=None):
    reference=selected if isinstance(selected,dict) else None
    selected=reference.get('movie_id') if reference else selected
    state=read_project(root)
    totals=project_totals(state)
    warning=''
    try:record_point(root,totals)
    except (sqlite3.Error,OSError,ValueError) as exc:warning='增长记录暂未保存：'+str(exc)[:200]
    learning=sorted(state['learning'],key=lambda r:r.get('updated',''),reverse=True)
    running=next((r for r in learning if r['status']=='running'),None)
    chosen=next((r for r in learning if str(r['movie_id'])==str(selected)),None) if selected else running
    chosen=chosen or next((r for r in learning if r.get('live_visual') or (r.get('model_analysis') or {}).get('visual_reference_path')),None)
    chosen=chosen or (learning[0] if learning else None)
    movies={m['id']:m for m in state['movies']}
    def source(job):
        if not job:return ''
        online=next((r for r in state['downloads'] if (r.get('result') or {}).get('movie_id')==job['movie_id']),None)
        return online['url'] if online else str(movies.get(job['movie_id'],{}).get('source_path',''))
    download=next((r for r in state['downloads'] if r['status']=='running'),None)
    visual=None
    if chosen:
        try:visual=completed_visual(root,chosen,reference)
        except (OSError,ValueError,TypeError,KeyError) as exc:
            if reference:warning='学习证据暂不可用：'+str(exc)
    return {'totals':totals,'history':read_history(root),'warning':warning,'job':chosen,'learning':running,
        'download':download,'source':source(running or chosen),'visual':visual,
        'movies':[{'id':r['movie_id'],'title':r['title'],'status':r['status']} for r in learning[:200]]}
