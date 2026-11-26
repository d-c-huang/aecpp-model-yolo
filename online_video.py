"""Cache finite online videos before admitting them to a local learning project."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urljoin, urlsplit, urlunsplit
import uuid

import dataset
from media import Movie, open_movie


VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".ts", ".avi", ".flv", ".mpeg", ".mpg", ".ogv"}
SUBTITLE_EXTENSIONS = {".srt", ".vtt", ".ass", ".ssa"}


def _progress_event(phase, **values):
    """Stable, JSON-safe values; a missing total is genuinely indeterminate."""
    event = {"phase": phase, "downloaded_bytes": None, "total_bytes": None,
             "percent": None, "speed": None, "eta": None, "title": ""}
    for key in ("downloaded_bytes", "total_bytes", "percent", "speed", "eta"):
        value = values.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value>=0:
            event[key] = value
    if event["total_bytes"]==0:
        event["total_bytes"] = None
    if event["percent"] is not None:
        event["percent"] = min(100., event["percent"])
    event["title"] = str(values.get("title") or "")[:180]
    return event


def validate_url(value):
    value = str(value).strip()
    if not value or any(ord(character)<32 for character in value):
        raise ValueError("请填写完整的视频网页链接或视频直链")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("视频链接格式无效") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("在线视频仅接受 http:// 或 https:// 链接")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("请使用不含账户密码的视频链接")
    if port is not None and not 1<=port<=65535:
        raise ValueError("视频链接端口无效")
    # Fragments do not identify downloaded content. Keep query parameters, which
    # some providers use to identify a video or authorize its media request.
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path or "/", parsed.query, ""))


def _write_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name+"."+uuid.uuid4().hex+".tmp")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _read_json(path):
    try:
        value = Path(path).read_text(encoding="utf-8")
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (OSError, ValueError):
        return {}


def _inside(folder, value):
    folder, path = Path(folder).resolve(), Path(value).resolve()
    if path==folder or not path.is_relative_to(folder):
        raise ValueError("在线缓存路径无效，请重新导入链接")
    return path


def _friendly_error(value):
    message = str(value)
    lower = message.lower()
    if any(word in lower for word in ("drm", "protected content")):
        return "此视频受 DRM 保护，无法读取。请改用可直接读取的视频来源。"
    if any(word in lower for word in ("sign in", "login", "log in", "cookies", "403", "401", "private video", "members-only")):
        return "视频来源需要登录或未允许当前请求。请使用可直接访问的链接，或将可播放的视频文件导入工程。"
    if any(word in lower for word in ("unsupported url", "no suitable extractor")):
        return "暂不支持这个视频网页。可以改用 MP4/M3U8 直链，或先下载视频文件再导入。"
    if any(word in lower for word in ("timed out", "timeout", "connection", "resolve", "network", "unreachable")):
        return "连接视频来源失败，请检查网络或链接是否已过期，然后重试。"
    if "ffmpeg" in lower and any(word in lower for word in ("not found", "not installed", "unable to find")):
        return "合并在线视频需要 FFmpeg，请安装 FFmpeg 并加入 PATH 后重试。"
    # Avoid placing signed query URLs or server credentials into the app log.
    message = re.sub(r"https?://[^\s\"'<>]+", "[视频来源]", message)
    return "在线视频读取失败 · "+message.replace("ERROR: ", "")[:350]


def _validate_info(info):
    if not isinstance(info, dict):
        raise ValueError("链接没有返回可用的视频信息")
    if info.get("_type") in {"playlist", "multi_video"} or info.get("entries") is not None:
        # Generic HTML pages represent even one <video> as a playlist. Permit
        # that single entry while refusing collection/batch downloads.
        from itertools import islice
        entries = list(islice(iter(info.get("entries") or []), 2))
        if len(entries)==1:
            return _validate_info(entries[0])
        raise ValueError("请填写单个视频的链接，暂不批量读取播放列表或合集")
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
        raise ValueError("请使用已结束且可完整读取的视频，暂不学习正在直播或尚未开始的内容")
    if info.get("has_drm"):
        raise ValueError("此视频受 DRM 保护，无法读取")
    duration = info.get("duration")
    if duration is not None and (not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration<=0):
        raise ValueError("视频时长无效，请使用可完整读取的视频")
    return info


def _finite_hls(ydl, info):
    """Do not mistake a moving live HLS playlist for a complete movie."""
    from yt_dlp.networking import Request
    checked = set()

    def inspect(url, headers, depth=0):
        url = validate_url(url)
        if url in checked:
            return
        checked.add(url)
        if depth>4:
            raise ValueError("在线视频播放列表层级过深，请改用视频文件直链")
        with ydl.urlopen(Request(url, headers=headers or {})) as response:
            body = response.read(2*1024*1024+1)
        if len(body)>2*1024*1024:
            raise ValueError("视频播放列表过大，请先下载为视频文件后导入")
        text = body.decode("utf-8-sig")
        if "#EXT-X-STREAM-INF" in text:
            lines = [line.strip() for line in text.splitlines()]
            for index, line in enumerate(lines):
                if line.startswith("#EXT-X-STREAM-INF"):
                    child = next((item for item in lines[index+1:] if item and not item.startswith("#")), None)
                    if child:
                        inspect(urljoin(url, child), headers, depth+1)
                        return
            raise ValueError("视频播放列表没有可读取的媒体")
        if "#EXT-X-ENDLIST" not in {line.strip() for line in text.splitlines()}:
            raise ValueError("这个 M3U8 尚未结束，暂不学习实时直播。请使用完整点播视频。")

    formats = info.get("requested_formats") or [info]
    for selected in formats:
        if "m3u8" in (selected.get("protocol") or ""):
            inspect(selected["url"], selected.get("http_headers", info.get("http_headers")))


def _download_impl(request, emit, factory=None, on_progress=None):
    """Called in an isolated worker so cancellation can terminate FFmpeg too."""
    if factory is None:
        try:
            from yt_dlp import YoutubeDL
        except ImportError as exc:
            raise ValueError("缺少在线视频组件，请在客户端目录运行 python -m pip install -r requirements.txt") from exc
        factory = YoutubeDL
    folder = Path(request["folder"]).resolve()
    height = request["height"]
    # Unknown-height direct files remain readable. Known formats are bounded,
    # without an unrestricted fallback that silently chooses a larger stream.
    cap = f"[height<=?{height}]" if height else ""
    format_selector = f"bv*{cap}+ba/b{cap}"

    class Logger:
        def debug(self, text):
            pass
        def warning(self, text):
            emit("提示 · "+_friendly_error(text))
        def error(self, text):
            pass

    title = ""
    last_update = [0., None]
    def details(phase, **values):
        if on_progress is not None:
            on_progress(_progress_event(phase, title=title, **values))

    def hook(data):
        status = data.get("status")
        if status=="downloading":
            event = _progress_event("downloading", downloaded_bytes=data.get("downloaded_bytes"),
                                    total_bytes=data.get("total_bytes") or data.get("total_bytes_estimate"),
                                    speed=data.get("speed"), eta=data.get("eta"), title=title)
            done, total = event["downloaded_bytes"], event["total_bytes"]
            if done is not None and total:
                event["percent"] = min(100., done*100/total)
            now = time.monotonic()
            # Progress hooks run for each fragment and subtitle too. Keep UI and
            # IPC traffic bounded while always reporting the first active file.
            identity = str(data.get("filename") or data.get("tmpfilename") or "")
            if identity!=last_update[1] or now-last_update[0]>=.2 or (total and done is not None and done>=total):
                last_update[:] = [now, identity]
                if on_progress is not None:
                    on_progress(event)
                amount = f"{(done or 0)/1048576:.1f} / {total/1048576:.1f} MB" if total else f"{(done or 0)/1048576:.1f} MB"
                emit("缓存在线视频 · "+amount)
        elif status=="finished":
            last_update[:] = [0., None]
            details("merging")
            emit("下载已完成，正在整理视频与声音…")

    def postprocess(_):
        details("merging")
        emit("正在合并视频与声音…")

    options = {"quiet": True, "no_warnings": True, "logger": Logger(), "noplaylist": True, "playlistend": 2,
               "format": format_selector, "outtmpl": str(folder/"media.%(ext)s"),
               "merge_output_format": "mkv", "progress_hooks": [hook],
               "postprocessor_hooks": [postprocess],
               "socket_timeout": 15, "retries": 3, "fragment_retries": 3, "extractor_retries": 2,
               "continuedl": True, "overwrites": False, "hls_prefer_native": True,
               "concurrent_fragment_downloads": 1, "restrictfilenames": True,
               "windowsfilenames": True, "cachedir": False, "ignoreerrors": False,
               "writesubtitles": bool(request["subtitles"]), "writeautomaticsub": bool(request["subtitles"]),
               "subtitleslangs": ["zh.*", "en.*"], "subtitlesformat": "vtt/srt/ass/best"}
    with factory(options) as ydl:
        details("resolving")
        emit("正在解析在线视频信息…")
        info = _validate_info(ydl.extract_info(request["url"], download=False))
        _finite_hls(ydl, info)
        title = " ".join(str(info.get("title") or info.get("id") or "在线视频").split())[:180]
        details("downloading")
        emit("开始缓存 · "+title)
        # Process the already inspected entry, so a playlist/live redirect cannot
        # silently replace the entry between metadata lookup and download.
        subtitle_warning = ""
        try:
            finished = ydl.process_ie_result(info, download=True)
        except Exception as exc:
            if not request["subtitles"] or "unable to download video subtitles" not in str(exc).lower():
                raise
            subtitle_warning = "部分在线字幕无法读取，已保留可用字幕；也可稍后手动导入字幕文件"
            emit(subtitle_warning)
            ydl.params.update(writesubtitles=False, writeautomaticsub=False)
            finished = ydl.process_ie_result(info, download=True)
        _validate_info(finished)
        candidates = [path for path in folder.glob("media.*") if path.stem=="media" and path.suffix.lower() in VIDEO_EXTENSIONS]
        candidates = [path for path in candidates if path.is_file()]
        if not candidates:
            raise ValueError("下载未生成完整视频文件，请检查来源是否只有音频或是否需要登录")
        # A merger may leave intermediate video-only files, named media.f<id>.*;
        # the stem check above excludes them.
        if len(candidates)!=1:
            selected = finished.get("filepath")
            candidates = [path for path in candidates if selected and path.resolve()==Path(selected).resolve()]
        if len(candidates)!=1:
            raise ValueError("无法确定下载后的完整视频，请重新导入链接")
        return {"path": str(_inside(folder, candidates[0])), "title": title,
                "extractor": info.get("extractor_key") or info.get("extractor"),
                "video_id": str(info.get("id", "")), "duration": info.get("duration"),
                "subtitle_warning": subtitle_warning}


class _ProcessTree:
    """Own the downloader and all merger subprocesses until it is finished."""
    def __init__(self, proc):
        self.proc, self.job = proc, None
        if os.name!="nt":
            return
        import ctypes
        from ctypes import wintypes
        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                             "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        job = kernel.CreateJobObjectW(None, None)
        if not job:
            raise OSError("无法创建在线视频后台任务")
        limits = Extended()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        handle = kernel.OpenProcess(0x0100|0x0001, False, proc.pid)
        try:
            if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not handle or not kernel.AssignProcessToJobObject(job, handle):
                raise OSError("无法隔离在线视频下载任务，请重试")
        except BaseException:
            kernel.CloseHandle(job)
            raise
        finally:
            if handle:
                kernel.CloseHandle(handle)
        self.kernel, self.job = kernel, job

    def close(self):
        if self.job is not None:
            self.kernel.CloseHandle(self.job)
            self.job = None
        elif self.proc.poll() is None:
            if os.name!="nt":
                try:
                    os.killpg(self.proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            else:
                self.proc.terminate()


def _run_download(folder, request, report, stop, on_progress=None):
    request_path, response_path = folder/"request.json", folder/"download.json"
    _write_json(request_path, request)
    response_path.unlink(missing_ok=True)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen([sys.executable, "-u", str(Path(__file__).resolve()), "--worker", str(request_path), str(response_path)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8", errors="replace",
                            creationflags=flags, start_new_session=os.name!="nt",
                            env=dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8"))
    tree = None
    events = queue.Queue()
    def reader():
        with proc.stdout:
            for line in proc.stdout:
                try:
                    event = json.loads(line)
                    if isinstance(event, dict) and ("progress" in event or isinstance(event.get("details"), dict)):
                        events.put(event)
                except ValueError:
                    pass
    def dispatch(event):
        if "progress" in event:
            report(str(event["progress"]))
        elif on_progress is not None:
            on_progress(event["details"])
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        tree = _ProcessTree(proc)
        while proc.poll() is None or not events.empty():
            if stop is not None and stop.is_set():
                raise RuntimeError("在线视频导入已停止，已下载的部分可在重试时继续使用")
            try:
                dispatch(events.get(timeout=.1))
            except queue.Empty:
                continue
        thread.join()
        while not events.empty():
            dispatch(events.get_nowait())
        result = _read_json(response_path)
        if proc.returncode or result.get("error"):
            raise ValueError(result.get("error") or "在线视频后台任务未完成，请重试")
        if not result.get("path"):
            raise ValueError("在线视频没有生成完整缓存，请重试")
        return result
    finally:
        if tree is not None:
            tree.close()
        elif proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        thread.join(timeout=2)


def _digest(path, report):
    digest = hashlib.sha256()
    size = path.stat().st_size
    with path.open("rb") as stream:
        while block := stream.read(8*1024*1024):
            digest.update(block)
            report(f"核对在线视频缓存 · {stream.tell()*100//max(1,size)}%")
    return digest.hexdigest()


def _cached(folder, request):
    info = _read_json(folder/"completed.json")
    if not info or info.get("source_url")!=request["url"] or info.get("height")!=request["height"] or info.get("subtitles")!=request["subtitles"]:
        return None
    try:
        path = _inside(folder, folder/info["relative_path"])
        stat = path.stat()
        if (stat.st_size, stat.st_mtime_ns)!=(info["size"], info["mtime_ns"]):
            return None
    except (OSError, ValueError, KeyError):
        return None
    return {**info, "path": str(path)}


def _media_metadata(path, url, *, duration=None, width=None, height=None, has_audio=None):
    def positive(value):
        return value if isinstance(value, (int, float)) and math.isfinite(value) and value>0 else None
    return {"duration": positive(duration), "width": positive(width), "height": positive(height),
            "size": Path(path).stat().st_size, "source_host": urlsplit(url).hostname or "",
            "has_audio": has_audio}


def _save_preview(image, path):
    """Publish only complete previews decoded from the local video."""
    temporary = path.with_name(path.name+"."+uuid.uuid4().hex+".tmp")
    try:
        preview = image.convert("RGB")
        preview.thumbnail((640, 360))
        preview.save(temporary, format="JPEG", quality=88, optimize=True)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _cache_media_details(folder, info, progress, first_image=None):
    """Upgrade legacy caches without redownloading or decoding the whole movie."""
    path, url = Path(info["path"]), info["source_url"]
    metadata = info.get("metadata")
    complete = isinstance(metadata, dict) and all(key in metadata for key in
                ("duration", "width", "height", "size", "source_host", "has_audio"))
    try:
        if (folder/"preview.jpg").is_symlink():
            raise ValueError("预览缓存不能引用其他文件")
        preview_path = _inside(folder, folder/"preview.jpg")
        has_preview = preview_path.is_file() and preview_path.stat().st_size>0
    except (OSError, ValueError):
        preview_path, has_preview = None, False
    if complete and has_preview:
        info["thumbnail_path"] = str(preview_path)
        return
    if first_image is None:
        progress("正在补全视频信息与本地预览…")
        try:
            import av
            with av.open(str(path)) as container:
                streams = [stream for stream in container.streams.video if not int(stream.disposition)&0x400]
                if not streams:
                    raise ValueError("没有可读取的视频轨")
                stream = max(streams, key=lambda item:(bool(int(item.disposition)&1), item.width*item.height))
                duration = float(stream.duration*stream.time_base) if stream.duration else None
                if not duration and container.duration:
                    duration = container.duration/av.time_base
                metadata = _media_metadata(path, url, duration=duration or info.get("duration"),
                                           width=stream.width, height=stream.height,
                                           has_audio=bool(container.streams.audio))
                frame = next(container.decode(stream), None)
                if frame is None:
                    raise ValueError("首帧无法解码")
                first_image = Movie.frame_image(frame)
                metadata.update(width=first_image.width, height=first_image.height)
        except Exception as exc:
            # This is an optional presentation upgrade; verified cached videos
            # remain importable even if the preview decoder is unavailable.
            progress("提示 · 暂时无法生成视频预览，仍可继续学习 · "+_friendly_error(exc))
    if not isinstance(metadata, dict):
        metadata = _media_metadata(path, url, duration=info.get("duration"))
    info["metadata"] = metadata
    if first_image is not None and not has_preview:
        try:
            if preview_path is None:
                raise ValueError("预览缓存路径无效")
            _save_preview(first_image, preview_path)
            has_preview = True
        except Exception as exc:
            progress("提示 · 视频预览保存失败，仍可继续学习 · "+_friendly_error(exc))
    info["thumbnail_path"] = str(preview_path) if has_preview else ""


@contextmanager
def _cache_lock(folder):
    """An OS file lock is released even when the desktop app is terminated."""
    path = folder/".import.lock"
    descriptor = os.open(path, os.O_CREAT|os.O_RDWR)
    acquired = False
    try:
        if os.fstat(descriptor).st_size==0:
            os.write(descriptor, b"0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            if os.name=="nt":
                import msvcrt
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("这个在线视频正在另一个任务中导入，请等待完成或先停止该任务") from exc
        acquired = True
        yield
    finally:
        if acquired:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name=="nt":
                import msvcrt
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def import_online(root, url, mode, report, *, height=720, subtitles=True, stop=None, on_progress=None):
    """Download once, verify a finite local movie, then use existing import logic."""
    url = validate_url(url)
    if mode not in {"time", "train", "val"}:
        raise ValueError("请为在线视频选择训练或验证分组")
    if type(height) is not int or height not in {0, 360, 480, 720, 1080, 1440, 2160}:
        raise ValueError("在线视频清晰度无效")
    root = Path(root).resolve()
    def check_stop():
        if stop is not None and stop.is_set():
            raise RuntimeError("在线视频导入已停止，已下载的部分可在重试时继续使用")
    def progress(message):
        check_stop()
        report(message)
        check_stop()
    def details(event):
        check_stop()
        if on_progress is not None:
            on_progress(event)
        check_stop()
    details(_progress_event("resolving"))
    progress("准备读取在线视频…")
    config = {"url": url, "height": height, "subtitles": bool(subtitles)}
    key = hashlib.sha256(json.dumps(config, sort_keys=True).encode("utf-8")).hexdigest()
    from cache_storage import locate_cache, storage_lease
    with storage_lease(root):
        folder = locate_cache(root,key)
        folder.mkdir(parents=True, exist_ok=True)
        with _cache_lock(folder):
            return _import_locked(root,folder,config,mode,progress,stop,details)


def _import_locked(root, folder, config, mode, progress, stop, details):
    url, height, subtitles = config["url"], config["height"], config["subtitles"]
    info = _cached(folder, config)
    reused = info is not None
    if reused:
        details(_progress_event("cached", title=info.get("title")))
        progress("复用已完成的在线视频缓存…")
        _cache_media_details(folder, info, progress)
        _write_json(folder/"completed.json", {k:v for k,v in info.items() if k not in {"path", "thumbnail_path"}})
    else:
        pending = _read_json(folder/"pending.json")
        attempt = folder/pending.get("attempt", "")
        if not (pending.get("config")==config and attempt.name.startswith("attempt-") and attempt.is_dir()):
            attempt = folder/("attempt-"+uuid.uuid4().hex)
            attempt.mkdir()
            _write_json(folder/"pending.json", {"config": config, "attempt": attempt.name})
        attempt = _inside(folder, attempt)
        info = _run_download(attempt, {**config, "folder": str(attempt)}, progress, stop, details)
        path = _inside(attempt, info["path"])
        details(_progress_event("probing", title=info.get("title")))
        progress("检查在线视频是否完整可读…")
        with open_movie(path, progress) as movie:
            expected = info.get("duration")
            if expected and movie.duration+max(3, expected*.03)<expected:
                raise ValueError("视频缓存时长不完整，请重新读取来源")
            target = max(0, movie.duration-movie.interval*2)
            frames = list(movie.samples([0, target]))
            if len(frames)!=2 or frames[-1][0]+max(1.5,movie.interval*3)<target:
                raise ValueError("视频缓存没有完整的首尾画面，请重新读取来源")
            info["metadata"] = _media_metadata(path, url, duration=movie.duration,
                                                width=frames[0][1].width, height=frames[0][1].height,
                                                has_audio=bool(movie.container.streams.audio))
        digest = _digest(path, progress)
        stat = path.stat()
        info.update(source_url=url, height=height, subtitles=bool(subtitles),
                    relative_path=path.relative_to(folder).as_posix(), size=stat.st_size,
                    mtime_ns=stat.st_mtime_ns, sha256=digest, version=1,
                    downloaded_at=datetime.now(timezone.utc).isoformat())
        _cache_media_details(folder, info, progress, frames[0][1])
        # Completion only follows successful downloader exit, decoding and hashing.
        _write_json(folder/"completed.json", {k:v for k,v in info.items() if k not in {"path", "thumbnail_path"}})
        (folder/"pending.json").unlink(missing_ok=True)
    path = Path(info["path"])
    details(_progress_event("importing", title=info.get("title")))
    progress("将在线视频加入学习工程…")
    with dataset.connect(root) as db:
        previous = db.execute("SELECT * FROM movies WHERE digest=?", (info["sha256"],)).fetchone()
        previous = dict(previous) if previous else None
    # Content deduplication preserves a valid existing canonical source, so an
    # online alias does not silently move an already annotated local movie.
    canonical = path
    if previous and previous.get("source_path"):
        original = Path(previous["source_path"])
        try:
            stat = original.stat()
            if (stat.st_size,stat.st_mtime_ns)==(previous["source_size"],previous["source_mtime"]):
                canonical = original
        except OSError:
            pass
    movie_id, subtitle_report = dataset.import_movie(root, canonical, mode, progress)
    if not previous:
        with dataset.connect(root) as db:
            db.execute("UPDATE movies SET name=? WHERE id=?", (info["title"], movie_id))
    if canonical.resolve()!=path.resolve() and subtitles:
        from subtitles import import_subtitles
        imported = 0
        with dataset.connect(root) as db:
            known = {row[0] for row in db.execute("SELECT fingerprint FROM subtitle_tracks WHERE movie=?", (movie_id,))}
        for caption in path.parent.glob("media.*"):
            if caption.suffix.lower() in SUBTITLE_EXTENSIONS and caption.is_file():
                checksum = hashlib.sha256(caption.read_bytes()).hexdigest()
                if checksum not in known:
                    imported += import_subtitles(root, movie_id, caption, progress)
                    known.add(checksum)
        if imported:
            subtitle_report += f" · 新增在线字幕 {imported} 条"
    if info.get("subtitle_warning"):
        subtitle_report += " · "+info["subtitle_warning"]
    result = {"movie_id": movie_id, "title": info["title"], "path": str(canonical), "reused": reused,
              "subtitle_report": subtitle_report, "source_url": url,
              "metadata": info.get("metadata", {}), "thumbnail_path": info.get("thumbnail_path", "")}
    details(_progress_event("completed", title=info["title"], downloaded_bytes=info["size"],
                            total_bytes=info["size"], percent=100., eta=0))
    return result


if __name__=="__main__":
    if len(sys.argv)!=4 or sys.argv[1]!="--worker":
        raise SystemExit("Use import_online() from the desktop application")
    response = Path(sys.argv[3])
    try:
        result = _download_impl(_read_json(sys.argv[2]),
                                lambda message: print(json.dumps({"progress":message}, ensure_ascii=False), flush=True),
                                on_progress=lambda event: print(json.dumps({"details":event}, ensure_ascii=False), flush=True))
    except Exception as exc:
        _write_json(response, {"error": _friendly_error(exc)})
        raise SystemExit(1)
    _write_json(response, result)
