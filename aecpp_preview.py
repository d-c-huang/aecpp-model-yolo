"""Bounded previews rendered by the installed AECPP engine, with native audio."""
import json
import hashlib
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import uuid


def check_stop(stop):
    if stop and stop.is_set():raise ValueError('预览已取消；已生成的工程保留')


def engine_path(resources):
    base=Path(resources).resolve()
    candidates=[os.environ.get('AECPP_ENGINE'),base.parent/'out/build/mingw-release/aecpp.exe',
                Path(__file__).resolve().parent.parent/'AECPP/out/build/mingw-release/aecpp.exe']
    for candidate in candidates:
        if candidate and Path(candidate).is_file():return Path(candidate).resolve()
    raise ValueError('未找到 AECPP 原生引擎，请安装或编译 AECPP 后重试预览；可编辑工程已保留')


class NativeEngine:
    def __init__(self,resources,session,*,stop=None,report=lambda message:None,executable=None):
        self.stop,self.report=stop,report;self.messages=queue.Queue();self.serial=0
        self.log=(Path(session)/'engine.log').open('w',encoding='utf-8')
        try:
            self.process=subprocess.Popen([str(executable or engine_path(resources)),'--engine','--asset-root',str(resources),
                '--session-root',str(session)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.log,
                text=True,encoding='utf-8',errors='replace',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        except BaseException:self.log.close();raise
        def read():
            for line in self.process.stdout:
                try:self.messages.put(json.loads(line))
                except ValueError:pass
            self.messages.put({'eof':True})
        self.reader=threading.Thread(target=read,daemon=True,name='aecpp-preview-engine');self.reader.start()

    def rpc(self,method,params=None,timeout=180):
        check_stop(self.stop);self.serial+=1
        self.process.stdin.write(json.dumps({'id':self.serial,'method':method,'params':params or {}},ensure_ascii=False)+'\n')
        self.process.stdin.flush();deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            check_stop(self.stop)
            try:row=self.messages.get(timeout=.1)
            except queue.Empty:continue
            if row.get('eof'):raise ValueError('AECPP 预览进程已退出，请检查预览目录 engine.log')
            if row.get('event')=='exportProgress':
                self.report(f"原生组合画面：{row.get('completed',0)}/{row.get('total',0)} 帧")
            if row.get('id')!=self.serial:continue
            if row.get('error'):raise ValueError(method+'：'+str(row['error']))
            return row['result']
        raise ValueError('AECPP 预览超时；可编辑工程已保留')

    def close(self):
        try:self.process.stdin.close()
        except OSError:pass
        try:self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=3)
        self.reader.join(2);self.process.stdout.close();self.log.close()


def _run(command,stop):
    with tempfile.TemporaryFile() as error:
        process=subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=error,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            deadline=time.monotonic()+180
            while process.poll() is None:
                check_stop(stop)
                if time.monotonic()>deadline:raise ValueError('合成预览超时')
                time.sleep(.05)
            if process.returncode:
                error.seek(0);raise ValueError('预览合成失败：'+error.read(1800).decode('utf-8','replace'))
        finally:
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=3)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)


def audio_filters(tracks):
    filters=[]
    for i,t in enumerate(tracks,1):
        speed=float(t.get('speed',1));tempo=[]
        if not math.isfinite(speed) or speed<=0:raise ValueError('原生音轨速度无效')
        remaining=speed
        while remaining>2:tempo.append('atempo=2');remaining/=2
        while remaining<.5:tempo.append('atempo=0.5');remaining*=2
        tempo.append(f'atempo={remaining}')
        filters.append(f'[{i}:a]atrim=start={float(t.get("sourceOffset",0))}:duration={float(t["duration"])*speed},'
            f'asetpts=PTS-STARTPTS,'+','.join(tempo)+f',volume={float(t.get("volume",1))},adelay={round(float(t.get("offset",0))*1000)}:all=1[a{i}]')
    if tracks:
        filters.append(''.join(f'[a{i}]' for i in range(1,len(tracks)+1))+f'amix=inputs={len(tracks)}:normalize=0:dropout_transition=0,alimiter=limit=0.95:latency=1,apad[mix]')
    return ';'.join(filters)


def render_preview(project,resources,*,stop=None,report=lambda message:None,max_seconds=30):
    project=Path(project).resolve()
    if not project.is_file():raise ValueError('可编辑工程已移动或删除，请重新生成')
    project_hash=hashlib.sha256(project.read_bytes()).hexdigest()
    if not 0<max_seconds<=30:raise ValueError('组合预览最多 30 秒')
    if not shutil.which('ffmpeg'):raise ValueError('请安装 FFmpeg 后重试预览；可编辑工程已保留')
    destination=project.parent/('preview-'+uuid.uuid4().hex[:8])
    with tempfile.TemporaryDirectory(prefix='aecpp-preview-') as temporary:
        session=Path(temporary);engine=NativeEngine(resources,session,stop=stop,report=report)
        try:
            state=engine.rpc('openProject',{'path':str(project)})
            full=float(state['duration']);duration=min(full,max_seconds)
            frames=engine.rpc('renderSequence',{'directory':str(session/'frames'),'start':0,'duration':duration,
                'fps':8,'width':640,'height':360,'format':'png'})
        finally:engine.close()
        check_stop(stop);report('正在合成带声音的组合预览…')
        tracks=[t for t in frames.get('audioTracks',[]) if not t.get('muted',False)]
        command=['ffmpeg','-v','error','-framerate','8','-i',frames['pattern']]
        for track in tracks:command+=['-i',track['path']]
        if tracks:command+=['-filter_complex',audio_filters(tracks),'-map','0:v','-map','[mix]','-c:a','aac']
        command+=['-t',str(duration),'-c:v','libx264','-pix_fmt','yuv420p','-movflags','+faststart',str(session/'preview.mp4')]
        _run(command,stop);check_stop(stop)
        if hashlib.sha256(project.read_bytes()).hexdigest()!=project_hash:raise ValueError('工程已变化，请重试预览')
        manifest={'version':1,'project_sha256':project_hash,'duration':duration,'full_duration':full,'fps':8,'frame_count':frames['frameCount'],
            'width':640,'height':360,'audio_tracks':len(tracks),'video':'preview.mp4',
            'frames':[f'frames/frame-{i:06d}.png' for i in range(frames['frameCount'])]}
        (session/'preview.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        try:
            shutil.copytree(session,destination)
            check_stop(stop)
        except BaseException:
            if destination.is_dir():shutil.rmtree(destination)
            raise
    manifest['directory']=str(destination)
    return manifest
