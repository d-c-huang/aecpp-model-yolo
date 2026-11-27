"""Prepare local models for a user-started continuous learning session.

Existing services/models are reused. This never installs system software, uses
remote inference, or submits media during setup. Settings publish only after
all required services, capabilities and local files have been checked.
"""
import importlib
import os
from pathlib import Path
import shutil
import subprocess
import time

from video_analysis import load_settings, save_settings, model_path
from video_model_setup import VISION_MODEL, prepare_vision_model
from video_semantics import AnalysisCancelled, ModelSetupRequired, _request, list_models
from video_transcription import validate_model, download_model


LOCAL_BASE_URL = "http://127.0.0.1:11434"
ASR_CACHE = Path(__file__).resolve().parent/"weights"/"asr"
SERVICE_START_SECONDS = 30


def _stopped(stop):
    return bool(stop.is_set() if hasattr(stop, "is_set") else stop()) if stop is not None else False


def _check(stop):
    if _stopped(stop):
        raise AnalysisCancelled("本地模型自动配置已取消；原设置保留")


def _emit(report, stop, phase, message, **details):
    _check(stop)
    report({"phase": phase, "message": message, **details})


def _dependencies():
    missing = [name for name in ("ffmpeg", "ffprobe") if not shutil.which(name)]
    if missing:
        raise ValueError("自动学习缺少 " + " / ".join(missing) + "；请安装 FFmpeg 并将 bin 目录加入 PATH，重启应用后重试")
    try:
        # Import validates native DLL availability without loading Whisper weights.
        importlib.import_module("faster_whisper")
    except (ImportError, OSError) as exc:
        raise ValueError("本地语音依赖不可用，请运行应用的 setup.cmd 完成依赖安装后重试") from exc


def _ollama_executable():
    found = shutil.which("ollama")
    if found:
        return str(Path(found).resolve())
    local = os.environ.get("LOCALAPPDATA")
    candidates = [Path(local)/"Programs"/"Ollama"/"ollama.exe"] if local else []
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    raise ValueError("未找到本地 Ollama；请安装 Ollama 后再次点击一键启动，已有学习成果和模型缓存会保留")


def _stop_owned(process):
    """Clean up only the server this setup started, never an existing service."""
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def _models(stop):
    return list_models(base_url=LOCAL_BASE_URL, provider="ollama", timeout=2, stop=stop)


def _ensure_service(report, stop):
    _emit(report, stop, "model-check", "正在检查本机 Ollama 服务和已安装模型", url=LOCAL_BASE_URL+"/api/tags")
    try:
        names = _models(stop)
        _emit(report, stop, "service-ready", "复用已运行的本机 Ollama 服务", url=LOCAL_BASE_URL)
        return names, None
    except (ModelSetupRequired, TimeoutError, ConnectionError):
        _check(stop)
    executable = _ollama_executable()
    _emit(report, stop, "service-start", "正在后台启动本机 Ollama 服务", url=LOCAL_BASE_URL, executable=executable)
    process = subprocess.Popen([executable, "serve"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, env={**os.environ, "OLLAMA_HOST": "127.0.0.1:11434"},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), start_new_session=os.name != "nt")
    deadline = time.monotonic()+SERVICE_START_SECONDS
    try:
        while time.monotonic() < deadline:
            _check(stop)
            try:
                names = _models(stop)
                _emit(report, stop, "service-ready", "本机 Ollama 服务已就绪", url=LOCAL_BASE_URL)
                # Another instance may have won the port while our child exited.
                return names, process if process.poll() is None else None
            except (ModelSetupRequired, TimeoutError, ConnectionError):
                if process.poll() is not None:
                    raise ValueError("Ollama 启动后已退出；请检查本机 11434 端口和 Ollama 安装后重试")
            if hasattr(stop, "wait"):
                stop.wait(.2)
            else:
                time.sleep(.2)
        raise ValueError("等待本机 Ollama 启动超时，请检查 Ollama 安装后重试")
    except BaseException:
        _stop_owned(process)
        raise


def _verify_vision(stop):
    value = _request(LOCAL_BASE_URL+"/api/show", payload={"model": VISION_MODEL}, timeout=20, stop=stop)
    capabilities = value.get("capabilities", [])
    if not isinstance(capabilities, list) or "vision" not in capabilities or "completion" not in capabilities:
        raise ValueError("本机 qwen2.5vl:3b 未声明完整的视觉与文本能力，请更新 Ollama 或重新准备模型")


def _free_space(path, minimum, label):
    selected = Path(path).expanduser().resolve()
    while not selected.exists() and selected != selected.parent:
        selected = selected.parent
    if shutil.disk_usage(selected).free < minimum:
        raise ValueError(f"{label}所在磁盘空间不足，需要至少 {minimum/1024**3:.1f} GiB 可用空间后重试")


def _prepare_asr(settings, report, stop):
    configured = model_path(settings) if settings["asr_model_path"] else ASR_CACHE/settings["asr_model"]
    candidates = [(configured, settings["asr_model"])]
    candidates += [(ASR_CACHE/size, size) for size in ("tiny", "base", "small")]
    seen = set()
    for path, size in candidates:
        _check(stop)
        path = Path(path).resolve()
        if path in seen:
            continue
        seen.add(path)
        try:
            ready = validate_model(path)
        except (OSError, ValueError):
            continue
        _emit(report, stop, "asr-ready", "复用现有本地语音模型 · "+str(ready), model=size, model_path=str(ready))
        return ready, size, True
    _free_space(ASR_CACHE, 256*1024**2, "语音模型缓存")
    _emit(report, stop, "asr-prepare", "缺少可用语音模型，正在准备 tiny；下载文件会逐项校验", model="tiny")
    ready = download_model("tiny", ASR_CACHE, report=report, stop=stop)
    return validate_model(ready), "tiny", False


def prepare_local_learning_models(root, *, report=lambda event: None, stop=None):
    """Return settings and observed setup details; stop accepts Event or callable.

    Downloading the small presets is authorized by the one-click action. A stopped
    or failed setup preserves the old settings. Verified model caches remain for
    retries. A newly started server stays running on success for later inference.
    """
    root = Path(root).resolve()
    _emit(report, stop, "dependencies", "正在检查 FFmpeg 和本地语音分析依赖")
    _dependencies()
    _check(stop)
    settings = load_settings(root)
    process = None
    try:
        installed, process = _ensure_service(report, stop)
        _check(stop)
        reused_vision = VISION_MODEL in installed
        if reused_vision:
            _emit(report, stop, "vision-check", "正在校验已安装视觉模型 · "+VISION_MODEL,
                  model=VISION_MODEL, url=LOCAL_BASE_URL+"/api/show")
            _verify_vision(stop)
            _emit(report, stop, "vision-ready", "复用已安装视觉模型 · "+VISION_MODEL, model=VISION_MODEL, url=LOCAL_BASE_URL)
        else:
            model_store = os.environ.get("OLLAMA_MODELS") or Path.home()/".ollama"/"models"
            _free_space(model_store, 4*1024**3, "Ollama 模型缓存")
            prepare_vision_model(base_url=LOCAL_BASE_URL, report=report, stop=stop)
            _verify_vision(stop)
        asr_path, asr_size, reused_asr = _prepare_asr(settings, report, stop)
        _check(stop)
        settings.update(enabled=True, asr_enabled=True, semantic_enabled=True, visual_enabled=True, keyframes=True,
            provider="ollama", base_url=LOCAL_BASE_URL, model=VISION_MODEL, visual_model=VISION_MODEL,
            api_key_env="", asr_model=asr_size, asr_model_path=str(asr_path))
        _emit(report, stop, "model-settings", "本地模型已检查完毕，正在保存自动分析设置", model=VISION_MODEL,
              model_path=str(asr_path), url=LOCAL_BASE_URL)
        _check(stop)
        saved = save_settings(root, settings)
    except BaseException:
        _stop_owned(process)
        raise
    # Publication is complete: a late cancel must not report that old settings
    # were preserved, nor kill a service that was handed to active analysis.
    result = {"settings": saved, "base_url": LOCAL_BASE_URL, "visual_model": VISION_MODEL,
              "text_model": VISION_MODEL, "asr_model_path": str(asr_path),
              "service_started": process is not None, "reused": {"vision": reused_vision, "asr": reused_asr},
              "warnings": []}
    report({"phase": "models-ready", "message": "本地语音、内容分析和画面理解已就绪；开始持续查找与学习",
            "url": LOCAL_BASE_URL, "model": VISION_MODEL, "model_path": str(asr_path)})
    return result
