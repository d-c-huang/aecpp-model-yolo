"""Persistent, serial online imports. All callbacks stay off the Tk thread.

Pause stops the current importer. Resume starts it again with the same options,
letting online_video reuse its verified cache and resumable download fragments.
Removing a record never removes downloaded media, annotations, or training data.
"""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
from urllib.parse import unquote, urlsplit, urlunsplit
import uuid

from online_video import classify_online_error, import_online, validate_url


PENDING = {"queued", "running", "paused"}
AUTOMATIC_LEARNING = {"story-reference", "story-and-movie"}
LOG_LIMIT = 64 * 1024
TAIL_LIMIT = 12 * 1024
EMPTY_ERROR = dict(error_code="", error_category="", restricted=False, skip_reason="", retryable=False)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _identity(url, config):
    parsed = urlsplit(url)
    host = parsed.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    if parsed.port and (parsed.scheme, parsed.port) not in {("http", 80), ("https", 443)}:
        host += ":" + str(parsed.port)
    canonical = urlunsplit((parsed.scheme, host, parsed.path, parsed.query, ""))
    return hashlib.sha256(json.dumps([canonical, config], sort_keys=True).encode("utf-8")).hexdigest()


class _ProjectLock:
    """An advisory lock released by the OS even after an application crash."""

    def __init__(self, path):
        self.fd = None
        fd = os.open(path, os.O_CREAT | os.O_RDWR)
        try:
            if not os.fstat(fd).st_size:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
        else:
            self.fd = fd

    def close(self):
        if self.fd is None:
            return
        fd, self.fd = self.fd, None
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class OnlineJobManager:
    def __init__(self, root, *, importer=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "online_jobs.sqlite"
        self.log_dir = self.root / "logs" / "online"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._importer = importer or import_online
        self._mutex = threading.RLock()
        self._thread = None
        self._worker_active = False
        self._stop = None
        self._current_id = None
        self._closed = False
        self._events = []
        self._updates = {}
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS online_jobs (id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL, status TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL, data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS online_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO online_meta VALUES ('paused','0')")
        lock = self._project_lock()
        if lock.fd is not None:
            try:
                with self._db(write=True) as db:
                    pending = list(db.execute("SELECT data FROM online_jobs WHERE status IN ('queued','running','paused')"))
                    if pending:
                        self._set_paused(db, True)
                    for row in pending:
                        job = json.loads(row[0])
                        cancelled = job.get("control") == "cancelled"
                        status = "cancelled" if cancelled else "paused"
                        job.update(status=status, phase=status, control=None,
                                   message="已取消；已有资源保留" if cancelled else "上次任务尚未完成；点击继续队列恢复，已有下载缓存会保留")
                        self._save(db, job)
                    self._migrate_history(db)
            finally:
                lock.close()

    @contextmanager
    def _db(self, *, write=False):
        db = sqlite3.connect(self.db_path, timeout=1)
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
        return _ProjectLock(self.root / ".online_queue.lock")

    @staticmethod
    def _set_paused(db, value):
        db.execute("INSERT OR REPLACE INTO online_meta VALUES ('paused',?)", ("1" if value else "0",))

    @staticmethod
    def _is_paused(db):
        return db.execute("SELECT value FROM online_meta WHERE key='paused'").fetchone()[0] == "1"

    @staticmethod
    def _get(db, job_id):
        row = db.execute("SELECT data FROM online_jobs WHERE id=?", (str(job_id),)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _save(db, job, *, insert=False):
        job["updated"] = _now()
        payload = json.dumps(job, ensure_ascii=False, default=str, allow_nan=False)
        if insert:
            db.execute("INSERT INTO online_jobs VALUES (?,?,?,?,?,?)", (job["id"], job["fingerprint"], job["status"], job["created"], job["updated"], payload))
        else:
            db.execute("UPDATE online_jobs SET status=?,updated=?,data=? WHERE id=?", (job["status"], job["updated"], payload, job["id"]))

    def _new_job(self, url, config, batch_id, request_version, paused):
        job_id = uuid.uuid4().hex
        parsed = urlsplit(url)
        title = unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1]) or parsed.hostname
        return dict(id=job_id, fingerprint=_identity(url, config), url=url,
                    title=title[:180], status="paused" if paused else "queued",
                    phase="paused" if paused else "queued", progress=None,
                    message="等待继续队列" if paused else "等待后台导入", error="",
                    **EMPTY_ERROR,
                    config=config, result=None, created=_now(), updated=_now(),
                    request_version=request_version, batch_id=batch_id,
                    log_path=str(self.log_dir / (job_id + ".log")), log_tail="",
                    speed=None, eta=None, downloaded_bytes=None, total_bytes=None,
                    attempts=0, control=None, **config)

    def _ensure_open(self):
        if self._closed:
            raise RuntimeError("在线视频任务管理器已关闭")

    @property
    def paused(self):
        with self._db() as db:
            return self._is_paused(db)

    @property
    def busy(self):
        return self._thread is not None and self._thread.is_alive()

    @property
    def has_pending(self):
        with self._db() as db:
            return db.execute("SELECT 1 FROM online_jobs WHERE status IN ('queued','running','paused') LIMIT 1").fetchone() is not None

    def list_jobs(self):
        with self._db() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT data FROM online_jobs ORDER BY created DESC,rowid DESC")]

    @staticmethod
    def _remember_skip(job):
        """Retain the last actual skip when a user later retries the source."""
        provenance = job.get("discovery") or {}
        job["last_skip"] = {
            "url": job["url"], "title": provenance.get("title") or job.get("title", ""),
            "source_url": provenance.get("source_url") or job["url"],
            "reason": job.get("skip_reason") or job.get("error") or job.get("message", ""),
            "timestamp": _now(), "error_code": job.get("error_code", ""),
        }

    def migrate_restricted_jobs(self):
        """Classify historical failures and skip only continuous restricted jobs.

        This local database operation never resumes a queue or accesses a site.
        It is idempotent and leaves ambiguous historical failures recoverable.
        """
        self._ensure_open()
        changed = []
        with self._mutex, self._db(write=True) as db:
            for row in list(db.execute("SELECT data FROM online_jobs WHERE status='failed'")):
                job = json.loads(row[0])
                details = classify_online_error(job)
                continuous = job.get("discovery", {}).get("continuous") is True
                skipped = continuous and details["restricted"]
                if not skipped and all(job.get(key) == value for key, value in details.items()):
                    continue
                job.update(details)
                if skipped:
                    job.update(status="skipped", phase="skipped", control=None, speed=None, eta=None,
                               message="已跳过受限视频：" + details["skip_reason"], learn=False)
                    self._remember_skip(job)
                    changed.append(job["id"])
                self._save(db, job)
                self._event(job, "skipped" if skipped else "status")
        return changed

    def add(self, text, *, mode="time", height=720, subtitles=True, learn=True, request_version=0,
            learning_kind="movie-temporal"):
        self._ensure_open()
        if mode not in {"time", "train", "val"}:
            raise ValueError("请选择有效的训练或验证分组")
        if type(height) is not int or height not in {0, 360, 480, 720, 1080, 1440, 2160}:
            raise ValueError("请选择有效的视频清晰度")
        if learning_kind not in {"movie-temporal", *AUTOMATIC_LEARNING}:
            raise ValueError("请选择有效的学习模式")
        config = dict(mode=mode, height=height, subtitles=bool(subtitles), learn=bool(learn))
        # Preserve legacy identities: an old queue entry without this field is
        # still the same movie-temporal task when re-added after an upgrade.
        if learning_kind != "movie-temporal":
            config["learning_kind"] = learning_kind
        request_version = int(request_version)
        result = dict(added=[], duplicates=[], errors=[])
        batch_id = uuid.uuid4().hex
        with self._mutex, self._db(write=True) as db:
            paused = self._is_paused(db)
            for number, raw in enumerate(str(text).splitlines(), 1):
                if not raw.strip():
                    continue
                try:
                    url = validate_url(raw)
                except ValueError as exc:
                    result["errors"].append(dict(line=number, text=raw, message=str(exc)))
                    continue
                job = self._new_job(url, config.copy(), batch_id, request_version, paused)
                if db.execute("SELECT 1 FROM online_jobs WHERE fingerprint=?", (job["fingerprint"],)).fetchone():
                    result["duplicates"].append(url)
                    continue
                self._save(db, job, insert=True)
                result["added"].append(job["id"])
                self._event(job)
        return result

    def _event(self, job, kind="status", **values):
        event = dict(type=kind, job_id=job["id"], batch_id=job["batch_id"],
                     status=job["status"], phase=job["phase"], progress=job["progress"],
                     **{key: job.get(key, default) for key, default in EMPTY_ERROR.items()}, **values)
        with self._mutex:
            if kind in {"completed", "skipped"}:
                self._events.append(event)
            else:
                self._updates[job["id"]] = event

    def drain_events(self):
        with self._mutex:
            events = list(self._updates.values()) + self._events
            self._updates = {}
            self._events = []
        return events

    @staticmethod
    def _authorize_batches(db, batch_ids, request_version):
        if request_version is None or not batch_ids:
            return
        for row in list(db.execute("SELECT data FROM online_jobs")):
            job = json.loads(row[0])
            if job["batch_id"] in batch_ids:
                job["request_version"] = int(request_version)
                # Resume/retry is a new explicit request for this batch. Keep
                # successful handoffs acknowledged, but remove a prior Stop.
                if job.get("config", {}).get("learning_kind") in AUTOMATIC_LEARNING:
                    job.pop("learning_cancelled", None)
                OnlineJobManager._save(db, job)

    def _start(self, *, resuming=False, request_version=None):
        self._ensure_open()
        with self._mutex:
            if self._worker_active and not resuming:
                return
            lock = None
            if not self._worker_active:
                lock = self._project_lock()
                if lock.fd is None:
                    raise ValueError("此工程的在线视频队列正在另一个窗口中运行，请在该窗口管理任务")
            try:
                with self._db(write=True) as db:
                    if resuming and request_version is not None:
                        batches = {json.loads(row[0])["batch_id"] for row in db.execute("SELECT data FROM online_jobs WHERE status IN ('queued','paused','running')")}
                        self._authorize_batches(db, batches, request_version)
                    self._set_paused(db, False)
                    statuses = "('paused','running')" if lock else "('paused')"
                    for row in list(db.execute("SELECT data FROM online_jobs WHERE status IN " + statuses)):
                        job = json.loads(row[0])
                        status = "cancelled" if job.get("control") == "cancelled" else "queued"
                        job.update(status=status, phase=status, control=None, error="", **EMPTY_ERROR,
                                   message="已取消；已有资源保留" if status == "cancelled" else "等待后台导入")
                        self._save(db, job)
                        self._event(job)
                if lock:
                    self._worker_active = True
                    self._thread = threading.Thread(target=self._work, args=(lock,), daemon=True, name="online-video-queue")
                    self._thread.start()
                    lock = None
            finally:
                if lock:
                    lock.close()
                    self._worker_active = False

    def start(self):
        self._start()

    def resume(self, *, request_version=None):
        self._start(resuming=True, request_version=request_version)

    def pause(self):
        self._ensure_open()
        with self._mutex, self._db(write=True) as db:
            self._set_paused(db, True)
            for row in list(db.execute("SELECT data FROM online_jobs WHERE status IN ('queued','running')")):
                job = json.loads(row[0])
                if job["status"] == "running":
                    if job.get("control") != "cancelled":
                        job.update(control="paused", message="正在暂停；继续时复用已下载的部分")
                else:
                    job.update(status="paused", phase="paused", message="队列已暂停")
                self._save(db, job)
                self._event(job)
            if self._stop:
                self._stop.set()

    @staticmethod
    def _ids(ids):
        return [ids] if isinstance(ids, str) else list(dict.fromkeys(str(value) for value in ids))

    def retry(self, ids, *, request_version=None):
        self._ensure_open()
        changed = []
        with self._mutex, self._db(write=True) as db:
            paused = self._is_paused(db)
            for job_id in self._ids(ids):
                job = self._get(db, job_id)
                if not job or job["status"] not in {"failed", "cancelled", "paused", "skipped"}:
                    continue
                if job["status"] == "skipped" and not isinstance(job.get("last_skip"), dict):
                    self._remember_skip(job)
                job.update(status="paused" if paused else "queued", phase="paused" if paused else "queued",
                           error="", control=None, progress=None, speed=None, eta=None, **EMPTY_ERROR,
                           learn=bool(job["config"]["learn"]),
                           message="等待继续队列" if paused else "等待重试；可复用的缓存会保留")
                job.pop("error_detail", None)
                job.pop("classification", None)
                self._save(db, job)
                self._event(job)
                changed.append(job_id)
            batches = {self._get(db, job_id)["batch_id"] for job_id in changed}
            self._authorize_batches(db, batches, request_version)
        return changed

    def acknowledge_learning(self, ids):
        """Record a durable handoff after automatic reference enqueue succeeds."""
        self._ensure_open()
        changed = []
        with self._mutex, self._db(write=True) as db:
            for job_id in self._ids(ids):
                job = self._get(db, job_id)
                if not job or job["status"] != "completed" or job.get("learning_dispatched"):
                    continue
                if job.get("config", {}).get("learning_kind") not in AUTOMATIC_LEARNING:
                    continue
                job["learning_dispatched"] = True
                self._save(db, job)
                changed.append(job_id)
        return changed

    def attach_discovery(self, job_id, candidate):
        """Keep search provenance outside the download's deduplication identity."""
        self._ensure_open()
        with self._mutex, self._db(write=True) as db:
            job = self._get(db, job_id)
            if not job:
                raise ValueError("下载任务已不存在")
            job["discovery"] = {key: str(candidate.get(key, ""))[:4000]
                                for key in ("id", "round_id", "title", "source_url", "provider", "category")}
            if candidate.get("continuous") is True:
                job["discovery"]["continuous"] = True
            if "video_metadata" in candidate:
                try:
                    from iqiyi_video import clean_video_metadata
                    job["discovery"]["video_metadata"] = clean_video_metadata(job["url"], candidate["video_metadata"])
                except (ValueError, ImportError) as exc:
                    job["discovery"]["metadata_warning"] = "搜索视频标识不可用，将使用原网页解析：" + str(exc)[:500]
            elif candidate.get("metadata_warning"):
                job["discovery"]["metadata_warning"] = str(candidate["metadata_warning"])[:1000]
            self._save(db, job)

    def request_learning(self, ids, *, request_version=0):
        """Explicitly retry learning of an already imported resource."""
        self._ensure_open()
        changed = []
        with self._mutex, self._db(write=True) as db:
            for job_id in self._ids(ids):
                job = self._get(db, job_id)
                if not job or job["status"] != "completed" or job.get("config",{}).get("learning_kind") not in AUTOMATIC_LEARNING:
                    continue
                job.update(learning_dispatched=False,learning_cancelled=False,request_version=int(request_version),
                           learn=bool(job["config"]["learn"]))
                self._save(db, job)
                changed.append(job_id)
        return changed

    def cancel_learning(self, ids):
        """Persist Stop independently of download state and future completion."""
        self._ensure_open()
        changed = []
        with self._mutex, self._db(write=True) as db:
            for job_id in self._ids(ids):
                job = self._get(db, job_id)
                if not job or job.get("learning_cancelled"):
                    continue
                if job.get("config", {}).get("learning_kind") not in AUTOMATIC_LEARNING:
                    continue
                job["learning_cancelled"] = True
                self._save(db, job)
                changed.append(job_id)
        return changed

    def cancel(self, ids):
        self._ensure_open()
        changed = []
        with self._mutex, self._db(write=True) as db:
            for job_id in self._ids(ids):
                job = self._get(db, job_id)
                if not job or job["status"] not in PENDING:
                    continue
                if job["status"] == "running":
                    job.update(control="cancelled", message="正在取消；保留电影和已下载的缓存")
                    if job_id == self._current_id and self._stop:
                        self._stop.set()
                else:
                    job.update(status="cancelled", phase="cancelled", control=None, message="已取消；可重试，已有资源保留")
                self._save(db, job)
                self._event(job)
                changed.append(job_id)
        return changed

    def remove(self, ids):
        self._ensure_open()
        ids = self._ids(ids)
        removed = []
        with self._mutex, self._db(write=True) as db:
            jobs = [self._get(db, job_id) for job_id in ids]
            if any(job and job["status"] == "running" for job in jobs):
                raise ValueError("请先取消正在运行的任务，等待停止后再移除记录")
            for job in jobs:
                if job:
                    db.execute("DELETE FROM online_jobs WHERE id=?", (job["id"],))
                    removed.append(job["id"])
                    self._updates.pop(job["id"], None)
        return removed

    def close(self):
        with self._mutex:
            if self._closed:
                return
            if self.busy:
                self.pause()
            self._closed = True
            if self._stop:
                self._stop.set()

    def _record(self, job_id, message=None, progress=None):
        with self._mutex, self._db(write=True) as db:
            job = self._get(db, job_id)
            if not job or job["status"] != "running":
                return
            if message is not None:
                message = str(message).replace("\x00", "")[-4000:]
                line = _now() + "  " + message + "\n"
                job["log_tail"] = (job["log_tail"] + line)[-TAIL_LIMIT:]
                job["message"] = message
                try:
                    path = Path(job["log_path"])
                    previous = path.read_bytes()[-LOG_LIMIT:] if path.exists() else b""
                    path.write_bytes((previous + line.encode("utf-8"))[-LOG_LIMIT:].decode("utf-8", errors="ignore").encode("utf-8"))
                except OSError:
                    pass  # An unavailable log folder must not abort a video import.
            if progress is not None:
                job["phase"] = str(progress.get("phase") or "downloading")
                for source, target in (("percent", "progress"), ("speed", "speed"), ("eta", "eta"),
                                       ("downloaded_bytes", "downloaded_bytes"), ("total_bytes", "total_bytes")):
                    value = progress.get(source)
                    valid = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
                    job[target] = min(100, value) if valid and target == "progress" else value if valid else None
                if progress.get("title"):
                    job["title"] = str(progress["title"])[:180]
            self._save(db, job)
            self._event(job)

    def _monitor(self, job_id, stop, done):
        while not done.wait(.1):
            if stop.is_set():
                return
            try:
                with self._db() as db:
                    job = self._get(db, job_id)
                    if self._closed or not job or job.get("control") or self._is_paused(db):
                        stop.set()
                        return
            except sqlite3.Error:
                continue

    def _work(self, lock):
        try:
            while not self._closed:
                with self._mutex, self._db(write=True) as db:
                    if self._is_paused(db):
                        return
                    row = db.execute("SELECT data FROM online_jobs WHERE status='queued' ORDER BY created,rowid LIMIT 1").fetchone()
                    if not row:
                        return
                    job = json.loads(row[0])
                    job.update(status="running", phase="resolving", control=None, error="", progress=None, **EMPTY_ERROR,
                               speed=None, eta=None, message="正在解析视频来源", attempts=job["attempts"] + 1)
                    self._save(db, job)
                    self._event(job)
                    stop = threading.Event()
                    self._stop, self._current_id = stop, job["id"]
                done = threading.Event()
                monitor = threading.Thread(target=self._monitor, args=(job["id"], stop, done), daemon=True)
                monitor.start()
                result, error, error_details = None, None, EMPTY_ERROR.copy()
                try:
                    config = job["config"]
                    kwargs = dict(height=config["height"], subtitles=config["subtitles"], stop=stop)
                    parameters = inspect.signature(self._importer).parameters
                    accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values())
                    if "on_progress" in parameters or accepts_kwargs:
                        kwargs["on_progress"] = lambda value: self._record(job["id"], progress=value)
                    discovery = job.get("discovery", {})
                    if discovery.get("metadata_warning"):
                        self._record(job["id"], message=discovery["metadata_warning"])
                    if "video_metadata" in discovery:
                        try:
                            from iqiyi_video import clean_video_metadata
                            metadata = clean_video_metadata(job["url"], discovery["video_metadata"])
                        except (ValueError, ImportError) as exc:
                            self._record(job["id"], message="搜索视频标识不可用，将使用原网页解析：" + str(exc)[:500])
                        else:
                            parameter = parameters.get("source_metadata")
                            if accepts_kwargs or (parameter is not None and parameter.kind in (
                                    inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)):
                                kwargs["source_metadata"] = metadata
                            else:
                                self._record(job["id"], message="当前下载器不接收搜索视频标识，将使用原网页解析")
                    result = self._importer(self.root, job["url"], config["mode"],
                                            lambda message: self._record(job["id"], message=message), **kwargs)
                    if not isinstance(result, dict):
                        raise ValueError("在线视频导入未返回有效结果")
                except Exception as exc:
                    error = (str(exc) or type(exc).__name__)[-4000:]
                    error_details = classify_online_error(exc)
                    result = None
                finally:
                    done.set()
                    monitor.join(timeout=.3)
                if error:
                    self._record(job["id"], message=error)
                with self._mutex, self._db(write=True) as db:
                    current = self._get(db, job["id"])
                    paused = self._is_paused(db)
                    control = current.get("control")
                    interrupted = stop.is_set() or bool(control) or self._closed or paused
                    if result is not None:
                        current.update(status="completed", phase="completed", progress=100, result=result,
                                       learn=bool(current["config"]["learn"] and not interrupted),
                                       speed=None, eta=None,
                                       title=str(result.get("title") or current["title"])[:180], error="", control=None, **EMPTY_ERROR,
                                       message="已导入；自动学习已取消" if interrupted else "视频已导入学习工程")
                    elif interrupted:
                        status = "cancelled" if control == "cancelled" else "paused" if paused or self._closed else "queued"
                        current.update(status=status, phase=status, control=None, error="", speed=None, eta=None, **EMPTY_ERROR,
                                       message="已取消；可重试，缓存保留" if status == "cancelled" else "已暂停；继续时复用已有下载" if status == "paused" else "等待继续导入")
                    else:
                        skipped = current.get("discovery", {}).get("continuous") is True and error_details["restricted"]
                        status = "skipped" if skipped else "failed"
                        current.update(status=status, phase=status, error=error, control=None, speed=None, eta=None,
                                       message="已跳过受限视频：" + error_details["skip_reason"] if skipped else error,
                                       **error_details)
                        if skipped:
                            current["learn"] = False
                            self._remember_skip(current)
                    self._save(db, current)
                    self._stop, self._current_id = None, None
                    if result is not None:
                        self._event(current, "completed", result=result, learn=bool(current["learn"] and not interrupted and not current.get("learning_cancelled")),
                                    request_version=current["request_version"])
                    else:
                        self._event(current, "skipped" if current["status"] == "skipped" else "status")
        except Exception as exc:
            # Keep unexpected queue/database faults visible and recoverable.
            try:
                with self._mutex, self._db(write=True) as db:
                    self._set_paused(db, True)
                    if self._current_id:
                        job = self._get(db, self._current_id)
                        if job:
                            job.update(status="paused", phase="paused", error=str(exc), message="队列已暂停：" + str(exc), control=None)
                            self._save(db, job)
                            self._event(job)
            except Exception:
                pass
        finally:
            with self._mutex:
                self._stop, self._current_id = None, None
                # An add/start can arrive after the empty-queue check and before
                # this thread exits. Hand off the held lock so no work is lost.
                restart = False
                try:
                    with self._db() as db:
                        restart = not self._closed and not self._is_paused(db) and db.execute("SELECT 1 FROM online_jobs WHERE status='queued' LIMIT 1").fetchone() is not None
                finally:
                    if restart:
                        self._thread = threading.Thread(target=self._work, args=(lock,), daemon=True, name="online-video-queue")
                        self._thread.start()
                    else:
                        lock.close()
                        self._worker_active = False

    def _migrate_history(self, db):
        if db.execute("SELECT 1 FROM online_meta WHERE key='legacy_migrated'").fetchone():
            return
        annotations = self.root / "annotations.sqlite"
        if annotations.exists():
            with closing(sqlite3.connect(annotations, timeout=1)) as movie_db:
                movie_db.row_factory = sqlite3.Row
                try:
                    movies = {row["digest"]: dict(row) for row in movie_db.execute("SELECT * FROM movies")}
                except sqlite3.Error:
                    movies = {}
            from cache_storage import cache_roots
            manifests = [manifest for base in cache_roots(self.root) for manifest in base.glob("*/completed.json")]
            for manifest in sorted(manifests):
                try:
                    info = json.loads(manifest.read_text(encoding="utf-8"))
                    if not isinstance(info, dict):
                        continue
                    movie = movies.get(info.get("sha256"))
                    if not movie:
                        continue
                    url = validate_url(info["source_url"])
                    config = dict(mode=movie["mode"] if movie["mode"] in {"time", "train", "val"} else "time",
                                  height=info.get("height", 720), subtitles=bool(info.get("subtitles", True)), learn=False)
                    job = self._new_job(url, config, "legacy", 0, False)
                    if db.execute("SELECT 1 FROM online_jobs WHERE fingerprint=?", (job["fingerprint"],)).fetchone():
                        continue
                    result = dict(movie_id=movie["id"], title=movie["name"], path=movie.get("source_path") or "",
                                  reused=True, subtitle_report="", source_url=url)
                    if isinstance(info.get("metadata"), dict):
                        result["metadata"] = info["metadata"]
                    preview = (manifest.parent / "preview.jpg").resolve()
                    if preview.is_relative_to(manifest.parent.resolve()) and preview.is_file():
                        result["thumbnail_path"] = str(preview)
                    job.update(status="completed", phase="completed", progress=100, title=movie["name"], result=result,
                               message="既有在线视频导入记录", created=info.get("downloaded_at") or _now())
                    self._save(db, job, insert=True)
                except (OSError, ValueError, KeyError, TypeError):
                    continue
        db.execute("INSERT OR REPLACE INTO online_meta VALUES ('legacy_migrated','1')")
