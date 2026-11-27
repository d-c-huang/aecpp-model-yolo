"""Import timed Video Helper transcription data without running its services.

The JSON contract was checked against LDJ-creat/video-helper commit
ab8684a80675ce24b9c1cee07f8cbd10448b640f. This module uses the public data
format; it contains no upstream implementation code.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

import dataset
import subtitles


MAX_BYTES = 16 * 1024 * 1024
MAX_SEGMENTS = 100_000
MAX_MS = 2**63 - 1


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_MS


def _valid_text(value):
    return isinstance(value, str) and not any(
        0xD800 <= ord(char) <= 0xDFFF or (ord(char) < 32 and char not in "\n\r\t") for char in value)


def _read_transcript(path):
    if not path.is_file():
        raise ValueError("请选择 Video Helper 生成的 transcript.json 文件")
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("转写文件不能超过 16 MiB")
    try:
        content = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError("转写文件不是有效的 UTF-8 JSON") from exc
    if not isinstance(content, dict):
        raise ValueError("转写内容必须是 JSON 对象")
    if "segments" not in content and "contentBlocks" in content:
        raise ValueError("内容重点 contentBlocks 是分析参考，不能作为逐句字幕导入；请选择 transcript.json")
    if not _integer(content.get("version")) or content["version"] != 1 or content.get("unit") != "ms":
        raise ValueError("仅支持 version=1、unit=ms 的 Video Helper 转写")
    segments = content.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError("转写文件没有真实的 segments 字幕片段")
    if len(segments) > MAX_SEGMENTS:
        raise ValueError("转写片段不能超过 100000 条")
    cues = []
    for index, row in enumerate(segments, 1):
        if not isinstance(row, dict):
            raise ValueError(f"第 {index} 段不是有效字幕对象")
        start, end, text = row.get("startMs"), row.get("endMs"), row.get("text")
        if not _integer(start) or not _integer(end) or end <= start:
            raise ValueError(f"第 {index} 段时间码须为非负整数毫秒，结束时间必须晚于开始时间")
        if not _valid_text(text) or not text.strip():
            raise ValueError(f"第 {index} 段缺少有效字幕文字")
        text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
        cues.append((start, end, text))
    duration = content.get("durationMs")
    if duration is not None and (not _integer(duration) or duration < max(cue[1] for cue in cues)):
        raise ValueError("durationMs 必须是覆盖全部字幕片段的整数毫秒")
    provider = content.get("provider", "unknown")
    language = content.get("language")
    if not _valid_text(provider) or len(provider) > 256 or (language is not None and (not _valid_text(language) or len(language) > 64)):
        raise ValueError("转写 provider 或 language 字段无效")
    return raw, sorted(set(cues)), provider, language


def _source_ids(path):
    """Only infer IDs from the documented UUID/artifacts/UUID/transcript.json layout."""
    if path.name.lower() != "transcript.json" or path.parent.parent.name != "artifacts":
        return {}
    project, job = path.parent.parent.parent.name, path.parent.name
    try:
        if str(uuid.UUID(project)) != project.lower() or str(uuid.UUID(job)) != job.lower():
            return {}
    except ValueError:
        return {}
    return {"project_id": project, "job_id": job}


def _write_json(path, value):
    temporary = path.with_name(uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def import_transcript(root, movie_id, path, report=lambda message: None):
    """Import under the project storage lease so migration cannot split artifacts."""
    from cache_storage import storage_lease
    root = Path(root).resolve()
    if not (root / "annotations.sqlite").is_file():
        raise ValueError("请先选择工程中已有的影片，再导入对应转写")
    with storage_lease(root):
        return _import_transcript(root, movie_id, path, report)


def _import_transcript(root, movie_id, path, report):
    """Bind the explicitly selected transcript to one existing movie.

    Returns cue_count, integer track ID, source key, sha256, provider/language,
    absolute import_path (SRT) and record_path (provenance JSON). Existing tracks
    remain separate; reimporting this track retains matching manual corrections.
    No other source files, remote URLs, databases, or services are accessed.
    """
    root, path = Path(root).resolve(), Path(path).resolve()
    if not _integer(movie_id) or movie_id == 0 or not (root / "annotations.sqlite").is_file():
        raise ValueError("请先选择工程中已有的影片，再导入对应转写")
    with dataset.connect(root) as db:
        movie = db.execute("SELECT id,name,source_path FROM movies WHERE id=?", (movie_id,)).fetchone()
    if movie is None:
        raise ValueError("目标影片不存在，请重新选择影片")
    if not movie["source_path"] or not Path(movie["source_path"]).is_file():
        raise ValueError("目标影片原片缺失，请恢复原片后再导入转写")
    report("正在校验 Video Helper 转写文件与真实时间码…")
    raw, cues, provider, language = _read_transcript(path)
    digest = hashlib.sha256(raw).hexdigest()
    source = f"video-helper:{movie_id}"
    destination = (root / "auto_learning" / "imports").resolve()
    if not destination.is_relative_to(root):
        raise ValueError("转写导入目录必须位于当前工程内")
    destination.mkdir(parents=True, exist_ok=True)
    stem = f"video-helper-{movie_id}-{digest}"
    output, record = destination / (stem + ".srt"), destination / (stem + ".json")
    metadata = {"version": 1, "format": "video-helper-transcript", "movie_id": movie_id,
                "movie_title": movie["name"], "source_path": str(path), "sha256": digest,
                "provider": provider, "language": language, "source": source, "cue_count": len(cues),
                "binding": "explicit-movie-selection", "unit": "ms", **_source_ids(path),
                "imported_at": datetime.now(timezone.utc).isoformat(), "import_path": str(output)}
    # Check artifact writing before replacing the indexed track. This file records
    # the attempted import until the final write includes its committed track ID.
    _write_json(record, {**metadata, "status": "importing"})
    report(f"正在导入 {len(cues)} 条真实字幕片段，保留其他字幕轨与人工修正…")
    subtitles.store_track(root, movie_id, source, "Video Helper 转写", digest, cues)
    with dataset.connect(root) as db:
        track = db.execute("SELECT id FROM subtitle_tracks WHERE movie=? AND source=?", (movie_id, source)).fetchone()[0]
    subtitles.export_srt(root, track, output)
    result = {**metadata, "track": track, "record_path": str(record), "status": "imported"}
    _write_json(record, result)
    report(f"已导入 {len(cues)} 条转写字幕，可按这些真实时间码重新分析字幕节奏。")
    return result
