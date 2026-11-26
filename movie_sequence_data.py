"""Stream whole films into versioned, reproducible temporal feature sequences.

These measurements describe images, sound energy and subtitle visibility. They do
not label actions, emotion, speech activity or the quality of a joke.
"""
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import uuid

import cv2
import numpy as np

from dataset import PERSON, connect
from media import open_movie
from scripts import CLAUSES, normalized


VERSION = 1
EXTRACTOR_VERSION = 1
FEATURE_NAMES = [
    "luma", "contrast", "saturation", "red", "green", "blue",
    "quadrant_luma_0", "quadrant_luma_1", "quadrant_luma_2", "quadrant_luma_3",
    "motion", "cut_score", "audio_rms", "audio_peak", "audio_available",
    "subtitle_active", "subtitle_chars", "subtitle_available", "confirmed_role_count",
    "annotation_available", "people_count", "people_area", "people_x", "people_y",
    "people_available", "script_role_count", "script_available", "subtitle_density",
    "edge_density", "motion_x", "motion_y", "frame_interval",
]
INDEX = {name: i for i, name in enumerate(FEATURE_NAMES)}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def snapshot_sources(root, movie_ids=None):
    """Freeze source metadata and all applicable human corrections in one read."""
    from movie_profiles import snapshot_from_db
    selected = None if movie_ids is None else {int(value) for value in movie_ids}
    with connect(root) as db:
        db.execute("BEGIN")
        movies = [dict(row) for row in db.execute("SELECT * FROM movies ORDER BY id")
                  if selected is None or row["id"] in selected]
        if selected is not None and selected != {movie["id"] for movie in movies}:
            raise ValueError("所选电影已不存在，请重新选择")
        if not movies:
            raise ValueError("请先导入至少一部电影")
        for movie in movies:
            movie_id = movie["id"]
            profile = snapshot_from_db(db, movie_id)
            movie["profile"] = profile if profile.get("status") == "confirmed" else None
            movie["subtitles"] = [dict(row) for row in db.execute("""
                SELECT t.id AS track_id,t.source,t.label,c.start_ms,c.end_ms,c.text,c.original_text
                FROM effective_subtitle_cues c JOIN subtitle_tracks t ON t.id=c.track
                WHERE t.movie=? ORDER BY c.start_ms,c.end_ms,t.id,c.text""", (movie_id,))]
            movie["scripts"] = [dict(row) for row in db.execute("""
                SELECT s.id AS script_id,l.line_number,l.role,l.text,l.start_ms,l.end_ms
                FROM script_lines l JOIN scripts s ON s.id=l.script
                WHERE s.movie=? ORDER BY s.id,l.line_number""", (movie_id,))]
            movie["frames"] = [dict(row) for row in db.execute("""
                SELECT id,ms,split,boxes,voices FROM frames
                WHERE movie=? AND state='reviewed' AND deleted=0 ORDER BY ms,id""", (movie_id,))]
            movie["tracks"] = [dict(row) for row in db.execute("""
                SELECT t.id,t.role,t.observations FROM tracks t JOIN clips c ON c.id=t.clip
                WHERE c.movie=? AND t.role!='' ORDER BY t.id""", (movie_id,))]
    result = {"version": VERSION, "movies": movies}
    result["fingerprint"] = fingerprint(result)
    return result


def _check_source(movie):
    if not movie.get("source_path"):
        raise ValueError(f"「{movie['name']}」尚未记录原片路径，请重新导入原片")
    source = Path(movie["source_path"])
    try:
        stat = source.stat()
    except OSError as exc:
        raise ValueError(f"找不到「{movie['name']}」原片，请重新导入定位") from exc
    if (stat.st_size, stat.st_mtime_ns) != (movie.get("source_size"), movie.get("source_mtime")):
        raise ValueError(f"「{movie['name']}」原片已变化，请重新导入核对")
    return source


def _weight_key(weights):
    if not weights:
        return None
    path = Path(weights)
    if not path.is_file():
        raise ValueError("人物检测权重不存在，请选择本地权重或关闭人物检测")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _save_json(path, value):
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _save_npz(path, **values):
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("wb") as stream:
            np.savez_compressed(stream, **values)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _visual_features(image, previous, previous_hist):
    rgb = np.asarray(image.convert("RGB").resize((96, 54)), dtype=np.float32) / 255
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    hist = np.histogram(gray, bins=16, range=(0, 1))[0].astype(np.float32)
    hist /= hist.sum()
    features = np.zeros(len(FEATURE_NAMES), dtype=np.float32)
    maximum, minimum = rgb.max(axis=2), rgb.min(axis=2)
    features[:6] = [gray.mean(), gray.std(), np.mean((maximum-minimum)/(maximum+1e-6)),
                    *rgb.mean(axis=(0, 1))]
    features[6:10] = [tile.mean() for row in np.array_split(gray, 2)
                      for tile in np.array_split(row, 2, axis=1)]
    if previous is not None:
        features[INDEX["motion"]] = np.abs(gray-previous).mean()
        features[INDEX["cut_score"]] = np.abs(hist-previous_hist).sum()/2
        flow = cv2.calcOpticalFlowFarneback((previous*255).astype(np.uint8),
                                           (gray*255).astype(np.uint8), None,
                                           .5, 2, 9, 2, 5, 1.2, 0)
        features[INDEX["motion_x"]] = np.median(flow[..., 0])/gray.shape[1]
        features[INDEX["motion_y"]] = np.median(flow[..., 1])/gray.shape[0]
    dx = cv2.Sobel(gray, cv2.CV_32F, 1, 0)
    dy = cv2.Sobel(gray, cv2.CV_32F, 0, 1)
    features[INDEX["edge_density"]] = (np.hypot(dx, dy)>.25).mean()
    return features, gray, hist


def _people_features(model, classes, image, row, device=None):
    result = model.predict(image, conf=.25, classes=classes, imgsz=320, device=device, verbose=False)[0]
    boxes = result.boxes.xyxyn.detach().cpu().numpy()
    row[INDEX["people_available"]] = 1
    row[INDEX["people_count"]] = len(boxes)
    if len(boxes):
        boxes = np.clip(boxes, 0, 1)
        area = np.maximum(0, boxes[:, 2]-boxes[:, 0])*np.maximum(0, boxes[:, 3]-boxes[:, 1])
        row[INDEX["people_area"]] = area.sum()
        row[INDEX["people_x"]] = np.mean((boxes[:, 0]+boxes[:, 2])/2)
        row[INDEX["people_y"]] = np.mean((boxes[:, 1]+boxes[:, 3])/2)


def _audio_features(source, origin, times_ms, duration_ms, features, report):
    """Measure local sound energy, with a mask for bins that contain audio."""
    import av

    sums = np.zeros(len(times_ms), dtype=np.float64)
    counts = np.zeros(len(times_ms), dtype=np.int64)
    peaks = np.zeros(len(times_ms), dtype=np.float32)
    with av.open(str(source)) as container:
        streams = list(container.streams.audio)
        if not streams:
            return
        stream = next((stream for stream in streams if int(stream.disposition)&1), streams[0])
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=8000)
        fallback = float((stream.start_time or 0)*stream.time_base)-origin
        last_report = -1

        def consume(frame):
            nonlocal fallback, last_report
            start = float(frame.pts*frame.time_base)-origin if frame.pts is not None else fallback
            values = frame.to_ndarray().reshape(-1).astype(np.float32)
            fallback = start+len(values)/frame.sample_rate
            millis = (start+np.arange(len(values))/frame.sample_rate)*1000
            indices = np.searchsorted(times_ms, millis, side="right")-1
            valid = (indices>=0) & (indices<len(times_ms)) & (millis<duration_ms)
            indices, values = indices[valid], values[valid]
            if len(indices):
                np.add.at(sums, indices, values.astype(np.float64)**2)
                np.add.at(counts, indices, 1)
                np.maximum.at(peaks, indices, np.abs(values))
            bucket = int(max(0, start)//30)
            if bucket!=last_report:
                report(f"读取电影声音 · {max(0, start):.1f} 秒")
                last_report = bucket

        for frame in container.decode(stream):
            for converted in resampler.resample(frame):
                consume(converted)
        for converted in resampler.resample(None):
            consume(converted)
    present = counts>0
    features[:, INDEX["audio_available"]] = present
    features[present, INDEX["audio_rms"]] = np.sqrt(sums[present]/counts[present])
    features[:, INDEX["audio_peak"]] = peaks


def _extract_raw(root, movie, fps, report, weights, weight_key, model_holder, device=None):
    source = _check_source(movie)
    config = {"extractor_version": EXTRACTOR_VERSION, "film_id": movie["digest"],
              "sample_fps": fps, "weights_sha256": weight_key}
    key = fingerprint(config)
    cache = Path(root)/"movie_features"
    cache.mkdir(parents=True, exist_ok=True)
    marker, arrays = cache/(key+".json"), cache/(key+".npz")
    if marker.is_file() and arrays.is_file():
        try:
            metadata = json.loads(marker.read_text(encoding="utf-8"))
            with np.load(arrays, allow_pickle=False) as stored:
                features, times = stored["features"], stored["times_ms"]
            if (metadata.get("config")==config and features.shape==(len(times), len(FEATURE_NAMES))
                    and len(times) and np.isfinite(features).all() and np.all(np.diff(times)>0)):
                report(f"复用电影画面和声音分析 · {movie['name']}")
                return features, times, metadata
        except (OSError, ValueError, KeyError):
            pass
    if weights and model_holder[0] is None:
        from ultralytics import YOLO
        report("加载本地人物检测权重…")
        model = YOLO(str(weights))
        ids = [key for key, name in model.names.items() if name in PERSON]
        if model.task!="detect" or not ids:
            raise ValueError("电影人物特征需要包含人物类别的目标检测权重")
        model_holder[0] = (model, ids)
    report(f"顺序分析整部电影 · {movie['name']}")
    rows, times = [], []
    previous = previous_hist = None
    with open_movie(source, report) as video:
        origin, duration_ms = float(video.origin), round(video.duration*1000)
        next_time = 0.
        last_decoded = 0.
        for seconds, frame in video.decoded(0):
            last_decoded = seconds
            if seconds+video.interval*.5<next_time:
                continue
            milliseconds = round(seconds*1000)
            if times and milliseconds<=times[-1]:
                continue
            image = video.frame_image(frame)
            row, previous, previous_hist = _visual_features(image, previous, previous_hist)
            if model_holder[0]:
                _people_features(*model_holder[0], image, row, device=device)
            row[INDEX["frame_interval"]] = (milliseconds-times[-1])/1000 if times else 1/fps
            rows.append(row)
            times.append(milliseconds)
            # Jump over absent timestamps, without synthesizing duplicate frames.
            next_time = max(next_time+1/fps, (math.floor(seconds*fps)+1)/fps)
            if len(rows)==1 or len(rows)%max(1, round(fps*15))==0:
                report(f"分析电影画面 · {movie['name']} · {seconds:.1f}/{duration_ms/1000:.1f} 秒")
        if not rows:
            raise ValueError(f"「{movie['name']}」没有可读取的视频帧")
        duration_ms = max(times[-1]+1, round((last_decoded+video.interval)*1000))
    features = np.asarray(rows, dtype=np.float32)
    times = np.asarray(times, dtype=np.int64)
    _audio_features(source, origin, times, duration_ms, features, report)
    _check_source(movie)
    metadata = {"config": config, "duration_ms": duration_ms, "samples": len(times)}
    # The JSON marker is published last. An interrupted extraction is never reusable.
    marker.unlink(missing_ok=True)
    _save_npz(arrays, features=features, times_ms=times)
    _save_json(marker, metadata)
    return features, times, metadata


def _boundary(movie, duration_ms):
    if movie.get("mode") in ("train", "val"):
        return None
    # Match the established screenshot split exactly.
    seconds = max(1, int(duration_ms/1000))
    return max(1, min(seconds-1, round(seconds*.8)))*1000


def _split_at(movie, milliseconds, boundary):
    return movie["mode"] if boundary is None else ("train" if milliseconds<boundary else "val")


def _ranges_at_times(records, times):
    """Yield active interval records with bounded work on long subtitle tracks."""
    records = sorted(records, key=lambda value: value["start_ms"])
    active, cursor = [], 0
    for ms in times:
        active = [record for record in active if record["end_ms"]>ms]
        while cursor<len(records) and records[cursor]["start_ms"]<=ms:
            record = records[cursor]
            if record["end_ms"]>ms:
                active.append(record)
            cursor += 1
        yield active


def _apply_annotations(movie, features, times, fps, duration_ms):
    from movie_context import canonical_role, role_aliases
    aliases = role_aliases(movie.get("profile"))
    role = lambda value: canonical_role(value, aliases)
    features = features.copy()
    captions = movie.get("subtitles", [])
    scripts = movie.get("scripts", [])
    timed_scripts = [row for row in scripts if row["start_ms"] is not None]
    untimed = defaultdict(set)
    for row in scripts:
        if row["start_ms"] is None:
            for text in (row["text"], *CLAUSES.split(row["text"])):
                key = normalized(text)
                if key:
                    untimed[key].add(role(row["role"]))
    features[:, INDEX["subtitle_available"]] = bool(captions)
    for index, (active, timed) in enumerate(zip(_ranges_at_times(captions, times),
                                               _ranges_at_times(timed_scripts, times))):
        texts = {row["text"] for row in active}
        features[index, INDEX["subtitle_active"]] = bool(texts)
        features[index, INDEX["subtitle_chars"]] = sum(len(text) for text in texts)
        features[index, INDEX["subtitle_density"]] = len(texts)
        roles = {role(row["role"]) for row in timed if row["role"]}
        matched = bool(timed)
        for text in texts:
            choices = untimed.get(normalized(text), set())
            if len(choices)==1:
                roles.update(choices)
                matched = True
        features[index, INDEX["script_role_count"]] = len(roles)
        features[index, INDEX["script_available"]] = matched
    boundary = _boundary(movie, duration_ms)
    if boundary is not None:
        # Raw audio bins run to the next sampled image. A sparse sample can
        # straddle the holdout boundary, so withhold its audio measurement.
        ends = np.concatenate((times[1:], np.asarray([duration_ms], dtype=np.int64)))
        crosses = (times<boundary) & (ends>boundary)
        for name in ("audio_rms", "audio_peak", "audio_available"):
            features[crosses, INDEX[name]] = 0
    roles_at = defaultdict(set)
    tolerance = 500/fps
    captions_by_track = defaultdict(list)
    captions_by_source = defaultdict(list)
    for cue in captions:
        captions_by_track[(cue["track_id"], cue["start_ms"], cue["end_ms"])].append(cue)
        captions_by_source[(cue["source"], cue["start_ms"], cue["end_ms"])].append(cue)

    def closest(ms):
        index = int(np.searchsorted(times, ms))
        possible = [i for i in (index-1, index) if 0<=i<len(times)]
        nearest = min(possible, key=lambda i: abs(int(times[i])-ms))
        return nearest if abs(int(times[nearest])-ms)<=tolerance else None

    for frame in movie.get("frames", []):
        index = closest(frame["ms"])
        notes = [box[5] for box in json.loads(frame["boxes"]) if len(box)>5]
        notes.extend(json.loads(frame["voices"]))
        if index is not None and frame["split"]==_split_at(movie, int(times[index]), boundary):
            roles_at[index].update(role(note["role"]) for note in notes if note.get("role"))
            features[index, INDEX["annotation_available"]] = 1
        # A confirmed cue binding remains tied to its source identity after a
        # subtitle text correction. Do not spread it across a held-out boundary.
        for note in notes:
            if not note.get("role"):
                continue
            for ref in note.get("subtitle_refs", []):
                if ref.get("text") not in note.get("lines", []):
                    continue
                start, end = ref.get("start_ms"), ref.get("end_ms")
                if ref.get("track_id") is not None:
                    possible = captions_by_track.get((ref["track_id"], start, end), [])
                elif ref.get("source"):
                    possible = captions_by_source.get((ref["source"], start, end), [])
                else:
                    continue
                text = ref.get("original_text", ref.get("text"))
                matches = [cue for cue in possible if text==cue["original_text"] or
                           ("original_text" not in ref and text==cue["text"])]
                if not matches:
                    continue
                cue = matches[0]
                left, right = np.searchsorted(times, [cue["start_ms"], cue["end_ms"]])
                for at in range(int(left), int(right)):
                    if frame["split"]==_split_at(movie, int(times[at]), boundary):
                        roles_at[at].add(role(note["role"]))
                        features[at, INDEX["annotation_available"]] = 1
    for track in movie.get("tracks", []):
        for point in json.loads(track["observations"]):
            index = closest(point[0])
            if index is not None and _split_at(movie, point[0], boundary)==_split_at(movie, int(times[index]), boundary):
                roles_at[index].add(role(track["role"]))
                features[index, INDEX["annotation_available"]] = 1
    for index, roles in roles_at.items():
        features[index, INDEX["confirmed_role_count"]] = len(roles)
    return features


def prepare_corpus(root, snapshot, run, sample_fps, report, weights=None, device=None):
    """Extract every selected film, then rebuild overlays from the frozen snapshot."""
    import movie_context
    fps = float(sample_fps)
    if not math.isfinite(fps) or not .25<=fps<=4:
        raise ValueError("电影学习采样率须在每秒 0.25–4 帧之间")
    if snapshot.get("version")!=VERSION or not snapshot.get("movies"):
        raise ValueError("电影学习快照为空或版本不支持")
    root, run = Path(root), Path(run)
    run.mkdir(parents=True, exist_ok=True)
    weights_hash = _weight_key(weights)
    features_all, times_all, contexts_all, sequences, movies = [], [], [], [], []
    contextual = any(movie_context.meaningful(movie.get("profile")) for movie in snapshot["movies"])
    offset = 0
    model_holder = [None]
    for movie in snapshot["movies"]:
        raw, times, metadata = _extract_raw(root, movie, fps, report, weights, weights_hash, model_holder, device=device)
        features = _apply_annotations(movie, raw, times, fps, metadata["duration_ms"])
        if contextual:
            contexts_all.append(movie_context.encode_profile(movie.get("profile"), times))
        boundary = _boundary(movie, metadata["duration_ms"])
        start = 0
        while start<len(times):
            start_ms = int(times[start])
            split = _split_at(movie, start_ms, boundary)
            end_ms = min(start_ms+120000, metadata["duration_ms"])
            if boundary is not None and start_ms<boundary:
                end_ms = min(end_ms, boundary)
            end = int(np.searchsorted(times, end_ms, side="left"))
            end = max(start+1, end)
            length = end-start
            sequences.append({"film_id": movie["digest"], "movie_id": movie["id"], "name": movie["name"],
                              "split": split, "start_ms": start_ms, "end_ms": end_ms,
                              "offset": offset+start, "length": length})
            start = end
        movies.append({"film_id": movie["digest"], "movie_id": movie["id"], "name": movie["name"],
                       "duration_ms": metadata["duration_ms"], "samples": len(times),
                       "mode": movie.get("mode"), "audio_available": bool(raw[:, INDEX["audio_available"]].any()),
                       "subtitle_available": bool(movie.get("subtitles")),
                       "profile_confirmed": bool(movie_context.confirmed(movie.get("profile"))),
                       "profile_context_available": movie_context.meaningful(movie.get("profile")),
                       "people_available": bool(raw[:, INDEX["people_available"]].any())})
        features_all.append(features)
        times_all.append(times)
        offset += len(times)
        _check_source(movie)
    train_films = {row["film_id"] for row in sequences if row["split"]=="train"}
    val_films = {row["film_id"] for row in sequences if row["split"]=="val"}
    independent = bool(train_films and val_films and train_films.isdisjoint(val_films))
    warnings = []
    if not independent:
        warnings.append("尚无完全独立的验证电影，片内留出只能衡量同片泛化，不能证明新电影效果")
    if not weights:
        warnings.append("未启用人物检测，人物检测相关特征以缺失掩码表示")
    if not any(movie["subtitle_available"] for movie in movies):
        warnings.append("没有导入字幕，仍学习画面与声音时序，字幕相关特征以缺失掩码表示")
    manifest = {
        "version": VERSION, "extractor_version": EXTRACTOR_VERSION, "feature_names": FEATURE_NAMES,
        "sample_fps": fps, "snapshot": snapshot.get("fingerprint") or fingerprint(snapshot),
        "weights_sha256": weights_hash, "sequences": sequences, "movies": movies,
        "frames": offset, "train_frames": sum(row["length"] for row in sequences if row["split"]=="train"),
        "val_frames": sum(row["length"] for row in sequences if row["split"]=="val"),
        "train_films": len(train_films), "val_films": len(val_films),
        "independent_validation": independent, "warnings": warnings,
        "profile_context": {"version": movie_context.VERSION, "names": movie_context.NAMES,
                            "note": movie_context.NOTE} if contextual else None,
        "profile_count": sum(movie["profile_confirmed"] for movie in movies),
        "scope": "全电影画面、运动、声音能量、字幕显示与已确认角色统计的时序特征；不含动作语义、情绪或幽默标签",
        "feature_notes": {
            "cut_score": "相邻采样画面亮度直方图距离，候选切点分数，未经人工确认",
            "motion": "相邻采样画面的平均绝对亮度差，含相机运动和切镜变化",
            "audio_rms": "音频能量，不代表说话人或真实开口时间",
            "subtitle_active": "字幕显示状态，不代表真实说话状态",
            "script_role_count": "定时剧本或唯一文字匹配得到的角色数量，未确认说话人",
            "confirmed_role_count": "当前采样附近已确认截图及已命名轨迹中的角色数量",
            "people_count": "可选目标检测器检出的数量，受遮挡和检测误差影响",
            "availability": "名称以 available 结尾的维度区分缺少输入与观测为零",
        },
    }
    # A new manifest is the completion marker for the paired corpus arrays.
    marker = run/"movie_corpus.json"
    marker.unlink(missing_ok=True)
    _save_npz(run/"movie_corpus.npz", features=np.concatenate(features_all), times_ms=np.concatenate(times_all),
              profile_context=np.concatenate(contexts_all) if contextual else np.zeros((offset, 0), dtype=np.float32))
    _save_json(marker, manifest)
    report(f"电影时序样本已准备 · {len(movies)} 部电影 · {offset} 帧 · {len(sequences)} 段")
    return manifest
