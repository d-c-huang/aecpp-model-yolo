"""Present observed queue/analysis/training state without inventing progress."""


def candidate_status(candidate, download, reference=None, temporal=None):
    if download is None:
        return {"state": "failed", "detail": "下载记录已移除；可重新加入所选素材"}
    state = download["status"]
    result = {"state": "downloading" if state == "running" else state,
              "detail": download.get("error") or download.get("message", ""),
              "phase": download.get("phase", state), "current_url": download.get("url", ""),
              "speed": download.get("speed"), "eta": download.get("eta"),
              "downloaded_bytes": download.get("downloaded_bytes"), "total_bytes": download.get("total_bytes")}
    if download.get("progress") is not None:
        result["progress"] = download["progress"]
    if state == "skipped":
        result.update(detail=download.get("skip_reason") or result["detail"],
                      skip_reason=download.get("skip_reason") or result["detail"],
                      error_code=download.get("error_code", ""), progress=None,
                      speed=None, eta=None)
    if state != "completed":
        return result
    # A finished download is not a measured learning percentage.
    result.update(progress=None,speed=None,eta=None,downloaded_bytes=None,total_bytes=None)
    movie_id = (download.get("result") or {}).get("movie_id")
    if movie_id is not None:
        result["movie_id"] = movie_id
    if download.get("learning_cancelled"):
        result.update(state="cancelled", detail="视频已导入，后续学习请求已取消", phase="cancelled")
        return result
    reference = reference or {"status": "waiting", "message": "等待交接字幕节奏分析"}
    parts = {"reference": {key: reference.get(key) for key in
                           ("status", "phase", "message", "example_count")}}
    labels = ["字幕节奏：" + (reference.get("message") or reference["status"])]
    if candidate.get("learning_kind") == "story-and-movie":
        temporal = temporal or {"status": "waiting", "message": "等待交接电影时序训练"}
        parts["temporal"] = {key: temporal.get(key) for key in ("status", "message", "run")}
        labels.append("电影时序：" + (temporal.get("message") or temporal["status"]))
    states = {part["status"] for part in parts.values()}
    if states & {"running", "queued"}:
        state = "learning"
    elif "failed" in states:
        state = "failed"
    elif states & {"paused", "unfinished", "stopped", "interrupted"}:
        state = "paused"
    elif "cancelled" in states:
        state = "cancelled"
    elif "waiting" in states:
        state = "learning" if not download.get("learning_dispatched") else "paused"
        if state == "paused":
            labels.append("学习已交接但暂无活动记录，请在学习任务中继续")
    elif states & {"needs_input","partial","needs_setup"}:
        state = "needs_input"
    elif states == {"completed"}:
        state = "completed"
    else:
        state = "paused"
    result.update(state=state, detail="；".join(labels), learning=parts, phase="learning")
    return result
