"""Offline speech, energy-driven editable mouth keys and short comic effects."""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import time
import wave

import numpy as np

RATE=16000


def _read(path):
    with wave.open(str(path),'rb') as w:
        if w.getnchannels()!=1 or w.getsampwidth()!=2 or w.getframerate()!=RATE:
            raise ValueError('配音须为 16 kHz 单声道 PCM 音频')
        samples=np.frombuffer(w.readframes(w.getnframes()),dtype='<i2').astype(np.float32)/32768
    if not len(samples) or not np.isfinite(samples).all():
        raise ValueError('配音没有有效声音')
    return samples


def _write(path,samples):
    with wave.open(str(path),'wb') as w:
        w.setparams((1,2,RATE,0,'NONE','not compressed'))
        w.writeframes((np.clip(samples,-.98,.98)*32767).astype('<i2').tobytes())


def synthesize(lines,folder,report,check):
    if os.name!='nt':
        raise ValueError('当前离线配音使用 Windows 中文系统语音；可在小说成片取消自动配音后生成无声工程')
    shell=Path(os.environ.get('WINDIR',r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
    request,response=folder/'request.json',folder/'response.json'
    request.write_text(json.dumps({'lines':lines},ensure_ascii=False),encoding='utf-8')
    stderr=folder/'speech-error.log'
    check()
    with stderr.open('w',encoding='utf-8') as errors:
        process=subprocess.Popen([str(shell),'-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
            '-File',str(Path(__file__).with_name('speech_sapi.ps1')),'-RequestPath',str(request),'-ResponsePath',str(response)],
            stdout=subprocess.DEVNULL,stderr=errors,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),
            env={**os.environ,'PYTHONUTF8':'1'})
        deadline=time.monotonic()+min(1800,max(60,sum(len(l['text']) for l in lines)*.2))
        completed=-1
        try:
            while process.poll() is None:
                check()
                if time.monotonic()>deadline:raise TimeoutError('本地配音超时，请按章节生成')
                try:
                    progress=json.loads(response.read_text(encoding='utf-8'))
                    if progress['completed']!=completed:
                        completed=progress['completed']
                        report(f'本地配音 · {completed}/{len(lines)} 句 · Windows 中文系统语音')
                except (OSError,ValueError,KeyError):pass
                time.sleep(.1)
            check()
            if process.returncode:
                detail=stderr.read_text(encoding='utf-8',errors='replace')[-1600:]
                raise ValueError('离线配音失败，请检查 Windows 中文语音包：'+detail)
            result=json.loads(response.read_text(encoding='utf-8'))
            if result.get('completed')!=len(lines):raise ValueError('配音未完整生成，未发布有声工程')
            return result['lines']
        finally:
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)


def mouth_keys(samples):
    """Amplitude envelope, not phoneme recognition; silence always closes."""
    step=round(RATE*.04)
    rms=np.array([np.sqrt(np.mean(samples[i:i+step]**2)) for i in range(0,len(samples),step)])
    reference=max(.015,float(np.percentile(rms,85)))
    values=np.where(rms<max(.006,reference*.1),0,np.minimum(.95,rms/reference*.8))
    return [{'time':round(i*.04,3),'openness':round(float(value),4),'shape':'natural'} for i,value in enumerate(values)]


def _effect(kind):
    duration={'jump':.4,'fall':.32,'shocked':.42,'happy':.34}[kind]
    t=np.arange(round(duration*RATE))/RATE
    if kind=='fall':
        rng=np.random.default_rng(1)
        values=(.3*np.sin(2*np.pi*90*t)+.2*rng.uniform(-1,1,len(t)))*np.exp(-18*t)
    elif kind=='jump':values=.45*np.sin(2*np.pi*(180*t+700*t*t))*np.exp(-7*t)
    else:
        frequency=900 if kind=='happy' else 440
        values=.35*(np.sin(2*np.pi*frequency*t)+.4*np.sin(2*np.pi*frequency*1.5*t))*np.exp(-10*t)
    return (values*np.minimum(1,t/.01)).astype(np.float32)


def _track(folder,name,segments,start,end):
    path=folder/(name+'.wav')
    # Stream silent gaps instead of allocating one movie-length buffer per role.
    with wave.open(str(path),'wb') as w:
        w.setparams((1,2,RATE,0,'NONE','not compressed'))
        cursor=0
        for at,samples in sorted(segments,key=lambda row:row[0]):
            target=round((at-start)*RATE)
            while cursor<target:
                count=min(RATE,target-cursor);w.writeframesraw(b'\0\0'*count);cursor+=count
            w.writeframesraw((np.clip(samples,-.98,.98)*32767).astype('<i2').tobytes());cursor+=len(samples)
    peaks=[]
    with wave.open(str(path),'rb') as w:
        step=max(1,math.ceil(w.getnframes()/1600))
        while raw:=w.readframes(step):peaks.append(round(float(np.max(np.abs(np.frombuffer(raw,dtype='<i2').astype(np.float32))))/32768,4))
    return {'id':name,'path':str(path),'offset':round(start,5),'duration':round(cursor/RATE,5),
            'sourceOffset':0,'sourceDuration':round(cursor/RATE,5),'volume':1,'muted':False,'speed':1,'waveform':peaks}


def prepare_story_audio(board,folder,*,effects=True,report=lambda s:None,check=lambda:None,synthesizer=None):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    board=copy.deepcopy(board)
    profiles={}
    lines=[]
    for shot in board['shots']:
        role=shot.get('speaker') or 'narrator'
        profiles.setdefault(role,len(profiles))
        index=profiles[role]
        lines.append({'id':shot['id'],'text':shot['text'],'profile':index,'rate':(index%3)-1,
                      'path':str(folder/(shot['id']+'.wav'))})
    report(f'正在为 {len(profiles)} 个角色/旁白生成本地配音 · {len(lines)} 句')
    generated=(synthesizer or synthesize)(lines,folder,report,check)
    by_id={line['id']:line for line in generated}
    voices,events,timeline,start=[],[],[],0.
    for shot,line in zip(board['shots'],lines):
        check()
        actual=by_id.get(shot['id'])
        if not actual:raise ValueError('缺少台词配音，未发布有声工程')
        samples=_read(line['path'])
        if float(np.max(np.abs(samples)))<.002:raise ValueError('配音文件只有静音，未发布有声工程')
        seconds=len(samples)/RATE
        original=shot['duration']
        shot['duration']=round(max(original,seconds+.3),3)
        if shot['duration']>60:raise ValueError('单句配音超过 60 秒，请拆分较长对白')
        role=shot.get('speaker') or 'narrator'
        at=start+.1
        voices.append({'shot_id':shot['id'],'character_id':shot.get('speaker',''),'role':role,'path':line['path'],
            'start':round(at,5),'duration':seconds,'voice':actual['voice'],'rate':actual['rate'],
            'keys':mouth_keys(samples),'samples':samples})
        timeline.append({'shot_id':shot['id'],'before':original,'after':shot['duration'],'speech_seconds':seconds,
                         'voice':actual['voice'],'rate':actual['rate'],'start':round(at,5)})
        if effects:
            kind=shot['action'] if shot['action'] in {'jump','fall'} else shot['emotion'] if shot['emotion'] in {'happy','shocked'} else None
            if kind:events.append({'kind':kind,'start':round(start+.02,5),'shot_id':shot['id'],'samples':_effect(kind)})
        start+=shot['duration']
        if start>3600:raise ValueError('有声工程超过 1 小时，请按章节生成')
    tracks=[]
    grouped={}
    for voice in voices:
        # AECPP supports 32 scene audio tracks. Large casts share a dialogue
        # track while keeping their separate source clips and mouth channels.
        key=voice['role'] if len(profiles)<=30 else 'dialogue'
        grouped.setdefault(key,[]).append(voice)
    for i,(role,items) in enumerate(grouped.items(),1):
        first=min(v['start'] for v in items);last=max(v['start']+v['duration'] for v in items)
        track=_track(folder,f'voice_{i:02d}',[(v['start'],v['samples']) for v in items],first,last)
        track['character_id']=role if role not in {'narrator','dialogue'} else ''
        track['name']='旁白' if role=='narrator' else role
        tracks.append(track)
        for voice in items:
            voice['audio_track_id']=track['id'];voice['source_offset']=round(voice['start']-first,5)
            voice['keys']=[{**k,'time':round(k['time']+voice['source_offset'],5)} for k in voice['keys']]
            voice['keys'].insert(0,{'time':voice['source_offset'],'openness':0,'shape':'natural'})
            voice['keys'].append({'time':round(voice['source_offset']+voice['duration'],5),'openness':0,'shape':'natural'})
            # Keep one canonical key for each source time.
            voice['keys']=list({k['time']:k for k in voice['keys']}.values())
    if events:
        # Effects do not overlap within a shot; the timeline remains editable.
        first=events[0]['start'];last=max(e['start']+len(e['samples'])/RATE for e in events)
        track=_track(folder,'effects',[(e['start'],e['samples']) for e in events],first,last)
        track.update(character_id='',name='自动喜剧音效',volume=.32)
        tracks.append(track)
    for item in [*voices,*events]:item.pop('samples',None)
    for item in [*voices,*tracks]:
        item['sha256']=hashlib.sha256(Path(item['path']).read_bytes()).hexdigest()
    summary={'provider':'windows-sapi','voiced_shots':len(voices),
        'mouth_clips':len({(v['character_id'],v['audio_track_id']) for v in voices if v['character_id']}),
        'mouth_segments':sum(bool(v['character_id']) for v in voices),
        'effect_events':len(events),'audio_tracks':len(tracks),'mouth_method':'audio-energy-envelope',
        'timing':timeline,'effects':events,'duration_seconds':round(start,3)}
    report(f'声音准备完成 · {len(voices)} 句配音 / {summary["mouth_clips"]} 个角色口型轨 / {len(events)} 处音效')
    return board,{'tracks':tracks,'voices':voices,'summary':summary}
