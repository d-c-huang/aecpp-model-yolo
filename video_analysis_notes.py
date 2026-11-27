"""Validated, unreviewed semantic video notes and editable reference storyboards.

The input shape is compatible with Video Helper's documented result/plan data.
These summaries never become transcript cues or confirmed training examples.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import uuid

import dataset


MAX_BYTES = 16 * 1024 * 1024
MAX_BLOCKS = 5000
MAX_HIGHLIGHTS = 20000
MAX_NODES = 10000
MAX_MS = 10**12
FORMAT = "video-analysis-reference"


def _text(value, label, maximum=2000, *, empty=False):
    if (not isinstance(value, str) or len(value) > maximum or
            any(0xD800 <= ord(char) <= 0xDFFF or (ord(char) < 32 and char not in "\n\r\t") for char in value)):
        raise ValueError(f"{label}文本无效或过长")
    value = value.strip()
    if not empty and not value:
        raise ValueError(f"{label}不能为空")
    return value


def _integer(value, label, maximum=MAX_MS):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{label}须为有效的非负整数")
    return value


def _items(value, label, maximum):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{label}须为不超过 {maximum} 项的列表")
    return value


def _object(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label}须为对象")
    return value


def _range(value, label):
    start = _integer(value.get("startMs"), label+"起点")
    end = _integer(value.get("endMs"), label+"终点")
    if end <= start:
        raise ValueError(f"{label}结束时间必须晚于开始时间")
    return start, end


def _encoded(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("分析内容不是有效 JSON") from exc
    if len(raw) > MAX_BYTES:
        raise ValueError("分析内容不能超过 16 MiB")
    return raw


def _relative_path(value):
    value = _text(value, "关键帧路径", 1000).replace("\\", "/")
    path = PurePosixPath(value)
    if path.is_absolute() or ":" in value or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("关键帧路径必须是工程内相对路径")
    return path.as_posix()


def _keyframe(value, block_range):
    value = _object(value, "关键帧")
    result = {}
    if value.get("timeMs") is not None:
        time = _integer(value["timeMs"], "关键帧时间")
        if not block_range[0] <= time < block_range[1]:
            raise ValueError("关键帧时间必须在所属内容段内")
        result["timeMs"] = time
    for key in ("assetId", "contentUrl", "caption"):
        if value.get(key) is not None:
            result[key] = _text(value[key], "关键帧"+key, 4096 if key=="contentUrl" else 2000, empty=key=="caption")
    if value.get("path") is not None:
        result["path"] = _relative_path(value["path"])
    if not result:
        raise ValueError("关键帧没有时间、资源或说明")
    return result


def normalize_result(payload):
    """Validate explicit result or content_blocks JSON, without guessing wrappers.

    Keeps camelCase result fields. A local plan may omit mindmap and assetRefs.
    The documented legacy singular keyframe is normalized into keyframes.
    """
    payload = _object(payload, "分析结果")
    _encoded(payload)
    schema = _text(payload.get("schemaVersion"), "分析格式版本", 100)
    blocks = _items(payload.get("contentBlocks"), "内容段", MAX_BLOCKS)
    if not blocks:
        raise ValueError("分析结果没有内容段")
    result = {"schemaVersion": schema, "contentBlocks": [], "mindmap": {"nodes": [], "edges": []}, "assetRefs": []}
    for key in ("resultId", "projectId", "pipelineVersion"):
        if key in payload:
            result[key] = _text(payload[key], key, 200)
    if "createdAtMs" in payload:
        result["createdAtMs"] = _integer(payload["createdAtMs"], "结果创建时间", 2**63-1)
    if "analysis" in payload:
        info = _object(payload["analysis"], "分析说明")
        clean_info = {}
        for key in ("generated", "transcript_only"):
            if key in info:
                if type(info[key]) is not bool:
                    raise ValueError("分析说明标记须为布尔值")
                clean_info[key] = info[key]
        if "review_status" in info and info["review_status"] != "unreviewed":
            raise ValueError("机器分析只能保存为尚待审核的参考")
        clean_info["review_status"] = "unreviewed"
        for key in ("provider", "model", "engine", "transcript_hash", "source_hash", "warning"):
            if key in info:
                clean_info[key] = _text(info[key], "分析说明"+key, 4096, empty=True)
        result["analysis"] = clean_info
    block_ids, highlight_ids, highlight_blocks = set(), set(), {}
    highlight_count = 0
    for row in blocks:
        row = _object(row, "内容段")
        identifier = _text(row.get("blockId"), "内容段编号", 100)
        if identifier in block_ids:
            raise ValueError("内容段编号重复")
        block_ids.add(identifier)
        start, end = _range(row, "内容段")
        block = {"blockId": identifier, "idx": _integer(row.get("idx"), "内容段序号", MAX_BLOCKS),
                 "title": _text(row.get("title"), "内容段标题", 300, empty=True),
                 "startMs": start, "endMs": end, "highlights": []}
        for key in ("summary", "analysis"):
            if key in row:
                block[key] = _text(row[key], "内容段"+key, 12000, empty=True)
        if "evidenceSegments" in row:
            block["evidenceSegments"] = []
            for segment in _items(row["evidenceSegments"], "转写证据", MAX_HIGHLIGHTS):
                segment = _object(segment, "转写证据")
                es, ee = _range(segment, "转写证据")
                if not start <= es < ee <= end:
                    raise ValueError("转写证据时间必须在所属内容段内")
                block["evidenceSegments"].append({"startMs": es, "endMs": ee,
                                                 "text": _text(segment.get("text"), "转写证据", 12000)})
        highlights = _items(row.get("highlights", []), "内容重点", MAX_HIGHLIGHTS)
        highlight_count += len(highlights)
        if highlight_count > MAX_HIGHLIGHTS:
            raise ValueError("分析重点总数过多")
        for item in highlights:
            item = _object(item, "内容重点")
            hid = _text(item.get("highlightId"), "内容重点编号", 100)
            if hid in highlight_ids:
                raise ValueError("内容重点编号重复")
            highlight_ids.add(hid)
            highlight_blocks[hid] = identifier
            hs, he = _range(item, "内容重点")
            if not start <= hs < he <= end:
                raise ValueError("内容重点时间必须在所属内容段内")
            highlight = {"highlightId": hid, "idx": _integer(item.get("idx"), "内容重点序号", MAX_HIGHLIGHTS),
                         "text": _text(item.get("text"), "内容重点", 12000), "startMs": hs, "endMs": he,
                         "keyframes": []}
            frames = item.get("keyframes")
            if frames is None:
                frames = []
            frames = list(_items(frames, "关键帧", 100))
            if item.get("keyframe") is not None:
                frames.insert(0, item["keyframe"])
            seen_frames = set()
            for frame in frames:
                frame = _keyframe(frame, (start, end))
                encoded = _encoded(frame)
                if encoded not in seen_frames:
                    highlight["keyframes"].append(frame)
                    seen_frames.add(encoded)
            block["highlights"].append(highlight)
        result["contentBlocks"].append(block)
    mindmap = _object(payload.get("mindmap", {"nodes": [], "edges": []}), "思维导图")
    nodes = _items(mindmap.get("nodes"), "导图节点", MAX_NODES)
    edges = _items(mindmap.get("edges"), "导图连线", MAX_NODES*3)
    node_ids = set()
    for node in nodes:
        node = _object(node, "导图节点")
        nid = _text(node.get("id"), "导图节点编号", 100)
        if nid in node_ids:
            raise ValueError("导图节点编号重复")
        node_ids.add(nid)
        data = _object(node.get("data", {}), "导图节点数据")
        if "targetBlockId" not in data and node.get("blockId") is not None:
            data = {**data, "targetBlockId": node["blockId"]}
        clean_data = {}
        if data.get("targetBlockId") is not None:
            target = _text(data["targetBlockId"], "导图内容段引用", 100)
            if target not in block_ids:
                raise ValueError("导图引用了不存在的内容段")
            clean_data["targetBlockId"] = target
        if data.get("targetHighlightId") is not None:
            target = _text(data["targetHighlightId"], "导图重点引用", 100)
            if target not in highlight_ids or (clean_data.get("targetBlockId") and highlight_blocks[target] != clean_data["targetBlockId"]):
                raise ValueError("导图引用了不存在或不属于该段的内容重点")
            clean_data["targetHighlightId"] = target
        result["mindmap"]["nodes"].append({"id": nid,
            "type": _text(node.get("type", "topic"), "导图节点类型", 100),
            "label": _text(node.get("label", ""), "导图节点文字", 2000, empty=True),
            "level": _integer(node.get("level", 0), "导图节点层级", 100), "data": clean_data})
    edge_ids = set()
    for edge in edges:
        edge = _object(edge, "导图连线")
        identifier = _text(edge.get("id"), "导图连线编号", 100)
        source, target = edge.get("source"), edge.get("target")
        if identifier in edge_ids or not isinstance(source, str) or not isinstance(target, str) or source not in node_ids or target not in node_ids:
            raise ValueError("导图连线编号重复或端点不存在")
        edge_ids.add(identifier)
        clean_edge = {"id": identifier, "source": source, "target": target}
        if edge.get("label") is not None:
            clean_edge["label"] = _text(edge["label"], "导图连线文字", 1000, empty=True)
        result["mindmap"]["edges"].append(clean_edge)
    assets = _items(payload.get("assetRefs", []), "资源引用", MAX_HIGHLIGHTS)
    asset_ids = set()
    for asset in assets:
        asset = _object(asset, "资源引用")
        identifier = _text(asset.get("assetId"), "资源编号", 200)
        if identifier in asset_ids:
            raise ValueError("资源编号重复")
        asset_ids.add(identifier)
        clean = {"assetId": identifier, "kind": _text(asset.get("kind"), "资源类型", 100)}
        if asset.get("contentUrl") is not None:
            clean["contentUrl"] = _text(asset["contentUrl"], "资源地址", 4096)
        if asset.get("path") is not None:
            clean["path"] = _relative_path(asset["path"])
        result["assetRefs"].append(clean)
    _encoded(result)
    return result


def _read_json(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_BYTES+1)
    if len(raw) > MAX_BYTES:
        raise ValueError("分析文件不能超过 16 MiB")
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("分析文件不是有效的 UTF-8 JSON") from exc


def read_result(path):
    """Read the one JSON explicitly selected by the user, never linked files."""
    return normalize_result(_read_json(path))


def _safe_child(root, value):
    value = Path(value)
    path = (value if value.is_absolute() else root/value).resolve()
    if not path.is_relative_to(root):
        raise ValueError("参考文件必须位于当前工程内")
    return path


def _validate_local_assets(root, result):
    assets = list(result["assetRefs"])
    assets.extend(frame for block in result["contentBlocks"] for highlight in block["highlights"] for frame in highlight["keyframes"])
    for asset in assets:
        if asset.get("path"):
            _safe_child(root, asset["path"])


def _source(value):
    if isinstance(value, str):
        return _text(value, "参考来源", 4096, empty=True)
    value = _object(value, "参考来源")
    if len(value) > 30:
        raise ValueError("参考来源字段过多")
    return {_text(key, "来源字段", 100): _text(content, "来源内容", 4096, empty=True)
            for key, content in value.items()}


def save_reference(root, movie_id, payload, *, source="", report=lambda message: None):
    """Persist an explicitly bound, unreviewed reference; never enqueue training."""
    from cache_storage import storage_lease
    root = Path(root).resolve()
    if type(movie_id) is not int or movie_id <= 0 or not (root/"annotations.sqlite").is_file():
        raise ValueError("请先选择工程中已有的影片")
    result = normalize_result(payload)
    source = _source(source)
    _validate_local_assets(root, result)
    with storage_lease(root):
        with dataset.connect(root) as db:
            movie = db.execute("SELECT id,name,digest FROM movies WHERE id=?", (movie_id,)).fetchone()
        if movie is None:
            raise ValueError("目标影片不存在")
        digest = hashlib.sha256(_encoded(result)).hexdigest()
        relative = f"video_analysis/references/{movie_id}-{digest}.json"
        target = _safe_child(root, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        note = {"version": 1, "format": FORMAT, "movie_id": movie_id, "movie_title": movie["name"],
                "movie_digest": movie["digest"], "source": source, "source_hash": digest,
                "created_at": datetime.now(timezone.utc).isoformat(), "review_status": "unreviewed",
                "path": relative, "result": result,
                "warning": "以下内容是机器分析摘要，尚未审核，不是原始台词或已确认训练标签。"}
        report("正在保存带时间定位的分析参考…")
        _atomic_json(target, note)
    report(f"已保存 {len(result['contentBlocks'])} 个内容段；等待查看与核对")
    return note


def _atomic_json(path, value):
    raw = _encoded(value)
    temporary = path.with_name(uuid.uuid4().hex+".tmp")
    try:
        temporary.write_bytes(raw)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def load_reference(root, path):
    root = Path(root).resolve()
    path = _safe_child(root, path)
    folder = _safe_child(root, "video_analysis/references")
    if not path.is_relative_to(folder) or path.suffix.lower() != ".json":
        raise ValueError("请选择当前工程中的分析参考记录")
    note = _object(_read_json(path), "参考记录")
    if type(note.get("version")) is not int or note["version"] != 1 or note.get("format") != FORMAT or note.get("review_status") != "unreviewed":
        raise ValueError("参考记录格式无效")
    result = normalize_result(note.get("result"))
    digest = hashlib.sha256(_encoded(result)).hexdigest()
    if note.get("source_hash") != digest:
        raise ValueError("分析参考内容校验失败")
    _validate_local_assets(root, result)
    movie_id = _integer(note.get("movie_id"), "绑定影片", 2**63-1)
    if not movie_id:
        raise ValueError("绑定影片无效")
    return {"version": 1, "format": FORMAT, "movie_id": movie_id,
            "movie_title": _text(note.get("movie_title"), "影片名", 1000),
            "movie_digest": _text(note.get("movie_digest"), "影片摘要", 200),
            "source": _source(note.get("source", "")), "source_hash": digest,
            "created_at": _text(note.get("created_at"), "保存时间", 100),
            "review_status": "unreviewed", "path": path.relative_to(root).as_posix(), "result": result,
            "warning": "以下内容是机器分析摘要，尚未审核，不是原始台词或已确认训练标签。"}


def list_references(root, movie_id=None):
    root = Path(root).resolve()
    folder = _safe_child(root, "video_analysis/references")
    if not folder.exists():
        return []
    notes = []
    for path in sorted(folder.glob("*.json"))[:1000]:
        try:
            note = load_reference(root, path)
            if movie_id is None or note["movie_id"] == movie_id:
                notes.append(note)
        except (ValueError, OSError):
            continue
    return sorted(notes, key=lambda item: item["created_at"], reverse=True)


def to_storyboard(note, *, block_ids=None):
    """Turn chosen summaries into clearly marked narration drafts for review."""
    from story_pipeline import MAX_SHOTS, MAX_TEXT, validate_storyboard
    note = _object(note, "分析参考")
    result = normalize_result(note.get("result"))
    if note.get("review_status") != "unreviewed":
        raise ValueError("请选择尚待审核的分析参考")
    wanted = None if block_ids is None else set(block_ids)
    existing = {block["blockId"] for block in result["contentBlocks"]}
    if wanted is not None and (not wanted or not wanted <= existing):
        raise ValueError("请选择有效的分析内容段")
    shots, total = [], 0
    for block in result["contentBlocks"]:
        if wanted is not None and block["blockId"] not in wanted:
            continue
        highlights = block["highlights"] or [{"text": block.get("summary") or block["title"] or "待补充分析说明",
                                              "startMs": block["startMs"], "endMs": block["endMs"]}]
        for highlight in highlights:
            text = highlight["text"]
            for begin in range(0, len(text), 1800):
                part = "【分析摘要，非原台词】"+text[begin:begin+1800]
                total += len(part)
                if total > MAX_TEXT or len(shots) >= MAX_SHOTS:
                    raise ValueError("分析内容过长，请选择部分内容段再制作草稿")
                shots.append({"id": f"analysis-{len(shots)+1}", "scene": block["title"] or "分析参考",
                    "speaker": "", "actor": "", "text": part,
                    "action": "idle", "emotion": "neutral", "camera": "wide",
                    "duration": round(min(15., max(2., len(part)/5.)), 3),
                    "source_start_ms": highlight["startMs"], "source_end_ms": highlight["endMs"],
                    "source_kind": "analysis-summary", "role_evidence": "",
                    "direction": "机器分析摘要的旁白草稿；请核对场景、动作、表情并改写。显示时长按文字长度建议，不代表原片时长。"})
    board = {"version": 1, "title": ("分析参考 · "+str(note.get("movie_title", "影片")))[:200],
             "characters": [], "shots": shots, "source_format": "analysis-summary",
             "source_hash": _text(note.get("source_hash"), "参考摘要", 200),
             "provenance": {"kind": "video-analysis-reference", "review_status": "unreviewed",
                            "text_kind": "analysis-summary", "is_original_dialogue": False,
                            "movie_id": note.get("movie_id"), "source_hash": note.get("source_hash"),
                            "reference_path": note.get("path", ""), "warning": "摘要不是原始台词，尚未确认学习"}}
    return validate_storyboard(board)
