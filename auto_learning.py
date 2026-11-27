"""Offline video reference analysis and bounded subtitle-timing learning.

Automatic observations are not reviewed directing preferences. Only actual timed
text trains this module's duration model; actions and expressions in its editable
boards remain rule suggestions. Nothing here writes story_learning/director.json.
"""
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import bisect
import hashlib
import json
import math
from pathlib import Path
import queue
import re
import sqlite3
import threading
import unicodedata
import uuid

from dataset import connect
from media import open_movie
from movie_sequence_data import snapshot_sources, _check_source
from online_jobs import _ProjectLock


VERSION = 1
MODEL_FORMAT = "yingge-auto-timing"
PENDING = {"queued", "running", "paused"}
WARNING = "动作、表情与景别为规则建议；仅字幕显示时长来自时间码，尚未经人工确认。"
MAX_RECORDS = 100000


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _chars(text):
    return sum(char.isalnum() for char in unicodedata.normalize("NFKC", str(text)))


def _clean_text(value):
    value = re.sub(r"<[^>]{0,300}>", "", str(value))
    value = re.sub(r"\{\\[^}]*\}", "", value)
    return " ".join(value.replace("\x00", "").split()).strip()


def _baseline(text):
    return min(20., max(2., len(text)/4.5 + .8))


def _estimate(coefficients, text):
    return min(20., max(.5, coefficients["seconds_per_character"] * _chars(text) + coefficients["intercept"]))


def _bounded_estimate(coefficients, text):
    base = _baseline(text)
    return round(min(base*1.4, max(base*.6, _estimate(coefficients, text))), 3)


def _fit(records):
    """Weighted ridge fit: every source film has equal total weight."""
    counts = defaultdict(int)
    for row in records:
        counts[row["film_hash"]] += 1
    xx, x, mass, xy, y = .5, 0., .5, .5/4.5, .5*.8
    for row in records:
        weight = 1/counts[row["film_hash"]]
        length, seconds = _chars(row["text"]), row["duration"]
        xx += weight*length*length
        x += weight*length
        mass += weight
        xy += weight*length*seconds
        y += weight*seconds
    denominator = xx*mass-x*x
    slope = (xy*mass-x*y)/denominator if denominator > 1e-9 else 1/4.5
    intercept = (xx*y-x*xy)/denominator if denominator > 1e-9 else .8
    return {"seconds_per_character": round(min(1., max(.04, slope)), 8),
            "intercept": round(min(3., max(0., intercept)), 8)}


def build_reference(analyses):
    """Fit observed text display lengths, keeping explicit validation films out."""
    by_hash = {}
    for analysis in analyses:
        if analysis.get("version") != VERSION:
            continue
        for row in analysis.get("timing_examples", []):
            key = (analysis["film_hash"], row["start_ms"], row["end_ms"], row["text"])
            item = {**row, "film_hash": analysis["film_hash"], "movie_id": analysis["movie_id"],
                    "split": "val" if analysis["mode"] == "val" else "train"}
            # Any explicit validation copy wins over an accidentally repeated
            # training copy of the same source content.
            if key not in by_hash or item["split"] == "val":
                by_hash[key] = item
    rows = list(by_hash.values())
    validation_hashes = {row["film_hash"] for row in rows if row["split"] == "val"}
    training = [row for row in rows if row["film_hash"] not in validation_hashes]
    external = [row for row in rows if row["film_hash"] in validation_hashes]
    stories = sorted({row["film_hash"] for row in training})
    test, fitting = external, training
    kind = "independent-films" if external else "none"
    if not test and len(stories) >= 2:
        held = set(stories[::5] if len(stories) >= 5 else stories[-1:])
        test = [row for row in training if row["film_hash"] in held]
        fitting = [row for row in training if row["film_hash"] not in held]
        kind = "held-out-films"
    validation = {"kind": "none", "sample_count": 0, "movie_count": 0,
                  "mae_seconds": None, "baseline_mae_seconds": None, "improved": None,
                  "note": "独立影片不足；当前仅积累字幕显示节奏参考，不代表动作或剧情理解。"}
    if fitting and test:
        candidate = _fit(fitting)
        error = sum(abs(_bounded_estimate(candidate, row["text"])-row["duration"]) for row in test)/len(test)
        baseline_error = sum(abs(_baseline(row["text"])-row["duration"]) for row in test)/len(test)
        validation = {"kind": kind, "sample_count": len(test),
                      "movie_count": len({row["film_hash"] for row in test}),
                      "mae_seconds": round(error, 6), "baseline_mae_seconds": round(baseline_error, 6),
                      "improved": error < baseline_error,
                      "note": "按完整影片留出比较字幕时长误差；最终拟合不包含显式验证影片。"}
    coefficients = _fit(training) if training else {"seconds_per_character": 1/4.5, "intercept": .8}
    usable = len(training) >= 3 and validation["improved"] is not False
    return {"format": MODEL_FORMAT, "version": VERSION, "scope": "subtitle-display-timing-only",
            "source": "automatically-observed", "trained_at": _now(), "usable": usable,
            "example_count": len(training), "movie_count": len(stories),
            "training_movie_ids": sorted({row["movie_id"] for row in training}),
            "validation_movie_ids": sorted({row["movie_id"] for row in test}),
            "explicit_validation_movie_ids": sorted({row["movie_id"] for row in external}),
            "coefficients": coefficients, "validation": validation,
            "note": "只调整字幕显示时长；人工确认的编排优先。" if usable else
                    "有效训练字幕不足，或留出效果未优于规则；生成继续使用原时长。"}


def load_reference(root):
    path = Path(root)/"auto_learning"/"reference.json"
    if not path.exists():
        return None
    try:
        if path.stat().st_size > 1024*1024:
            raise ValueError("自动节奏参考文件过大")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("format") != MODEL_FORMAT or value.get("version") != VERSION:
            raise ValueError("自动节奏参考格式不兼容")
        if type(value.get("usable")) is not bool:
            raise ValueError("自动节奏参考状态无效")
        for key, low, high in (("seconds_per_character", .04, 1.), ("intercept", 0., 3.)):
            number = value["coefficients"][key]
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or not low <= number <= high:
                raise ValueError("自动节奏参考系数无效")
        return value
    except (json.JSONDecodeError, UnicodeError, KeyError, TypeError) as exc:
        raise ValueError("自动节奏参考损坏；请重新分析已导入视频") from exc


def predict_reference_duration(reference, text):
    if not reference or reference.get("format") != MODEL_FORMAT or not reference.get("usable") or _chars(text) < 2:
        return None
    # Keep unreviewed references a bounded hint even for out-of-domain text.
    return _bounded_estimate(reference["coefficients"], text)


def _timed_text(movie, duration_ms):
    tracks = defaultdict(list)
    script_by_text = defaultdict(list)
    for row in movie.get("scripts", []):
        script_by_text[_clean_text(row["text"])].append(row)
    for row in movie.get("subtitles", []):
        tracks[row["track_id"]].append(row)
    if tracks:
        def score(rows):
            first = rows[0]
            label = (str(first.get("label", ""))+str(first.get("source", ""))).lower()
            manual = any(word in label for word in ("manual", "人工", "手动"))
            chinese = any("\u3400" <= char <= "\u9fff" for row in rows for char in row["text"])
            return manual, chinese, len(rows)
        selected = max(tracks.values(), key=score)
        source_kind = "subtitle"
    else:
        selected = [row for row in movie.get("scripts", []) if row.get("start_ms") is not None and row.get("end_ms") is not None]
        source_kind = "script"
    clean, seen = [], set()
    for row in sorted(selected, key=lambda value: (value["start_ms"], value["end_ms"])):
        text = _clean_text(row["text"])
        start, end = row["start_ms"], row["end_ms"]
        if (not text or len(text) > 2000 or type(start) is not int or type(end) is not int
                or start < 0 or start >= duration_ms or end <= start or end > duration_ms+250):
            continue
        key = start, end, text
        if key in seen:
            continue
        seen.add(key)
        role, evidence = "", "unknown"
        if source_kind == "script" and row.get("role"):
            role, evidence = row["role"], "timed-script"
        else:
            roles = {entry["role"] for entry in script_by_text[text]
                     if entry.get("role")
                     and (entry.get("start_ms") is None or entry["start_ms"] < end and entry["end_ms"] > start)}
            if len(roles) == 1:
                role, evidence = roles.pop(), "script-match"
        if len(role) > 80:
            role, evidence = "", "unknown"
        clean.append({"text": text, "start_ms": start, "end_ms": min(end, duration_ms),
                      "duration": (min(end, duration_ms)-start)/1000, "role": role,
                      "role_evidence": evidence, "source_kind": source_kind})
        if len(clean) > MAX_RECORDS:
            raise ValueError("单片有效字幕超过 100000 条，请按章节导入")
    return clean


class _Interrupted(Exception):
    pass


def _coarse_scan(source,report,check):
    cuts, previous, last_cut, samples = [0], None, -2., 0
    with open_movie(source, lambda _: check()) as video:
        duration_ms = round(video.duration*1000)
        if duration_ms <= 0:raise ValueError("视频时长无效，请重新导入完整视频")
        for timestamp, picture in video.samples(range(math.ceil(video.duration))):
            check()
            histogram = picture.convert('L').resize((64,36)).histogram()
            histogram = [sum(histogram[index:index+16])/2304 for index in range(0,256,16)]
            difference = sum(abs(a-b) for a,b in zip(histogram,previous))/2 if previous else 0.
            if difference>=.55 and timestamp-last_cut>=2:cuts.append(round(timestamp*1000));last_cut=timestamp
            previous=histogram;samples+=1
            if samples%30==0:report('analysing',f'正在分析 · {timestamp:.0f} / {video.duration:.0f} 秒 · {len(cuts)} 个候选镜头')
    return {'duration_ms':duration_ms,'cuts':cuts}


def analyse_movie(root, movie_id, report=lambda phase, message: None, check=lambda: None,*,cache_dir=None,source_hash=''):
    """Read a frozen movie snapshot, observe coarse cuts, and prepare drafts."""
    check()
    snapshot = snapshot_sources(root, [movie_id])
    movie = snapshot["movies"][0]
    source = _check_source(movie)
    report("analysing", "正在读取视频，分析每秒画面变化与字幕时间码…")
    cache=None;observed=None
    if cache_dir:
        import av,PIL
        from video_analysis_cache import AnalysisCache,signature,file_hash
        cache=AnalysisCache(cache_dir)
        key=signature({'kind':'coarse-luma-histogram-1fps-v1','source':source_hash or file_hash(source,lambda:check() or False),
                       'av':av.__version__,'ffmpeg':av.library_versions,'pillow':PIL.__version__})
        observed=cache.get(key)
        if not (isinstance(observed,dict) and type(observed.get('duration_ms')) is int and observed['duration_ms']>0
                and isinstance(observed.get('cuts'),list) and observed['cuts'] and observed['cuts'][0]==0
                and all(type(t) is int and 0<=t<observed['duration_ms'] for t in observed['cuts'])
                and observed['cuts']==sorted(set(observed['cuts']))):observed=None
    scan_cached=observed is not None
    if scan_cached:report('analysing','已复用校验缓存：每秒画面变化；字幕与人工校正仍重新读取')
    else:
        observed=_coarse_scan(source,report,check);check();_check_source(movie)
        if cache:
            try:cache.put(key,observed)
            except (OSError,ValueError):pass
    duration_ms,cuts=observed['duration_ms'],observed['cuts']
    check()
    _check_source(movie)
    cues = _timed_text(movie, duration_ms)
    examples, prior_end = [], -1
    for index, row in enumerate(cues):
        check()
        length, seconds = _chars(row["text"]), row["duration"]
        following = cues[index+1]["start_ms"] if index+1 < len(cues) else duration_ms+1
        # Overlap, credits, very short flashes and implausible long holds are not
        # useful duration supervision. They remain visible in editable drafts.
        if (2 <= length <= 120 and .5 <= seconds <= 20 and .5 <= length/seconds <= 20
                and row["start_ms"] >= prior_end and row["end_ms"] <= following):
            examples.append({key: row[key] for key in ("text", "start_ms", "end_ms", "duration", "source_kind")})
        prior_end = max(prior_end, row["end_ms"])
    from story_pipeline import generate_storyboard
    parts, group, size = [], [], 0
    for row in cues:
        # generate_storyboard can split long captions, so budget by character
        # count as well as caption count before creating each independent board.
        if group and (len(group) >= 600 or size+len(row["text"]) > 50000):
            parts.append(group)
            group, size = [], 0
        group.append(row)
        size += len(row["text"])
    if group:
        parts.append(group)
    boards = []
    for part, rows in enumerate(parts, 1):
        check()
        characters, role_ids, shots, source_lines = [], {}, [], []
        for row in rows:
            check()
            role = row["role"]
            if role and role not in role_ids and len(role_ids) < 100:
                key = f"character-{len(role_ids)+1}"
                role_ids[role] = key
                characters.append({"id": key, "name": role})
            speaker = role_ids.get(role, "")
            # Use narration to keep colon-bearing text from inventing actors.
            draft = generate_storyboard(row["text"], source_format="novel")
            chunks = draft["shots"]
            total = sum(len(item["text"]) for item in chunks) or 1
            for item in chunks:
                if len(shots) >= 4500:
                    raise ValueError("单个参考片段分镜过多，请拆分长字幕后重新分析")
                shot = {**item, "id": f"shot-{len(shots)+1}", "speaker": speaker, "actor": speaker,
                        "scene": f"候选镜头 {bisect.bisect_right(cuts, row['start_ms'])}",
                        "duration": round(min(60., max(.5, row["duration"]*len(item["text"])/total)), 3),
                        "source_start_ms": row["start_ms"], "source_end_ms": row["end_ms"],
                        "source_kind": row["source_kind"], "role_evidence": row["role_evidence"]}
                if speaker and shot["action"] == "idle":
                    shot["action"] = "talk"
                shots.append(shot)
            source_lines.append((role+"：" if role else "")+row["text"])
        boards.append({"version": 1, "title": f"{movie['name'][:160]} · 自动参考 {part}/{len(parts)}",
                       "characters": characters, "shots": shots, "source_text": "\n".join(source_lines),
                       "source_format": "script", "source_hash": _hash(source_lines),
                       "provenance": {"kind": "automatic-video-reference", "movie_id": movie_id,
                                      "source_hash": movie["digest"], "review_status": "unreviewed",
                                      "timing_basis": "subtitle-display", "part": part, "parts": len(parts),
                                      "warning": WARNING}})
    return {"version": VERSION, "movie_id": movie_id, "title": movie["name"], "film_hash": movie["digest"],
            "mode": movie["mode"], "snapshot_hash": snapshot["fingerprint"], "created": _now(),
            "duration_ms": duration_ms, "cut_candidates_ms": cuts, "cut_method": "coarse-luma-histogram-1fps-v1",
            "timing_examples": examples, "cue_count": len(cues), "boards": boards,
            "warning": WARNING, "observations": cues,'scan_cache_hit':scan_cached}


class AutoLearningManager:
    """Single-worker persistent queue, independent of download/GRU state."""

    def __init__(self, root, *, analyser=None):
        self.root = Path(root).resolve()
        self.folder = self.root/"auto_learning"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root/"auto_learning.sqlite"
        self._mutex = threading.RLock()
        self._thread = None
        self._closed = False
        self._current_id = None
        self._events = queue.Queue()
        if analyser is None:
            from video_analysis import analyse_video
            analyser = analyse_video
        self._analyser = analyser
        with self._db(write=True) as db:
            db.execute("CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,movie_id INTEGER UNIQUE NOT NULL,status TEXT NOT NULL,data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO meta VALUES('paused','0')")
        try:
            lock = self._project_lock()
        except (OSError, ValueError):
            lock = None
        if lock is not None and lock.fd is not None:
            try:
                with self._db(write=True) as db:
                    unfinished = [json.loads(row[0]) for row in db.execute("SELECT data FROM jobs WHERE status IN ('queued','running')")]
                    if unfinished:
                        db.execute("UPDATE meta SET value='1' WHERE key='paused'")
                    for job in unfinished:
                        job.update(status="paused", phase="paused", control=None, message="上次分析未完成；点击继续自动学习恢复")
                        self._save(db, job)
            finally:
                lock.close()

    @contextmanager
    def _db(self, *, write=False):
        db = sqlite3.connect(self.db_path, timeout=5)
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _project_lock(self):
        return _ProjectLock(self.folder/".worker.lock")

    @staticmethod
    def _get(db, job_id):
        row = db.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _save(db, job):
        job["updated"] = _now()
        db.execute("INSERT INTO jobs VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,data=excluded.data",
                   (job["id"], job["movie_id"], job["status"], json.dumps(job, ensure_ascii=False, allow_nan=False)))

    def _visible(self, job):
        result = dict(job)
        paths = []
        for value in job.get("board_paths", []):
            path = (self.root/value).resolve()
            if not Path(value).is_absolute() and path.is_relative_to(self.root) and path.suffix == ".json":
                paths.append(str(path))
        result["board_paths"] = paths
        result["board_path"] = result["board_paths"][0] if result["board_paths"] else None
        return result

    def _emit(self, job):
        self._events.put({"type": job["status"], **self._visible(job)})

    @property
    def busy(self):
        return self._thread is not None and self._thread.is_alive()

    @property
    def paused(self):
        with self._db() as db:
            return db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] == "1"

    @property
    def has_pending(self):
        with self._db() as db:
            return db.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running','paused') LIMIT 1").fetchone() is not None

    def list_jobs(self):
        with self._db() as db:
            return [self._visible(json.loads(row[0])) for row in db.execute("SELECT data FROM jobs ORDER BY rowid DESC")]

    def drain_events(self):
        events = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                return events

    def _ensure_open(self):
        if self._closed:
            raise RuntimeError("自动学习队列已关闭")

    def enqueue(self, movie_ids, *, force=False):
        self._ensure_open()
        movie_ids = list(dict.fromkeys(int(value) for value in movie_ids))
        if not movie_ids:
            return []
        with connect(self.root) as source:
            names = {row["id"]: row["name"] for row in source.execute("SELECT id,name FROM movies")}
        if any(movie_id not in names for movie_id in movie_ids):
            raise ValueError("所选视频已不存在，请刷新影片列表")
        added = []
        with self._mutex, self._db(write=True) as db:
            paused = db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] == "1"
            for movie_id in movie_ids:
                stored = db.execute("SELECT data FROM jobs WHERE movie_id=?", (movie_id,)).fetchone()
                job = json.loads(stored[0]) if stored else None
                if job and (not force or job["status"] in {"running", "queued", "paused"}):
                    continue
                # Older successful jobs predate published_result. Preserve
                # their observed summary before a retry resets live counters;
                # a current movie digest cannot authenticate that old analysis.
                if job and "published_result" not in job and job.get("status") in {"completed", "partial"}:
                    understanding = job.get("model_analysis")
                    understanding = understanding if isinstance(understanding, dict) else {}
                    examples = job.get("example_count", 0)
                    examples = examples if type(examples) is int and examples > 0 else 0
                    clips = understanding.get("visual_clip_count", 0)
                    clips = clips if type(clips) is int and clips > 0 and understanding.get("visual_status") == "completed" else 0
                    if examples or clips:
                        job["published_result"] = {
                            "status": job["status"], "film_hash": job.get("film_hash", ""),
                            "example_count": examples, "mode": job.get("mode", ""),
                            "published_at": job.get("updated") or job.get("created") or "",
                            "model_analysis": {key: understanding.get(key) for key in (
                                "semantic_status", "visual_status", "visual_clip_count", "visual_frame_count")},
                        }
                job = job or {"id": uuid.uuid4().hex, "movie_id": movie_id, "created": _now(), "attempts": 0}
                status = "paused" if paused else "queued"
                job.update(title=names[movie_id], status=status, phase=status,
                           message="等待继续自动学习" if paused else "等待自动分析视频参考", error="", control=None,
                           example_count=0, board_paths=[])
                self._save(db, job)
                self._emit(job)
                added.append(job["id"])
        self._start()
        return added

    def _start(self):
        with self._mutex:
            if self._closed or self.busy or self.paused:
                return
            with self._db() as db:
                if not db.execute("SELECT 1 FROM jobs WHERE status='queued' LIMIT 1").fetchone():
                    return
            try:
                lock = self._project_lock()
            except (OSError, ValueError):
                return
            if lock.fd is None:
                return
            self._thread = threading.Thread(target=self._work, args=(lock,), daemon=True, name="auto-reference-learning")
            self._thread.start()

    def wake(self):
        """Retry already-authorized queued work after a storage lock is released."""
        self._start()

    def pause(self):
        self._ensure_open()
        with self._mutex, self._db(write=True) as db:
            db.execute("UPDATE meta SET value='1' WHERE key='paused'")
            for row in list(db.execute("SELECT data FROM jobs WHERE status IN ('queued','running')")):
                job = json.loads(row[0])
                if job["status"] == "running":
                    job.update(control=job.get("control") or "paused", message="正在暂停；已有视频保留")
                else:
                    job.update(status="paused", phase="paused", message="自动学习已暂停")
                self._save(db, job)
                self._emit(job)

    def resume(self):
        self._ensure_open()
        with self._mutex, self._db(write=True) as db:
            db.execute("UPDATE meta SET value='0' WHERE key='paused'")
            for row in list(db.execute("SELECT data FROM jobs WHERE status='paused'")):
                job = json.loads(row[0])
                job.update(status="queued", phase="queued", control=None, error="", message="等待继续自动分析")
                self._save(db, job)
                self._emit(job)
        self._start()

    @staticmethod
    def _ids(ids):
        return [ids] if isinstance(ids, str) else list(dict.fromkeys(str(value) for value in ids))

    def cancel(self, ids):
        self._ensure_open()
        changed = []
        with self._mutex, self._db(write=True) as db:
            for job_id in self._ids(ids):
                job = self._get(db, job_id)
                if not job or job["status"] not in PENDING:
                    continue
                if job["status"] == "running":
                    job.update(control="cancelled", message="正在取消自动分析")
                else:
                    job.update(status="cancelled", phase="cancelled", control=None, message="已取消；视频与历史参考保留")
                self._save(db, job)
                self._emit(job)
                changed.append(job_id)
        return changed

    def retry(self, ids):
        self._ensure_open()
        movie_ids = []
        with self._db() as db:
            for job_id in self._ids(ids):
                job = self._get(db, job_id)
                if job and job["status"] in {"failed", "cancelled", "needs_input", "partial", "completed"}:
                    movie_ids.append(job["movie_id"])
        return self.enqueue(movie_ids, force=True)

    def close(self):
        with self._mutex:
            if self._closed:
                return
            if self.busy:
                self.pause()
            self._closed = True

    def _check(self, job_id):
        with self._db() as db:
            job = self._get(db, job_id)
            paused = db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] == "1"
            if self._closed or paused or not job or job.get("control"):
                raise _Interrupted()

    def _report(self, job_id, phase, message,details=None):
        self._check(job_id)
        with self._mutex, self._db(write=True) as db:
            job = self._get(db, job_id)
            job.update(phase=phase, message=str(message)[:1000])
            if details:
                job['live_visual']=details
                if details.get('observation'):job['last_visual']=details
            self._save(db, job)
            self._emit(job)

    def _publish(self, job, analysis):
        self._check(job["id"])
        self._report(job["id"], "learning", f"正在学习 {len(analysis['timing_examples'])} 条真实时间码的字幕节奏…")
        with connect(self.root) as db:
            current_movies = {row["id"]: dict(row) for row in db.execute("SELECT id,digest,mode FROM movies")}
        current_movie = current_movies.get(analysis["movie_id"])
        if not current_movie or current_movie["digest"] != analysis["film_hash"]:
            raise ValueError("分析期间影片来源已变化，请重新选择视频分析")
        # Group changes apply before fitting. A previously analysed training
        # film moved to validation must never silently remain a training source.
        analysis["mode"] = current_movie["mode"]
        analyses = []
        for path in sorted((self.folder/"analyses").glob("*.json")):
            self._check(job["id"])
            old = json.loads(path.read_text(encoding="utf-8"))
            current_movie = current_movies.get(old["movie_id"])
            if (old["movie_id"] != analysis["movie_id"] and current_movie
                    and current_movie["digest"] == old["film_hash"]):
                analyses.append({**old, "mode": current_movie["mode"]})
        analyses.append({key: value for key, value in analysis.items() if key != "boards"})
        model = build_reference(analyses)
        with self._mutex, self._db(write=True) as db:
            current = self._get(db, job["id"])
            paused = db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] == "1"
            if self._closed or paused or not current or current.get("control"):
                raise _Interrupted()
            paths = []
            for number, board in enumerate(analysis["boards"], 1):
                path = self.folder/"boards"/f"{job['movie_id']}-{analysis['snapshot_hash'][:16]}-{number}.json"
                _write_json(path, board)
                paths.append(path.relative_to(self.root).as_posix())
            stored = {key: value for key, value in analysis.items() if key != "boards"}
            _write_json(self.folder/"analyses"/f"{job['movie_id']}.json", stored)
            _write_json(self.folder/"reference.json", model)
            count = len(analysis["timing_examples"])
            status = "completed" if count else "needs_input"
            detail = (f"已积累 {count} 条字幕时长参考 · {len(paths)} 份待确认分镜" if count else
                      "未找到足够有效的定时文本；请补充字幕或定时剧本后重新分析")
            if analysis["mode"] == "val":
                detail += " · 验证影片仅评估，不参与训练"
            if count and not model["usable"]:
                detail += " · 当前生成仍使用原时长"
            understanding = analysis.get("model_analysis",{})
            cached_visual=(understanding.get('performance') or {}).get('visual',{})
            if cached_visual:
                detail+=f" · 视觉缓存 {cached_visual.get('cache_hits',0)} 段 / 新增推理 {cached_visual.get('model_requests',0)} 次"
            visual_count = understanding.get("visual_clip_count",0)
            if understanding.get("visual_status")=="completed" and visual_count:
                if not count:
                    status = "completed"
                    detail = "无有效定时文本，已完成独立画面分析"
                    if analysis["mode"]=="val":
                        detail += " · 验证影片不参与生成参考"
                detail += (f" · 画面学习 {visual_count} 个片段 / {understanding.get('visual_frame_count',0)} 张连续帧"
                           "（待审核；合格参考用于新故事分镜）")
            if understanding.get("semantic_status")=="completed":
                detail += f" · 内容分析已保存 · {understanding.get('frame_count',0)} 张实际关键帧（待审核）"
            if understanding.get("issues"):
                detail += " · "+"；".join(understanding["issues"])
                if count or visual_count:
                    status = "partial"
            current.update(status=status, phase=status, message=detail, error="", control=None,
                           board_paths=paths, example_count=count, cut_count=len(analysis["cut_candidates_ms"]),
                           validation=model["validation"], reference_usable=model["usable"],
                           snapshot_hash=analysis["snapshot_hash"],model_analysis=understanding,
                           film_hash=analysis["film_hash"],
                           published_result={"status": status, "film_hash": analysis["film_hash"],
                                             "example_count": count, "mode": analysis["mode"],
                                             "published_at": _now(),
                                             "model_analysis": {key: understanding.get(key) for key in (
                                                 "semantic_status", "visual_status", "visual_clip_count", "visual_frame_count")}})
            self._save(db, current)
            self._emit(current)

    def _work(self, lock):
        try:
            while not self._closed:
                with self._mutex, self._db(write=True) as db:
                    if db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] == "1":
                        return
                    row = db.execute("SELECT data FROM jobs WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
                    if not row:
                        return
                    job = json.loads(row[0])
                    self._current_id = job["id"]
                    job.update(status="running", phase="snapshot", control=None, error="",
                               live_visual={},last_visual={},
                               attempts=job["attempts"]+1, message="正在冻结字幕、角色与影片资料…")
                    self._save(db, job)
                    self._emit(job)
                try:
                    def progress(phase,message):self._report(job['id'],phase,message)
                    progress.visual=lambda phase,message,details:self._report(job['id'],phase,message,details)
                    analysis = self._analyser(self.root, job["movie_id"],progress,
                                              lambda: self._check(job["id"]))
                    self._publish(job, analysis)
                    try:
                        from learning_telemetry import record_project_point
                        record_project_point(self.root)
                    except (OSError,ValueError,sqlite3.Error):
                        pass  # A graph write cannot invalidate published learning.
                except Exception as exc:
                    with self._mutex, self._db(write=True) as db:
                        current = self._get(db, job["id"])
                        paused = db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] == "1"
                        interrupted = isinstance(exc, _Interrupted) or self._closed or paused or current.get("control")
                        if interrupted:
                            status = "cancelled" if current.get("control") == "cancelled" else "paused" if paused or self._closed else "queued"
                            message = "已取消；视频和历史学习结果保留" if status == "cancelled" else "已暂停；继续后重新分析此片，已有视频保留"
                            current.update(status=status, phase=status, control=None, error="", message=message)
                        else:
                            message = (str(exc) or type(exc).__name__)[:2000]
                            current.update(status="failed", phase="failed", control=None, error=message, message=message)
                        self._save(db, current)
                        self._emit(current)
                self._current_id = None
        finally:
            with self._mutex:
                self._current_id = None
                # An enqueue can arrive after the empty check while this worker
                # is exiting. Pass the same OS lock to its successor.
                with self._db() as db:
                    pending = db.execute("SELECT 1 FROM jobs WHERE status='queued' LIMIT 1").fetchone()
                    paused = db.execute("SELECT value FROM meta WHERE key='paused'").fetchone()[0] == "1"
                if pending and not paused and not self._closed:
                    self._thread = threading.Thread(target=self._work, args=(lock,), daemon=True, name="auto-reference-learning")
                    self._thread.start()
                else:
                    lock.close()
