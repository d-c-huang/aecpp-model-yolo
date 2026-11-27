"""Explicit, cancellable local Ollama vision-model preparation with real progress."""
import http.client
import ipaddress
import json
import time
from urllib.parse import urlsplit

from video_semantics import _check, _request, _ResponseSocket, validate_base_url

# This older instruct model works with Ollama >= 0.7 and does not spend the
# bounded visual-output budget on mandatory thinking. qwen3-vl:2b currently
# aliases its thinking variant; newer instruct tags require a newer server.
VISION_MODEL = "qwen2.5vl:3b"


def prepare_vision_model(*, base_url="http://127.0.0.1:11434", report=lambda event: None, stop=None):
    """Download only the documented small preset into the user's local Ollama store.

    The API reports layer bytes, not an overall download percentage. Cancelling
    closes this request; Ollama owns partial blobs and may share other pulls.
    """
    base_url = validate_base_url(base_url)
    parsed = urlsplit(base_url)
    local = parsed.hostname.lower() == "localhost"
    try:
        local = local or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        pass
    if not local:
        raise ValueError("一键准备仅用于本机 Ollama；远程服务请填写已部署的视觉模型名称")
    _check(stop)
    url = base_url + "/api/pull"
    connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(parsed.hostname, parsed.port, timeout=5)
    deadline = time.monotonic() + 7200
    class Response(http.client.HTTPResponse):
        def __init__(self, sock, *args, **kwargs):
            super().__init__(_ResponseSocket(sock, deadline, stop), *args, **kwargs)
    connection.response_class = Response
    response = None
    report({"phase":"vision-download", "message":f"正在通过本机 Ollama 准备 {VISION_MODEL}（约 3.2 GB）", "url":url})
    try:
        connection.request("POST", urlsplit(url).path,
                           json.dumps({"model":VISION_MODEL,"stream":True}).encode(),
                           {"Content-Type":"application/json"})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError(f"视觉模型准备失败（HTTP {response.status}）；请检查 Ollama 是否已启动")
        success = False
        for _ in range(100000):
            _check(stop)
            line = response.readline(16385)
            if not line:
                break
            if len(line)>16384:
                raise ValueError("模型下载进度响应过大")
            event = json.loads(line)
            if not isinstance(event,dict):
                raise ValueError("模型下载进度格式无效")
            if event.get("error"):
                # Do not echo arbitrary server error bodies, credentials or URLs.
                raise ValueError("Ollama 未完成视觉模型下载；请检查网络、磁盘空间与 Ollama 版本后重试")
            status = str(event.get("status","正在下载"))[:200]
            progress = {"phase":"vision-download","message":f"{VISION_MODEL} · {status}","url":url}
            total, done = event.get("total"), event.get("completed",0)
            if type(total) is int and total>0 and type(done) is int and 0<=done<=total:
                progress.update(completed=done,total=total,percent=100*done/total)
                progress["message"] += f" · 当前文件 {done/1048576:.1f}/{total/1048576:.1f} MiB"
            report(progress)
            if status=="success":
                success = True
                break
        _check(stop)
        if not success:
            raise ValueError("视觉模型下载尚未完成，可再次点击准备以续传")
    finally:
        if response is not None:
            response.close()
        connection.close()
    value = _request(base_url+"/api/show", payload={"model":VISION_MODEL}, stop=stop, report=report, timeout=20)
    if "vision" not in value.get("capabilities",[]):
        raise ValueError("下载后的模型未声明视觉能力，请检查 Ollama 版本")
    report({"phase":"vision-ready","message":f"视觉模型已就绪 · {VISION_MODEL}","url":base_url+"/api/show"})
    return VISION_MODEL
