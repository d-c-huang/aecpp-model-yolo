"""Embedded, offline Whisper transcription with explicit model acquisition.

Uses public faster-whisper APIs, independently implemented for this application.
Normal transcription requires a local model directory and never downloads models.
"""
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.request import Request, urlopen
import wave

from dataset import connect
from media import open_movie
from movie_sequence_data import _check_source
from online_jobs import _ProjectLock


MODEL_SIZES = ("tiny", "base", "small")
MODEL_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt", "vocabulary.json", "preprocessor_config.json")
RATE = 16000
CHUNK_SECONDS = 120
MAX_DURATION_SECONDS = 12 * 60 * 60
MAX_SEGMENTS = 100000
MAX_MODEL_FILE_BYTES = 1024 * 1024 * 1024
_INFERENCE_LOCK = threading.Lock()


class TranscriptionCancelled(RuntimeError):
    pass


def _check(stop):
    if stop is not None and (stop.is_set() if hasattr(stop, "is_set") else stop()):
        raise TranscriptionCancelled("本地语音转写已停止")


def _emit(report, stop, phase, message, **details):
    _check(stop)
    report({"phase": phase, "message": message, **details})


def dependencies_available():
    return all(importlib.util.find_spec(name) is not None for name in ("faster_whisper", "ctranslate2", "av"))


def validate_model(model_path):
    if not model_path or not Path(model_path).expanduser().is_dir():
        raise ValueError("请先下载或选择本地 Whisper 模型目录；转写不会自动下载模型")
    path = Path(model_path).expanduser().resolve()
    for name in ("config.json", "model.bin", "tokenizer.json"):
        if not (path/name).is_file() or not (path/name).stat().st_size:
            raise ValueError(f"本地模型缺少 {name}，请重新下载或选择完整的 CTranslate2 Whisper 模型")
    if not any((path/name).is_file() for name in ("vocabulary.txt", "vocabulary.json")):
        raise ValueError("本地模型缺少 vocabulary.txt / vocabulary.json")
    return path


def _digest_file(path, item, stop):
    size = path.stat().st_size
    if size != item["size"]:
        return False
    lfs = item.get("lfs") or {}
    expected = lfs.get("oid") or item.get("oid")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", expected):
        raise ValueError("模型来源未提供有效内容摘要")
    digest = hashlib.sha256() if lfs else hashlib.sha1(b"blob "+str(size).encode()+b"\0")
    with path.open("rb") as stream:
        while block := stream.read(1024*1024):
            _check(stop)
            digest.update(block)
    return digest.hexdigest() == expected


def download_model(size, cache_dir, *, report=lambda event: None, stop=None):
    """Explicitly fetch tiny/base/small into cache_dir/size; no user tokens used."""
    if size not in MODEL_SIZES:
        raise ValueError("模型大小请选择 tiny、base 或 small")
    root = Path(cache_dir).expanduser().resolve()
    folder = (root/size).resolve()
    if not folder.is_relative_to(root):
        raise ValueError("模型目录超出所选缓存目录")
    _check(stop)
    folder.mkdir(parents=True, exist_ok=True)
    lock = _ProjectLock(folder/".download.lock")
    if lock.fd is None:
        raise ValueError("此模型正在另一个任务中下载")
    repository = "Systran/faster-whisper-"+size
    metadata_url = "https://huggingface.co/api/models/"+repository+"/tree/main?recursive=false&expand=false"
    try:
        _emit(report,stop,"model_metadata","正在读取模型文件清单",url=metadata_url,model=size)
        with urlopen(Request(metadata_url,headers={"User-Agent":"YinggeLocalASR/1.0"}),timeout=15) as response:
            raw = response.read(1024*1024+1)
        if len(raw)>1024*1024:
            raise ValueError("模型文件清单过大")
        items = json.loads(raw)
        if not isinstance(items,list):
            raise ValueError("模型文件清单无效")
        selected = [item for item in items if isinstance(item,dict) and item.get("type")=="file" and item.get("path") in MODEL_FILES]
        if not {"config.json","model.bin","tokenizer.json"}.issubset({item["path"] for item in selected}):
            raise ValueError("模型来源缺少必要文件")
        if any(type(item.get("size")) is not int or not 0<item["size"]<=MAX_MODEL_FILE_BYTES for item in selected):
            raise ValueError("模型文件大小无效或超过 1 GiB")
        total, completed = sum(item["size"] for item in selected), 0
        for item in selected:
            _check(stop)
            path = folder/item["path"]
            if path.resolve().parent != folder:
                raise ValueError("模型文件链接超出目标目录")
            url = "https://huggingface.co/"+repository+"/resolve/main/"+item["path"]
            if path.is_file() and _digest_file(path,item,stop):
                completed += item["size"]
                _emit(report,stop,"model_download","已校验并复用 "+item["path"],url=url,filename=item["path"],
                      downloaded_bytes=completed,total_bytes=total,percent=completed*100/total)
                continue
            temporary = path.with_name(path.name+".part")
            if temporary.is_symlink():
                raise ValueError("模型下载临时文件不能是符号链接")
            count, last_report = 0, 0.
            try:
                _emit(report,stop,"model_download","正在下载 "+item["path"],url=url,filename=item["path"],
                      downloaded_bytes=completed,total_bytes=total,percent=completed*100/total)
                with urlopen(Request(url,headers={"User-Agent":"YinggeLocalASR/1.0"}),timeout=15) as response, temporary.open("wb") as out:
                    while block := response.read(256*1024):
                        _check(stop)
                        count += len(block)
                        if count>item["size"]:
                            raise ValueError("下载文件超过声明的大小")
                        out.write(block)
                        if time.monotonic()-last_report>.15:
                            last_report=time.monotonic()
                            _emit(report,stop,"model_download","正在下载 "+item["path"],url=url,filename=item["path"],
                                  downloaded_bytes=completed+count,total_bytes=total,percent=(completed+count)*100/total)
                if not _digest_file(temporary,item,stop):
                    raise ValueError("模型文件校验失败："+item["path"])
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            completed += count
        result = validate_model(folder)
        _emit(report,stop,"model_ready","本地模型已下载并校验",model_path=str(result),downloaded_bytes=total,total_bytes=total,percent=100.)
        return result
    finally:
        lock.close()


def _extract_audio(source, output, *, origin, duration, report, stop):
    """Render audio on the video's presentation timeline; preserve silence gaps."""
    import av
    written, last_report = 0, -1
    limit = math.ceil(duration*RATE)
    with av.open(str(source)) as container, wave.open(str(output),"wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        streams = list(container.streams.audio)
        if not streams:
            raise ValueError("影片没有音轨，无法进行语音转写")
        stream = next((item for item in streams if int(item.disposition)&1),streams[0])
        resampler = av.AudioResampler(format="s16",layout="mono",rate=RATE)
        fallback = float((stream.start_time or 0)*stream.time_base)-origin
        def consume(frame):
            nonlocal written,fallback,last_report
            _check(stop)
            timestamp = float(frame.pts*frame.time_base)-origin if frame.pts is not None else fallback
            values = frame.to_ndarray().reshape(-1).astype("<i2",copy=False)
            fallback = timestamp+len(values)/RATE
            target = round(timestamp*RATE)
            if target<0:
                values=values[min(len(values),-target):]
                target=0
            target=min(target,limit)
            while written<target:
                _check(stop)
                silence=min(RATE,target-written)
                out.writeframesraw(b"\0\0"*silence)
                written+=silence
            if target<written:
                values=values[min(len(values),written-target):]
            values=values[:max(0,limit-written)]
            out.writeframesraw(values.tobytes())
            written+=len(values)
            bucket=int(written/RATE)
            if bucket!=last_report:
                last_report=bucket
                _emit(report,stop,"extract_audio",f"提取声音 · {written/RATE:.1f} / {duration:.1f} 秒",
                      processed_seconds=written/RATE,duration_seconds=duration,percent=min(100.,written*100/max(1,limit)))
        for frame in container.decode(stream):
            _check(stop)
            for converted in resampler.resample(frame):
                consume(converted)
            if written>=limit:
                break
        for converted in resampler.resample(None):
            consume(converted)
    if written==0:
        raise ValueError("音轨中没有可解码的声音")
    return written


def transcribe_movie(root, movie_id, *, model_path, device="cpu", compute_type="int8", language=None,
                     report=lambda event: None, stop=None):
    """Return actual ASR segments, never an inferred summary or confirmed speaker."""
    _check(stop)
    model_path=validate_model(model_path)
    if device not in {"cpu","cuda"} or compute_type not in {"int8","float32","float16","int8_float16"}:
        raise ValueError("无效的转写设备或计算精度")
    if language is not None and (not isinstance(language,str) or not re.fullmatch(r"[a-z]{2,3}",language)):
        raise ValueError("语音语言应为 zh、en 等代码，或留空自动检测")
    if not dependencies_available():
        raise ValueError("缺少本地语音组件，请运行 .venv\\Scripts\\python.exe -m pip install -r requirements-learning.txt")
    if not _INFERENCE_LOCK.acquire(blocking=False):
        raise ValueError("已有本地语音转写任务在运行，请等待完成")
    model=None
    try:
        if type(movie_id) is not int or movie_id<=0 or not (Path(root)/"annotations.sqlite").is_file():
            raise ValueError("请先选择工程内已有影片")
        with connect(root) as db:
            row=db.execute("SELECT * FROM movies WHERE id=?",(movie_id,)).fetchone()
        if row is None:
            raise ValueError("所选影片已不存在")
        movie=dict(row)
        source=_check_source(movie)
        with open_movie(source,lambda message:_emit(report,stop,"probe",message)) as video:
            origin,duration=float(video.origin),video.duration
        if duration<=0 or duration>MAX_DURATION_SECONDS:
            raise ValueError("本地语音转写支持时长不超过 12 小时的影片")
        with tempfile.TemporaryDirectory(prefix="yingge-asr-") as temporary:
            folder=Path(temporary)
            audio=folder/"audio.wav"
            _emit(report,stop,"extract_audio","正在提取 16 kHz 单声道音频",source_path=str(source),duration_seconds=duration)
            samples=_extract_audio(source,audio,origin=origin,duration=duration,report=report,stop=stop)
            _check_source(movie)
            _emit(report,stop,"load_model","正在加载本地 Whisper 模型",model_path=str(model_path))
            from faster_whisper import WhisperModel
            model=WhisperModel(str(model_path),device=device,compute_type=compute_type,local_files_only=True,
                               cpu_threads=min(4,os.cpu_count() or 1),num_workers=1)
            _check(stop)
            segments,detected=[],language
            with wave.open(str(audio),"rb") as source_wave:
                offset=0
                while offset<samples:
                    _check(stop)
                    chunk=folder/"chunk.wav"
                    count=min(CHUNK_SECONDS*RATE,samples-offset)
                    with wave.open(str(chunk),"wb") as out:
                        out.setnchannels(1)
                        out.setsampwidth(2)
                        out.setframerate(RATE)
                        out.writeframes(source_wave.readframes(count))
                    _emit(report,stop,"transcribe",f"本地转写 · {offset/RATE:.1f} / {samples/RATE:.1f} 秒",
                          processed_seconds=offset/RATE,duration_seconds=samples/RATE,percent=offset*100/samples)
                    stream,info=model.transcribe(str(chunk),language=detected,beam_size=5,vad_filter=True,
                                                 condition_on_previous_text=False)
                    detected=detected or info.language
                    for item in stream:
                        _check(stop)
                        if not all(math.isfinite(value) for value in (item.start,item.end)):
                            raise ValueError("语音模型返回无效时间码")
                        start=max(round(offset/RATE*1000),round((offset/RATE+item.start)*1000))
                        end=min(round(duration*1000),round((offset/RATE+min(item.end,count/RATE))*1000))
                        if not isinstance(item.text,str) or any(0xD800<=ord(char)<=0xDFFF or
                            (ord(char)<32 and char not in "\n\r\t") for char in item.text):
                            raise ValueError("语音模型返回无效对白文本")
                        text=item.text.strip()
                        if text and end>start:
                            if segments:
                                start=max(start,segments[-1]["endMs"])
                            if end>start:
                                segments.append({"startMs":start,"endMs":end,"text":text})
                        if len(segments)>MAX_SEGMENTS:
                            raise ValueError("语音转写超过 100000 段，请按章节拆分影片")
                        _emit(report,stop,"transcribe",f"已转写 {end/1000:.1f} 秒 · {len(segments)} 段",
                              processed_seconds=end/1000,duration_seconds=samples/RATE,percent=min(100.,end/(samples/RATE*10)))
                    offset+=count
            _check_source(movie)
            _emit(report,stop,"transcribed",f"本地转写完成 · {len(segments)} 段" if segments else "未识别到可用对白，请检查音轨或补充字幕",
                  processed_seconds=samples/RATE,duration_seconds=samples/RATE,percent=100.,segment_count=len(segments))
            return {"version":1,"unit":"ms","provider":"faster-whisper-local","language":detected,
                    "durationMs":round(duration*1000),"segments":segments,"movie_id":movie_id,
                    "source_digest":movie["digest"],"model_path":str(model_path),"model":"faster-whisper"}
    finally:
        del model
        _INFERENCE_LOCK.release()
