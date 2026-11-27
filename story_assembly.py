"""Explain real per-shot matches and keep dashboard generation independent of Tk."""
import json
import hashlib
from pathlib import Path
import threading

from aecpp_preview import render_preview

LABELS={'idle':'站立','talk':'说话','walk':'走路','run':'奔跑','jump':'跳跃','fall':'摔倒','shake':'抖动',
        'neutral':'平静','happy':'开心','angry':'生气','sad':'伤心','shocked':'震惊',
        'wide':'全景','medium':'中景','close':'近景','unknown':'未知'}
METHODS={'exact':'直接对应','synonym':'同义匹配','similar':'相近替代','fallback':'回退素材',
         'manual':'已有绑定','context':'场景匹配','identity':'人物名称匹配','capability':'动作能力匹配','missing':'缺少素材'}


def _read(path,maximum=8*1024*1024):
    if path.stat().st_size>maximum:raise ValueError('匹配记录过大，请按章节生成')
    return json.loads(path.read_text(encoding='utf-8'))


def explain_assembly(board,matching,output):
    """Use final bindings, provenance and post-speech timing; never rerun a match."""
    characters={c['id']:c['name'] for c in board['characters']}
    shots={s['id']:s for s in board['shots']}
    decisions=matching.get('matches',[])
    timing={t['shot_id']:t for t in matching.get('sound',{}).get('timing',[])}
    rows=[]
    for selection in matching['shot_matches']:
        shot=shots[selection['shot_id']];name=characters.get(selection['character_id'],'旁白')
        def decision(kind,requested,asset_id):
            item=next((d for d in decisions if d['kind']==kind and d['asset_id']==asset_id and
                d['requested']==requested and d['subject']==(shot['scene'] if kind=='background' else name)),{})
            return {'asset_id':asset_id,'name':item.get('asset_name') or asset_id or '未使用',
                    'method':item.get('method','missing'),'reason':item.get('reason',''),
                    'substituted':item.get('substituted',False),'requested':requested}
        rows.append({'id':shot['id'],'text':shot['text'],'character':name,'scene':shot['scene'],
            'actor':decision('character',name,selection['actor_asset_id']),
            'background':decision('background',shot['scene'],selection['background_asset_id']),
            'action':decision('action',shot['action'],selection['action']['asset_id']),
            'expression':decision('expression',shot['emotion'],selection['expression']['asset_id']),
            'camera':shot['camera'],'scale':{'wide':.8,'medium':1.,'close':1.3}[shot['camera']],
            'start':selection['start'],'duration':selection['duration'],
            'reference':selection.get('visual_reference') or {},'sound_timing':timing.get(shot['id'],{}),
            'direction':shot.get('direction','')})
    return {'output':str(Path(output).resolve()),'title':board['title'],'input':board.get('source_text',''),
            'rows':rows,'sound':matching.get('sound',{}),'warnings':matching.get('warnings',[]),
            'resource_root':matching['resource_root'],'reference_summary':matching.get('visual_reference_summary',{}),
            'preview':None,'preview_error':''}


def load_assembly(folder):
    folder=Path(folder).resolve();project=folder/'story.aecpp'
    if not project.is_file():return None
    value=explain_assembly(_read(folder/'storyboard.json'),_read(folder/'matching-report.json'),project)
    path=folder/'assembly-preview.json'
    if path.is_file():
        try:
            manifest=_read(path,128*1024)
            directory=(folder/manifest['folder']).resolve()
            if not directory.is_relative_to(folder):raise ValueError('预览目录越界')
            if manifest.get('project_sha256')!=hashlib.sha256(project.read_bytes()).hexdigest():raise ValueError('工程已变化，请重试预览')
            if not 0<float(manifest['duration'])<=30 or not 1<=float(manifest['fps'])<=8 or not 1<=len(manifest['frames'])<=240:raise ValueError('预览范围无效')
            for filename in [manifest['video'],*manifest['frames']]:
                image=(directory/filename).resolve()
                if not image.is_relative_to(directory) or not image.is_file():raise ValueError('预览文件缺失或越界，请重试预览')
            value['preview']={**manifest,'directory':str(directory)}
        except (OSError,ValueError,KeyError,TypeError) as exc:value['preview_error']=str(exc)
    return value


def latest_assembly(root):
    base=(Path(root)/'creations/animations').resolve()
    if not base.is_relative_to(Path(root).resolve()):return None
    paths=sorted(base.glob('*/matching-report.json'),key=lambda p:p.stat().st_mtime,reverse=True)
    for path in paths[:30]:
        try:
            if not path.resolve().is_relative_to(base):continue
            value=load_assembly(path.parent)
            if value:return value
        except (OSError,ValueError,KeyError,TypeError):continue
    return None


def make_preview(value,*,stop,report):
    preview=render_preview(value['output'],value['resource_root'],stop=stop,report=report)
    folder=Path(value['output']).parent
    manifest={k:v for k,v in preview.items() if k!='directory'}
    manifest['folder']=Path(preview['directory']).relative_to(folder).as_posix()
    temporary=folder/'assembly-preview.tmp'
    temporary.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    temporary.replace(folder/'assembly-preview.json')
    return {**value,'preview':preview,'preview_error':''}


class AssemblyTask:
    def __init__(self,root):
        self.root=Path(root).resolve();self._lock=threading.Lock();self._thread=None;self._stop=threading.Event()
        self._state={'busy':False,'message':'粘贴小说或剧本，即可查看学习参考如何用于现有素材组装。',
                     'result':None,'error':'','input':'','revision':0}

    @property
    def busy(self):return self.snapshot()['busy']

    def snapshot(self):
        with self._lock:return dict(self._state)

    def _update(self,**changes):
        with self._lock:self._state.update(changes);self._state['revision']+=1

    def _launch(self,work):
        with self._lock:
            if self._state['busy']:return False
            self._state.update(busy=True,error='');self._stop=threading.Event()
        def worker():
            try:work()
            except Exception as exc:self._update(error=str(exc),message=str(exc))
            finally:self._update(busy=False)
        self._thread=threading.Thread(target=worker,daemon=True,name='dashboard-assembly');self._thread.start()
        return True

    def restore(self):
        def work():
            value=latest_assembly(self.root)
            if value:self._update(result=value,input=value['input'],message='已恢复最近生成的工程与真实素材匹配记录'+
                ('；'+value['preview_error'] if value['preview_error'] else ''))
        return self._launch(work)

    def start(self,text,title='仪表盘动画',*,resources=None,sound=True):
        if not text.strip():return False
        def work():
            self._update(input=text,message='正在解析小说并匹配学习参考…')
            from story_automation import generate_animation
            generated=generate_animation(self.root,text,title,resource_root=resources,sound=sound,stop=self._stop,
                                         report=lambda m:self._update(message=m))
            value=load_assembly(generated['run_dir'])
            self._update(result=value,message='可编辑工程已生成，正在绘制组合预览…')
            try:value=make_preview(value,stop=self._stop,report=lambda m:self._update(message=m))
            except Exception as exc:value={**value,'preview_error':str(exc)}
            self._update(result=value,message='有声可编辑工程与组合预览已生成' if value['preview'] and sound else
                         '可编辑工程已生成'+('；预览未完成：'+value['preview_error'] if value['preview_error'] else ''))
        return self._launch(work)

    def retry_preview(self):
        value=self.snapshot()['result']
        if not value:return False
        def work():
            result=make_preview(value,stop=self._stop,report=lambda m:self._update(message=m))
            self._update(result=result,message='组合预览已更新；可编辑工程保留')
        return self._launch(work)

    def cancel(self):self._stop.set();self._update(message='正在取消当前生成或预览；已发布工程保留')
