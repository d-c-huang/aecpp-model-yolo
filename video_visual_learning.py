"""Traceable, unreviewed visual examples for draft directing suggestions.

This is retrieval of observed examples, not weight training. It never writes the
human-reviewed director and never imports source dialogue or character names.
"""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from pathlib import Path, PurePosixPath
import statistics
import unicodedata
import uuid

FORMAT = "video-visual-reference"
FOLDER = "video_analysis/visual_references"
MAX_BYTES = 8 * 1024 * 1024
MAX_CLIPS = 120
MIN_CONFIDENCE = .75
MIN_SIMILARITY = .6
WARNING = "视觉模型的待审核观察，仅用于生成可编辑建议；尚未经独立标注验证，不代表模型权重训练。"


def _text(value, label, maximum=2000, *, empty=False):
    if (not isinstance(value, str) or len(value) > maximum
            or any(unicodedata.category(char) in {"Cc", "Cs"} and char not in "\n\t\r" for char in value)):
        raise ValueError(f"{label}文本无效")
    value = value.strip()
    if not value and not empty:
        raise ValueError(f"{label}不能为空")
    return value


def _int(value, label, maximum=12*3600*1000):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{label}数值无效")
    return value


def _encoded(value):
    try:
        result = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("视觉参考不是有效 JSON") from exc
    if len(result) > MAX_BYTES:
        raise ValueError("视觉参考过大")
    return result


def _child(root, value):
    value = _text(str(value), "工程相对路径", 1500)
    # Reject Windows drives and backslashes even on POSIX, making exports safe.
    if "\\" in value or ":" in value or PurePosixPath(value).is_absolute() or ".." in PurePosixPath(value).parts:
        raise ValueError("视觉证据必须位于当前工程内")
    child = (root/value).resolve()
    if child == root or not child.is_relative_to(root):
        raise ValueError("视觉证据必须位于当前工程内")
    return child


def _frame(root, value, start, end, *, saved):
    if not isinstance(value, dict):
        raise ValueError("画面证据格式无效")
    time = _int(value.get("time_ms"), "画面时间")
    if not start <= time <= end:
        raise ValueError("画面时间不在片段范围内")
    path = _child(root, value.get("path", ""))
    if not path.is_file() or path.stat().st_size > 8*1024*1024:
        raise ValueError("画面证据文件缺失或过大")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if (saved or "sha256" in value) and value.get("sha256") != digest:
        raise ValueError("画面证据内容已变化")
    return {"time_ms": time, "path": path.relative_to(root).as_posix(), "sha256": digest}


def normalize_visual_reference(root, payload, *, saved=False):
    """Validate observations and real local frame evidence before any use."""
    from story_pipeline import ACTIONS, EMOTIONS, CAMERAS, _metadata
    root = Path(root).resolve()
    if not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] != 1:
        raise ValueError("视觉参考版本无效")
    movie_id = _int(payload.get("movie_id"), "影片编号", 2**63-1)
    if not movie_id:
        raise ValueError("影片编号无效")
    result = {"version": 1, "movie_id": movie_id,
              "film_hash": _text(payload.get("film_hash"), "原片摘要", 200),
              "model": _text(payload.get("model"), "视觉模型", 200),
              "provider": _text(payload.get("provider"), "模型服务", 100),
              "rhythm_method": _text(payload.get("rhythm_method", "luma-cut-candidates"), "节奏方法", 200),
              "coverage": _metadata(payload.get("coverage", {}), "画面抽样范围")}
    clips = payload.get("clips")
    if not isinstance(clips, list) or len(clips) > MAX_CLIPS:
        raise ValueError("视觉片段数量无效")
    clean, ids = [], set()
    for row in clips:
        if not isinstance(row, dict):
            raise ValueError("视觉片段格式无效")
        key = _text(row.get("id"), "片段编号", 100)
        if key in ids:
            raise ValueError("视觉片段编号重复")
        ids.add(key)
        start, end = _int(row.get("start_ms"), "片段起点"), _int(row.get("end_ms"), "片段终点")
        if start >= end:
            raise ValueError("视觉片段范围无效")
        confidence = row.get("confidence")
        if type(confidence) not in (float, int) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ValueError("视觉置信度无效")
        item = {"id": key, "start_ms": start, "end_ms": end,
                "text": _text(row.get("text", ""), "画面描述", empty=True),
                "context_text": _text(row.get("context_text", ""), "字幕上下文", empty=True),
                "confidence": float(confidence), "evidence": _text(row.get("evidence", ""), "视觉依据", empty=True)}
        for field, allowed in (("action", ACTIONS), ("emotion", EMOTIONS), ("camera", CAMERAS)):
            if row.get(field) not in (*allowed, "unknown"):
                raise ValueError(f"视觉{field}标签无效")
            item[field] = row[field]
        frames = row.get("frames")
        if not isinstance(frames, list) or not 2 <= len(frames) <= 12:
            raise ValueError("视觉片段需要 2–12 张连续画面")
        item["frames"] = [_frame(root, frame, start, end, saved=saved) for frame in frames]
        times = [frame["time_ms"] for frame in item["frames"]]
        if times != sorted(set(times)) or len({frame["path"] for frame in item["frames"]}) != len(frames):
            raise ValueError("画面证据必须按时间排列且不能重复")
        cuts = row.get("cut_times_ms", [])
        if not isinstance(cuts, list) or len(cuts) > 1000:
            raise ValueError("候选切镜数据无效")
        item["cut_times_ms"] = [_int(cut, "候选切镜时间") for cut in cuts]
        if cuts != sorted(set(cuts)) or any(not start < cut < end for cut in cuts):
            raise ValueError("候选切镜须位于片段内部且按时间排列")
        clean.append(item)
    result["clips"] = clean
    if "rhythm" in payload:
        rhythm = payload["rhythm"]
        if not isinstance(rhythm, dict) or rhythm.get("method") != "ffmpeg-scene-difference":
            raise ValueError("全片节奏方法无效")
        duration = _int(rhythm.get("duration_ms"), "全片节奏时长")
        cuts = rhythm.get("cut_times_ms")
        if (not duration or not isinstance(cuts, list) or len(cuts) > 50000
                or any(type(t) is not int or not 0 < t < duration for t in cuts)
                or cuts != sorted(set(cuts)) or any(b-a < 500 for a,b in zip(cuts,cuts[1:]))
                or any(clip["end_ms"] > duration for clip in clean)
                or rhythm.get("sample_fps") != 4 or rhythm.get("threshold") != .28):
            raise ValueError("全片切镜数据无效")
        result["rhythm"] = {"method":rhythm["method"], "duration_ms":duration, "cut_times_ms":cuts,
                            "sample_fps":4, "threshold":.28}
    _encoded(result)
    return result


def save_visual_reference(root, payload):
    from cache_storage import storage_lease
    from dataset import connect
    from movie_sequence_data import _check_source
    root = Path(root).resolve()
    if not (root/"annotations.sqlite").is_file():
        raise ValueError("请先选择已有影片的工程")
    with storage_lease(root):
        clean = normalize_visual_reference(root, payload)
        with connect(root) as db:
            movie = db.execute("SELECT * FROM movies WHERE id=?", (clean["movie_id"],)).fetchone()
        if movie is None or movie["digest"] != clean["film_hash"]:
            raise ValueError("视觉参考的原片来源已变化")
        _check_source(dict(movie))
        digest = hashlib.sha256(_encoded(clean)).hexdigest()
        relative = f"{FOLDER}/{clean['movie_id']}-{digest}.json"
        target = _child(root, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        note = {**clean, "format": FORMAT, "movie_title": movie["name"], "source_hash": digest,
                "created_at": datetime.now(timezone.utc).isoformat(), "review_status": "unreviewed",
                "path": relative, "warning": WARNING}
        temporary = target.with_name(uuid.uuid4().hex+".tmp")
        try:
            temporary.write_bytes(_encoded(note))
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return note


def load_visual_reference(root, path):
    root = Path(root).resolve()
    path = Path(path)
    if path.is_absolute():
        try:
            path = path.resolve().relative_to(root)
        except ValueError as exc:
            raise ValueError("请选择当前工程的视觉参考") from exc
    path = _child(root, path.as_posix())
    if not path.is_relative_to(_child(root, FOLDER)) or path.suffix != ".json" or path.stat().st_size > MAX_BYTES:
        raise ValueError("视觉参考文件无效")
    try:
        note = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError("视觉参考文件损坏") from exc
    if not isinstance(note, dict) or note.get("format") != FORMAT or note.get("review_status") != "unreviewed":
        raise ValueError("视觉参考格式无效")
    clean = normalize_visual_reference(root, note, saved=True)
    digest = hashlib.sha256(_encoded(clean)).hexdigest()
    if note.get("source_hash") != digest:
        raise ValueError("视觉参考完整性校验失败")
    return {**clean, "format": FORMAT, "movie_title": _text(note.get("movie_title"), "影片名", 1000),
            "source_hash": digest, "created_at": _text(note.get("created_at"), "保存时间", 100),
            "review_status": "unreviewed", "path": path.relative_to(root).as_posix(), "warning": WARNING}


def list_visual_references(root, movie_id=None):
    root = Path(root).resolve()
    folder = _child(root, FOLDER)
    if not folder.exists():
        return []
    notes = []
    for path in sorted(folder.glob("*.json"))[:1000]:
        try:
            note = load_visual_reference(root, path)
            if movie_id is None or note["movie_id"] == movie_id:
                notes.append(note)
        except (OSError, ValueError, TypeError):
            continue
    return sorted(notes, key=lambda note: note["created_at"], reverse=True)


def load_visual_library(root):
    """Exclude validation films, stale sources, weak labels and old revisions."""
    from dataset import connect
    from movie_sequence_data import _check_source
    from story_pipeline import _terms, _vector
    root = Path(root).resolve()
    empty = {"examples": [], "example_count": 0, "movie_count": 0, "idf": {}, "warning": ""}
    if not (root/"annotations.sqlite").is_file():
        return empty
    with connect(root) as db:
        movies = {row["id"]: dict(row) for row in db.execute("SELECT * FROM movies")}
    excluded = {movie["digest"] for movie in movies.values() if movie["mode"] == "val"}
    examples, seen = [], set()
    for note in list_visual_references(root):
        movie = movies.get(note["movie_id"])
        if (not movie or movie["id"] in seen or movie["digest"] != note["film_hash"]
                or movie["digest"] in excluded):
            continue
        seen.add(movie["id"])
        try:
            _check_source(movie)
        except (ValueError, OSError):
            continue
        for clip in note["clips"]:
            text = " ".join(value for value in (clip["text"], clip["context_text"]) if value)
            if (clip["confidence"] < MIN_CONFIDENCE or not clip["evidence"]
                    or sum(char.isalnum() for char in text) < 4
                    or all(clip[key] == "unknown" for key in ("action", "emotion", "camera"))):
                continue
            examples.append({**clip, "match_text": text, "movie_id": note["movie_id"],
                             "film_hash": note["film_hash"], "reference_path": note["path"],
                             "source_hash": note["source_hash"], "model": note["model"],
                             "rhythm_method": note["rhythm_method"], "source_rhythm":note.get("rhythm",{})})
            if len(examples) >= 4000:
                break
        if len(examples) >= 4000:
            break
    frequency = Counter()
    for row in examples:
        frequency.update(_terms(row["match_text"]).keys())
    idf = {term: 1.+math.log((1+len(examples))/(1+count)) for term, count in frequency.items()}
    for row in examples:
        row["vector"] = _vector(row["match_text"], idf)
        # A concise genuine subtitle can be more relevant than a long visual description.
        row["context_vector"] = _vector(row["context_text"], idf)
        row["description_vector"] = _vector(row["text"], idf)
        # A short new line should match a relevant sentence, rather than having
        # its signal diluted by the full eight-second multi-line transcript.
        fragments = re.split(r"[\n。！？!?；;]+", row["match_text"])
        row["fragment_vectors"] = [_vector(fragment, idf) for fragment in fragments[:80]
                                   if sum(c.isalnum() for c in fragment) >= 4]
    return {"examples": examples, "idf": idf, "example_count": len(examples),
            "movie_count": len({row["film_hash"] for row in examples}), "warning": ""}


def predict_visual_direction(library, text):
    from story_pipeline import _vector
    if not library or not library.get("examples") or sum(char.isalnum() for char in text) < 4:
        return None
    vector = _vector(text, library["idf"])
    candidates = []
    for row in library["examples"]:
        vectors = [row[key] for key in ("vector", "context_vector", "description_vector")]
        vectors.extend(row.get("fragment_vectors", []))
        score = max(sum(value*other.get(term, 0) for term, value in vector.items()) for other in vectors)
        if score >= MIN_SIMILARITY:
            candidates.append((score, row["confidence"], row))
    if not candidates:
        return None
    score, _, row = max(candidates, key=lambda item: item[:2])
    direction = {key: row[key] for key in ("action", "emotion", "camera") if row[key] != "unknown"}
    cuts = row["cut_times_ms"]
    rhythm_scope, rhythm_method, rhythm_count = "clip", row["rhythm_method"], len(cuts)
    if len(cuts) >= 2:
        direction["rhythm_seconds"] = statistics.median((right-left)/1000 for left, right in zip(cuts, cuts[1:]))
    else:
        rhythm = row.get("source_rhythm", {})
        boundaries = [0, *rhythm.get("cut_times_ms", []), rhythm.get("duration_ms",0)]
        if len(boundaries) >= 4:
            intervals = [(b-a)/1000 for a,b in zip(boundaries,boundaries[1:]) if .5 <= (b-a)/1000 <= 60]
            if len(intervals) >= 3:
                direction["rhythm_seconds"] = statistics.median(intervals)
                rhythm_scope, rhythm_method, rhythm_count = "source-film", rhythm["method"], len(boundaries)-2
    return {"direction": direction, "provenance": {
        "kind": "visual-example-retrieval", "review_status": "unreviewed", "movie_id": row["movie_id"],
        "film_hash": row["film_hash"], "reference_path": row["reference_path"], "source_hash": row["source_hash"],
        "clip_id": row["id"], "start_ms": row["start_ms"], "end_ms": row["end_ms"],
        "confidence": row["confidence"], "similarity": round(score, 6), "model": row["model"],
        "rhythm_method": rhythm_method, "rhythm_scope":rhythm_scope, "rhythm_cut_count":rhythm_count}}


def prepare_matched_rhythm(root, movie_ids, *, report=lambda message:None, check=lambda:None):
    """Upgrade only references actually matched by this story, caching results.

    Older learning records may have sparse windows without a single observed
    edit. Detect source-film rhythm once, retaining the original model labels
    and frames; a bounded failure leaves those references usable.
    """
    from dataset import connect
    from media import open_movie
    from movie_sequence_data import _check_source
    from video_rhythm import analyse_rhythm
    root=Path(root).resolve()
    ids=list(dict.fromkeys(movie_ids))
    if not ids or not (root/"annotations.sqlite").is_file():
        return False, []
    with connect(root) as db:
        movies={row["id"]:dict(row) for row in db.execute("SELECT * FROM movies")}
    changed, warnings=False, []
    for movie_id in ids[:3]:
        check()
        movie=movies.get(movie_id)
        notes=list_visual_references(root,movie_id)
        if not movie or movie["mode"]=="val" or not notes:
            continue
        note=notes[0]
        if note.get("rhythm") or note["film_hash"]!=movie["digest"]:
            continue
        try:
            source=_check_source(movie)
            report("正在补全已匹配参考的镜头节奏 · "+movie["name"])
            with open_movie(source,lambda _:check()) as video:
                duration_ms=round(video.duration*1000)
            rhythm=analyse_rhythm(source,duration_ms=duration_ms,timeout=60,check=check,
                                  report=lambda phase,message:report(message))
            check()
            _check_source(movie)
            save_visual_reference(root,{**note,"rhythm":rhythm})
            changed=True
        except Exception as exc:
            check()
            warning="已保留动作和表情参考，镜头节奏暂未补全："+str(exc)[:500]
            warnings.append(warning)
            report(warning)
    if len(ids)>3:
        warnings.append("本次仅补全前 3 部匹配影片的全片节奏，其他已有动作和表情参考照常使用。")
    return changed,warnings
