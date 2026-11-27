"""Observed edit rhythm from a bounded local FFmpeg scene-difference pass."""
import math
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
import shutil


def analyse_rhythm(source, *, duration_ms, report=lambda phase, message: None, check=lambda: None,
                   timeout=600,cache_dir=None,source_hash=''):
    if type(duration_ms) is not int or not 0 < duration_ms <= 12*3600*1000:
        raise ValueError("镜头节奏的视频时长无效")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or not .1 <= timeout <= 600:
        raise ValueError("镜头节奏分析超时无效")
    check()
    source=Path(source);source_stat=source.stat();cache=None
    if cache_dir:
        from video_analysis_cache import AnalysisCache,signature,file_hash
        cache=AnalysisCache(cache_dir);executable=Path(shutil.which('ffmpeg') or 'ffmpeg')
        tool=executable.stat() if executable.is_file() else None
        key=signature({'kind':'ffmpeg-scene-difference-v1','duration_ms':duration_ms,'fps':4,'threshold':.28,
            'source':source_hash or file_hash(source,lambda:check() or False),
            'decoder':(str(executable.resolve()),tool.st_size,tool.st_mtime_ns) if tool else None})
        value=cache.get(key)
        if (isinstance(value,dict) and value.get('method')=='ffmpeg-scene-difference' and value.get('duration_ms')==duration_ms
                and value.get('sample_fps')==4 and value.get('threshold')==.28 and isinstance(value.get('cut_times_ms'),list)):
            cuts=value['cut_times_ms']
            if (len(cuts)<=50000 and all(type(t) is int and 0<t<duration_ms for t in cuts)
                    and cuts==sorted(set(cuts)) and all(b-a>=500 for a,b in zip(cuts,cuts[1:]))):
                check();report('shot-rhythm-cache',f'已复用校验缓存：全片 {len(cuts)} 个切镜候选')
                return {**value,'cache_hit':True}
    report("shot-rhythm", "正在分析全片真实切镜候选 · 每秒 4 帧 / 场景差异阈值 0.28")
    process = subprocess.Popen(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info",
        "-threads", "1", "-i", str(source), "-an", "-sn", "-dn",
        "-vf", "fps=4,scale=160:-2,select='gt(scene,0.28)',showinfo", "-f", "null", "-"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    lines = queue.Queue(maxsize=256)
    done = threading.Event()
    def read():
        try:
            with process.stderr:
                for line in process.stderr:
                    # Drain FFmpeg continuously without retaining unbounded logs.
                    while not done.is_set():
                        try:
                            lines.put(line, timeout=.1)
                            break
                        except queue.Full:
                            continue
        finally:
            done.set()
    reader = threading.Thread(target=read, daemon=True, name="video-rhythm-progress")
    reader.start()
    deadline, cuts, last_report, tail = time.monotonic()+timeout, [], time.monotonic(), []
    try:
        while not done.is_set() or not lines.empty():
            check()
            if time.monotonic() >= deadline:
                raise TimeoutError("全片切镜分析超时；可保留其他已完成的学习成果")
            try:
                line = lines.get(timeout=.1)
            except queue.Empty:
                continue
            tail = (tail+[line.strip()])[-3:]
            match = re.search(r"\bpts_time:([0-9.]+)", line)
            if match:
                stamp = round(float(match[1])*1000)
                if 0 < stamp < duration_ms and (not cuts or stamp-cuts[-1] >= 500):
                    cuts.append(stamp)
                    if len(cuts) > 50000:
                        raise ValueError("候选切镜数量过多，请拆分长片")
            if time.monotonic()-last_report >= 2:
                report("shot-rhythm", f"全片切镜分析中 · 已检测 {len(cuts)} 个候选切镜")
                last_report = time.monotonic()
        process.wait(timeout=3)
        check()
        if process.returncode:
            raise ValueError("全片切镜分析未完成："+" ".join(tail)[-400:])
        report("shot-rhythm", f"全片节奏分析完成 · {len(cuts)} 个候选切镜；保留检测方法与原片时间")
        if (source_stat.st_size,source_stat.st_mtime_ns)!=(source.stat().st_size,source.stat().st_mtime_ns):raise ValueError('切镜分析期间原片发生变化')
        value={"method":"ffmpeg-scene-difference", "duration_ms":duration_ms,"cut_times_ms":cuts, "sample_fps":4,"threshold":.28}
        if cache:
            try:cache.put(key,value)
            except (OSError,ValueError):pass
        return value
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        done.set()
        reader.join(timeout=2)
