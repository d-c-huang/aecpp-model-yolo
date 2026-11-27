"""Bounded online discovery, with durable provenance and download handoffs.

The worker discovers metadata only. The application admits claimed candidates to
its existing download queue and reports download/learning progress back here.
"""
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import uuid

from online_jobs import _ProjectLock
from online_video import validate_url


DEFAULT_CONFIG = {
    "animation_enabled": True, "movie_enabled": True,
    "animation_query": "沙雕动画 搞笑", "movie_query": "喜剧电影",
    "animation_provider": "web", "movie_provider": "web",
    "sites": ["bilibili", "youku", "iqiyi"],
    "max_per_category": 3, "max_total_downloads": 4, "auto_download": True,
}
CANDIDATE_STATES = {"discovered", "dispatching", "queued", "downloading", "learning",
                    "completed", "needs_input", "failed", "skipped", "ignored", "cancelled", "paused"}
SEARCH_STATES = {"searching", "paused", "stopped", "interrupted", "completed", "partial", "failed"}
MAX_EVENTS = 2000
_UNSET = object()
DETAIL_FIELDS = {"phase", "current_url", "speed", "eta", "downloaded_bytes", "total_bytes",
                 "error", "reference_status", "movie_status", "failure_stage", "learning_kind",
                 "title", "download", "learning", "progress", "message", "error_code", "skip_reason"}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def clean_config(config):
    if not isinstance(config, dict):
        raise ValueError("联网学习设置无效")
    result = {**DEFAULT_CONFIG, **{key: value for key, value in config.items() if key in DEFAULT_CONFIG}}
    for key in ("animation_enabled", "movie_enabled", "auto_download"):
        if type(result[key]) is not bool:
            raise ValueError("请选择有效的学习类型和自动下载设置")
    if not result["animation_enabled"] and not result["movie_enabled"]:
        raise ValueError("请至少选择沙雕动画或电影")
    for category in ("animation", "movie"):
        query = result[category+"_query"]
        if not isinstance(query, str) or len(query) > 300 or "\x00" in query:
            raise ValueError("搜索关键词须为不超过 300 字的文本")
        result[category+"_query"] = query.strip()
        if result[category+"_enabled"] and not query.strip():
            raise ValueError("请输入已选类型的搜索关键词")
        if result[category+"_provider"] != "web":
            raise ValueError("请选择可用的搜索来源")
    sites = result["sites"]
    if not isinstance(sites, list) or not sites or any(site not in {"bilibili", "youku", "iqiyi"} for site in sites):
        raise ValueError("请至少选择优酷、爱奇艺或 B 站中的一个来源")
    result["sites"] = list(dict.fromkeys(sites))
    for key, maximum in (("max_per_category", 10), ("max_total_downloads", 20)):
        if type(result[key]) is not int or not 1 <= result[key] <= maximum:
            raise ValueError(f"{key} 须为 1–{maximum} 的整数")
    return result


def canonical_url(value):
    parsed = urlsplit(validate_url(value))
    host = parsed.hostname.lower()
    if ":" in host:
        host = "["+host+"]"
    if parsed.port and (parsed.scheme, parsed.port) not in {("http", 80), ("https", 443)}:
        host += ":"+str(parsed.port)
    query = [(key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in {"spm_id_from", "vd_source", "feature"}]
    return urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", urlencode(query), ""))


def video_site(value):
    parsed = urlsplit(validate_url(value))
    host = parsed.hostname.lower()
    if (host == "bilibili.com" or host.endswith(".bilibili.com")) and re.fullmatch(r"/video/(?:BV[a-zA-Z0-9]+|av[0-9]+)/?", parsed.path):
        return "bilibili"
    if (host == "youku.com" or host.endswith(".youku.com")) and re.fullmatch(r"/(?:v_show|video)/id_[a-zA-Z0-9=_-]+\.html", parsed.path):
        return "youku"
    if (host == "iqiyi.com" or host.endswith(".iqiyi.com")) and re.fullmatch(r"/[vw]_[a-zA-Z0-9_-]+\.html", parsed.path):
        return "iqiyi"
    raise ValueError("仅允许优酷、爱奇艺和 B 站的单个视频页面")


def _bounded(value, *, depth=0):
    if depth > 4:
        raise ValueError("任务状态层级过深")
    if value is None or type(value) is bool:
        return value
    if isinstance(value, str):
        return value.replace("\x00", "")[:4000]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            raise ValueError("任务状态包含无效数值")
        return value
    if isinstance(value, dict) and len(value) <= 40:
        return {str(key)[:80]: _bounded(item, depth=depth+1) for key, item in value.items()}
    if isinstance(value, (list, tuple)) and len(value) <= 40:
        return [_bounded(item, depth=depth+1) for item in value]
    raise ValueError("任务状态格式无效或过大")


class _Stopped(Exception):
    pass


class DiscoveryManager:
    def __init__(self, root, *, searcher=None):
        self.root = Path(root).resolve()
        self.folder = self.root/"discovery"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root/"discovery.sqlite"
        self._mutex = threading.RLock()
        self._thread = None
        self._worker_lock = None
        self._stop = threading.Event()
        self._closed = False
        self._current_round = None
        self._events = deque(maxlen=500)
        self._owner = uuid.uuid4().hex
        self._builtin_searcher = searcher is None
        if searcher is None:
            from discovery_sources import discover
            searcher = discover
        self._searcher = searcher
        with self._db(write=True) as db:
            db.execute("CREATE TABLE IF NOT EXISTS rounds(id TEXT PRIMARY KEY,status TEXT NOT NULL,data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS candidates(id TEXT PRIMARY KEY,url_key TEXT UNIQUE NOT NULL,round_id TEXT NOT NULL,state TEXT NOT NULL,data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        lock = self._project_lock()
        if lock.fd is not None:
            try:
                with self._db(write=True) as db:
                    for raw in db.execute("SELECT data FROM rounds").fetchall():
                        row = json.loads(raw[0])
                        if row["status"] != "searching":
                            continue
                        row["auto_allowed"] = False
                        row.update(status="interrupted", message="上次联网查找未完成；点击继续后重新查找未完成的类型")
                        self._save_round(db, row)
            finally:
                lock.close()

    @contextmanager
    def _db(self, *, write=False):
        db = sqlite3.connect(self.db_path, timeout=2)
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

    @contextmanager
    def _mutation(self, *, optional=False):
        """Serialize short queue writes against another worker or project export."""
        with self._mutex:
            lock = None
            if self._worker_lock is None:
                lock = self._project_lock()
                if lock.fd is None:
                    lock.close()
                    if optional:
                        yield None
                        return
                    raise ValueError("其他窗口正在联网查找或迁移本工程，请稍后重试")
            try:
                with self._db(write=True) as db:
                    yield db
            finally:
                if lock is not None:
                    lock.close()

    @staticmethod
    def _round(db, identifier):
        row = db.execute("SELECT data FROM rounds WHERE id=?", (identifier,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _candidate(db, identifier):
        row = db.execute("SELECT data FROM candidates WHERE id=?", (identifier,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _save_round(db, row):
        row["updated"] = _now()
        db.execute("INSERT INTO rounds VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,data=excluded.data",
                   (row["id"], row["status"], json.dumps(row, ensure_ascii=False, allow_nan=False)))

    @staticmethod
    def _save_candidate(db, row):
        row["updated"] = _now()
        db.execute("INSERT INTO candidates VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,data=excluded.data",
                   (row["id"], row["url_key"], row["round_id"], row["state"], json.dumps(row, ensure_ascii=False, allow_nan=False)))

    def _event(self, db, **event):
        event = {"timestamp": _now(), "provider": "", "query": "", "url": "", "title": "",
                 "phase": "", "message": "", **_bounded(event)}
        db.execute("INSERT INTO events(data) VALUES(?)", (json.dumps(event, ensure_ascii=False, allow_nan=False),))
        db.execute("DELETE FROM events WHERE seq <= (SELECT COALESCE(MAX(seq),0)-? FROM events)", (MAX_EVENTS,))
        self._events.append(event)
        return event

    def _ensure_open(self):
        if self._closed:
            raise RuntimeError("联网发现管理器已关闭")

    @property
    def busy(self):
        return self._thread is not None and self._thread.is_alive()

    @property
    def paused(self):
        rows = self.list_rounds()
        return bool(rows and rows[0]["status"] in {"paused", "stopped", "interrupted"})

    def get_config(self):
        with self._db() as db:
            row = db.execute("SELECT value FROM meta WHERE key='config'").fetchone()
        return clean_config(json.loads(row[0])) if row else clean_config({})

    def list_rounds(self):
        with self._db() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT data FROM rounds ORDER BY rowid DESC LIMIT 200")]

    def get_round(self, identifier):
        with self._db() as db:
            return self._round(db, identifier)

    def resume_pending(self, round_ids):
        """Re-authorize only the continuous owner's unhanded-off past candidates.

        This does not search, restart failed downloads, or alter completed jobs.
        Calling it requires an explicit continuous start and successful setup.
        """
        if not round_ids:
            return 0
        owned = set(round_ids[-2000:])
        changed = 0
        with self._mutation() as db:
            for identifier in owned:
                parent = self._round(db, identifier)
                if not parent:
                    continue
                parent.update(auto_allowed=parent["config"]["auto_download"], owner=self._owner)
                self._save_round(db, parent)
                for raw in db.execute("SELECT data FROM candidates WHERE round_id=? AND state IN ('discovered','dispatching','cancelled')", (identifier,)).fetchall():
                    candidate = json.loads(raw[0])
                    if candidate.get("online_job_id") or (candidate["state"] == "cancelled" and not candidate.get("cancelled_by_discovery")):
                        continue
                    candidate.update(state="discovered", cancelled_by_discovery=False, manual_requested=False,
                                     detail="持续学习已继续，等待交接下载")
                    candidate.pop("claim_owner", None)
                    self._save_candidate(db, candidate)
                    changed += 1
        return changed

    def list_candidates(self):
        with self._db() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT data FROM candidates ORDER BY rowid DESC LIMIT 2000")]

    def list_events(self, limit=100):
        limit = max(1, min(MAX_EVENTS, int(limit)))
        with self._db() as db:
            return [{**json.loads(row[1]), "id": row[0]} for row in db.execute("SELECT seq,data FROM events ORDER BY seq DESC LIMIT ?", (limit,))]

    def drain_events(self):
        with self._mutex:
            result = list(self._events)
            self._events.clear()
            return result

    def snapshot(self):
        rounds = self.list_rounds()
        active = next((row.get("active", {}) for row in rounds if row["status"] == "searching"),
                      rounds[0].get("active", {}) if rounds else {})
        return {"busy": self.busy, "rounds": rounds, "candidates": self.list_candidates(),
                "events": self.list_events(), "active": active, "config": self.get_config()}

    def start(self, config):
        self._ensure_open()
        config = clean_config(config)
        with self._mutex:
            if self.busy:
                raise ValueError("当前仍在联网查找，请先停止或等待完成")
            lock = self._project_lock()
            if lock.fd is None:
                raise ValueError("其他窗口正在联网查找或迁移本工程，请稍后重试")
            try:
                identifier = uuid.uuid4().hex
                row = {"id": identifier, "status": "searching", "config": config, "created": _now(),
                       "owner": self._owner,
                       "auto_allowed": config["auto_download"], "completed_categories": [], "errors": {},
                       "candidate_count": 0, "duplicate_count": 0, "message": "准备按关键词查找动画和电影", "active": {}}
                with self._db(write=True) as db:
                    self._save_round(db, row)
                    db.execute("INSERT OR REPLACE INTO meta VALUES('config',?)", (json.dumps(config, ensure_ascii=False),))
                    self._event(db, type="round", round_id=identifier, phase="starting", message=row["message"])
                self._launch(identifier, lock)
                return identifier
            except BaseException:
                lock.close()
                raise

    def _launch(self, identifier, lock):
        self._current_round = identifier
        self._worker_lock = lock
        self._stop.clear()
        self._thread = threading.Thread(target=self._work, args=(identifier, lock), daemon=True, name="discovery-search")
        try:
            self._thread.start()
        except BaseException:
            self._worker_lock = None
            raise

    def _check(self, identifier):
        if self._closed or self._stop.is_set():
            raise _Stopped()
        with self._db() as db:
            row = self._round(db, identifier)
        if not row or row["status"] != "searching":
            raise _Stopped()

    def _report(self, identifier, category, query, provider, event):
        self._check(identifier)
        if not isinstance(event, dict):
            event = {"message": str(event)}
        data = {key: event[key] for key in ("phase", "url", "title", "message", "site") if key in event}
        data = {"phase": "fetch", "message": "正在获取素材信息", **data,
                "query": query, "provider": provider, "category": category}
        with self._mutex, self._db(write=True) as db:
            row = self._round(db, identifier)
            if row["status"] != "searching":
                raise _Stopped()
            row["active"] = self._event(db, type="search", round_id=identifier, **data)
            row["message"] = str(data["message"])[:1000]
            self._save_round(db, row)

    def _store_results(self, identifier, category, query, provider, results):
        self._check(identifier)
        if not isinstance(results, list):
            raise ValueError("搜索来源没有返回有效的素材列表")
        with self._mutex, self._db(write=True) as db:
            row = self._round(db, identifier)
            if row["status"] != "searching":
                raise _Stopped()
            accepted = 0
            for item in results[:100]:
                if accepted >= row["config"]["max_per_category"]:
                    break
                if not isinstance(item, dict):
                    continue
                try:
                    url = validate_url(item.get("url", ""))
                    source = validate_url(item.get("source_url") or url)
                    key = canonical_url(url)
                    site = video_site(url)
                    if site not in row["config"]["sites"]:
                        raise ValueError("素材不属于本轮选择的网站")
                except ValueError:
                    continue
                existing = db.execute("SELECT id FROM candidates WHERE url_key=?", (key,)).fetchone()
                if existing:
                    row["duplicate_count"] += 1
                    self._event(db, type="duplicate", round_id=identifier, candidate_id=existing[0], category=category,
                                phase="duplicate", provider=provider, query=query, url=url, message="已发现过此链接，跳过重复加入")
                    continue
                candidate = {"id": uuid.uuid4().hex, "round_id": identifier, "url_key": key,
                             "url": url, "source_url": source, "provider": provider, "query": query,
                             "category": category, "site": site, "title": str(item.get("title") or url)[:500],
                             "state": "discovered", "detail": "已发现；等待加入下载学习队列" if row["config"]["auto_download"] else "已发现；可选择后手动加入",
                             "created": _now(), "manual_requested": False, "budget_reserved": False,
                             "online_job_id": None, "movie_id": None, "progress": None, "learning": None}
                for field in ("description", "duration", "thumbnail", "license"):
                    if field in item:
                        candidate[field] = _bounded(item[field])
                if "video_metadata" in item:
                    try:
                        from iqiyi_video import clean_video_metadata
                        candidate["video_metadata"] = clean_video_metadata(url, _bounded(item["video_metadata"]))
                    except (ValueError, ImportError) as exc:
                        candidate["metadata_warning"] = "搜索视频标识不可用，将使用原网页解析：" + str(exc)[:500]
                        self._event(db, type="warning", round_id=identifier, candidate_id=candidate["id"],
                                    category=category, phase="metadata", provider=provider, query=query,
                                    url=url, title=candidate["title"], message=candidate["metadata_warning"])
                self._save_candidate(db, candidate)
                accepted += 1
                row["candidate_count"] += 1
                self._event(db, type="candidate", round_id=identifier, candidate_id=candidate["id"], category=category,
                            phase="discovered", provider=provider, query=query, url=url, source_url=source,
                            title=candidate["title"], message=candidate["detail"])
            row["completed_categories"] = list(dict.fromkeys([*row["completed_categories"], category]))
            self._save_round(db, row)

    def _work(self, identifier, lock):
        try:
            with self._db() as db:
                initial = self._round(db, identifier)
            config = initial["config"]
            for category in ("animation", "movie"):
                if not config[category+"_enabled"] or category in initial["completed_categories"]:
                    continue
                self._check(identifier)
                query, provider = config[category+"_query"], config[category+"_provider"]
                try:
                    self._report(identifier, category, query, provider, {"phase": "query", "message": "正在搜索："+query})
                    extra = {}
                    if self._builtin_searcher:
                        with self._db() as db:
                            extra["exclude_urls"] = {raw[0] for raw in db.execute("SELECT url_key FROM candidates")}
                    results = self._searcher(query, category=category, provider=provider, sites=config["sites"], limit=config["max_per_category"],
                                            report=lambda event, c=category, q=query, p=provider: self._report(identifier,c,q,p,event),
                                            stop=self._stop, **extra)
                    self._store_results(identifier, category, query, provider, results)
                except Exception as exc:
                    self._check(identifier)
                    with self._mutex, self._db(write=True) as db:
                        row = self._round(db, identifier)
                        message = (str(exc) or type(exc).__name__)[:2000]
                        row["errors"][category] = message
                        self._save_round(db, row)
                        self._event(db, type="error", round_id=identifier, category=category, query=query,
                                    provider=provider, phase="failed", message=message,
                                    url=row.get("active", {}).get("url", ""),
                                    title=row.get("active", {}).get("title", ""),
                                    site=row.get("active", {}).get("site", ""))
            self._check(identifier)
            with self._mutex, self._db(write=True) as db:
                row = self._round(db, identifier)
                count, duplicate = row["candidate_count"], row["duplicate_count"]
                status = "partial" if row["errors"] and row["completed_categories"] else "failed" if row["errors"] else "completed"
                row.update(status=status, message=f"本轮发现 {count} 条新素材，跳过 {duplicate} 条重复链接" + ("；部分来源失败，可继续重试" if row["errors"] else ""))
                self._save_round(db, row)
                self._event(db, type="round", round_id=identifier, phase=status, message=row["message"])
        except _Stopped:
            pass
        except Exception as exc:
            with self._mutex, self._db(write=True) as db:
                row = self._round(db, identifier)
                if row and row["status"] == "searching":
                    row.update(status="failed", auto_allowed=False, message=(str(exc) or type(exc).__name__)[:2000])
                    self._save_round(db, row)
                    self._event(db, type="error", round_id=identifier, phase="failed", message=row["message"])
        finally:
            with self._mutex:
                lock.close()
                self._worker_lock = None
                self._current_round = None

    def _halt(self, status, *, owned_only=False, optional=False):
        self._stop.set()
        with self._mutation(optional=optional) as db:
            if db is None:
                return
            for raw in db.execute("SELECT data FROM rounds").fetchall():
                row = json.loads(raw[0])
                if owned_only and row.get("owner") != self._owner:
                    continue
                if row["status"] == "searching" or row.get("auto_allowed"):
                    row.update(auto_allowed=False)
                    if row["status"] == "searching":
                        row["status"] = status
                    row["message"] = "已暂停联网查找与新的自动下载交接" if status == "paused" else "已停止联网查找与新的自动下载交接"
                    self._save_round(db, row)
                    self._event(db, type="round", round_id=row["id"], phase=status, message=row["message"])
            for raw in db.execute("SELECT data FROM candidates WHERE state IN ('discovered','dispatching')").fetchall():
                row = json.loads(raw[0])
                if owned_only:
                    parent = self._round(db, row["round_id"])
                    if parent.get("owner") != self._owner and row.get("manual_owner") != self._owner:
                        continue
                row.update(manual_requested=False)
                if status != "paused":
                    row.update(state="cancelled", cancelled_by_discovery=True, detail="联网发现已停止；可手动选择此项重新加入")
                else:
                    row.update(state="discovered", detail="联网发现已暂停；继续后可加入")
                row.pop("claim_owner", None)
                self._save_candidate(db, row)

    def pause(self):
        self._ensure_open()
        self._halt("paused")

    def stop(self):
        self._ensure_open()
        self._halt("stopped")

    def cancel_pending(self):
        self.stop()

    def resume(self, round_id=None):
        self._ensure_open()
        with self._mutex:
            if self.busy:
                raise ValueError("正在结束当前搜索，请稍后继续")
            rounds = self.list_rounds()
            if not rounds:
                raise ValueError("没有可继续的联网查找，请先开始一轮查找")
            identifier = round_id or rounds[0]["id"]
            lock = self._project_lock()
            if lock.fd is None:
                raise ValueError("其他窗口正在联网查找或迁移本工程")
            try:
                with self._db(write=True) as db:
                    row = self._round(db, identifier)
                    if not row:
                        raise ValueError("本轮发现记录已不存在")
                    row.update(status="searching", owner=self._owner, auto_allowed=row["config"]["auto_download"], errors={}, message="继续查找未完成的类型")
                    self._save_round(db, row)
                    for raw in db.execute("SELECT data FROM candidates WHERE round_id=?", (identifier,)).fetchall():
                        candidate = json.loads(raw[0])
                        if candidate.get("cancelled_by_discovery") or candidate["state"] == "dispatching":
                            candidate.update(state="discovered", cancelled_by_discovery=False, detail="等待继续交接")
                            candidate.pop("claim_owner", None)
                            self._save_candidate(db, candidate)
                    self._event(db, type="round", round_id=identifier, phase="resuming", message=row["message"])
                self._launch(identifier, lock)
                return identifier
            except BaseException:
                lock.close()
                raise

    def pending_candidates(self):
        self._ensure_open()
        result = []
        with self._mutation(optional=True) as db:
            if db is None:
                return result
            rounds = {row["id"]: row for raw in db.execute("SELECT data FROM rounds") for row in [json.loads(raw[0])]}
            reservations = {}
            for raw in db.execute("SELECT data FROM candidates").fetchall():
                row = json.loads(raw[0])
                if row.get("budget_reserved"):
                    reservations[row["round_id"]] = reservations.get(row["round_id"], 0)+1
            for raw in db.execute("SELECT data FROM candidates WHERE state='discovered' ORDER BY rowid").fetchall():
                row = json.loads(raw[0])
                parent = rounds.get(row["round_id"])
                if not parent or not ((parent.get("auto_allowed") and parent.get("owner") == self._owner) or
                                      (row.get("manual_requested") and row.get("manual_owner") == self._owner)):
                    continue
                if not row.get("budget_reserved") and reservations.get(row["round_id"], 0) >= parent["config"]["max_total_downloads"]:
                    if row.get("detail") != "本轮自动下载名额已用完；请调整数量后开始新一轮或保留此参考":
                        row["detail"] = "本轮自动下载名额已用完；请调整数量后开始新一轮或保留此参考"
                        self._save_candidate(db, row)
                    continue
                if not row.get("budget_reserved"):
                    reservations[row["round_id"]] = reservations.get(row["round_id"], 0)+1
                row.update(state="dispatching", budget_reserved=True, claim_owner=self._owner,
                           detail="正在加入下载学习队列")
                self._save_candidate(db, row)
                result.append(dict(row))
        return result

    def request_download(self, ids):
        self._ensure_open()
        ids = [ids] if isinstance(ids, str) else list(dict.fromkeys(ids))
        changed = []
        with self._mutation() as db:
            reservations = {}
            for raw in db.execute("SELECT data FROM candidates").fetchall():
                row = json.loads(raw[0])
                if row.get("budget_reserved") or row.get("manual_requested"):
                    reservations[row["round_id"]] = reservations.get(row["round_id"], 0)+1
            for identifier in ids:
                row = self._candidate(db, identifier)
                if not row:
                    raise ValueError("所选素材已不存在，请刷新列表")
                if row["state"] in {"dispatching", "queued", "downloading", "learning", "completed"}:
                    continue
                parent = self._round(db, row["round_id"])
                if not row.get("budget_reserved") and not row.get("manual_requested"):
                    if reservations.get(row["round_id"], 0) >= parent["config"]["max_total_downloads"]:
                        raise ValueError("所选素材超过本轮下载名额；已发现的链接仍保留，可复制到在线视频页面单独加入")
                    reservations[row["round_id"]] = reservations.get(row["round_id"], 0)+1
                row.update(state="discovered", manual_requested=True, manual_owner=self._owner, cancelled_by_discovery=False,
                           detail="已手动选择，等待加入下载学习队列")
                self._save_candidate(db, row)
                changed.append(identifier)
                self._event(db, type="candidate", round_id=row["round_id"], candidate_id=identifier,
                            phase="requested", title=row["title"], url=row["url"], message=row["detail"])
        return changed

    def retry_candidate(self, identifier):
        return self.request_download([identifier])

    def ack_candidate(self, identifier, online_job_id, *, movie_id=None, learning_kind=None, **details):
        self._ensure_open()
        if not isinstance(online_job_id, str) or not online_job_id or len(online_job_id) > 200:
            raise ValueError("下载任务编号无效")
        with self._mutation(optional=True) as db:
            if db is None:
                return False
            row = self._candidate(db, identifier)
            if not row:
                return False
            if row["state"] == "queued" and row.get("online_job_id") == online_job_id:
                return True
            if row["state"] != "dispatching" or row.get("claim_owner") != self._owner:
                return False
            row.update(online_job_id=online_job_id, state="queued", detail="已加入下载队列，等待下载")
            if movie_id is not None:
                row["movie_id"] = int(movie_id)
            if learning_kind is not None:
                row["learning_kind"] = str(learning_kind)[:100]
            row.update(_bounded({key: value for key, value in details.items() if key in DETAIL_FIELDS}))
            row.pop("claim_owner", None)
            self._save_candidate(db, row)
            self._event(db, type="candidate", round_id=row["round_id"], candidate_id=identifier,
                        phase="queued", title=row["title"], url=row["url"], message=row["detail"])
        return True

    def set_candidate_state(self, identifier, state, detail="", *, movie_id=None, progress=_UNSET, learning=None, **details):
        self._ensure_open()
        if state not in CANDIDATE_STATES:
            raise ValueError("发现素材状态无效")
        with self._mutation(optional=True) as db:
            if db is None:
                return False
            row = self._candidate(db, identifier)
            if not row:
                return False
            before = json.dumps(row, sort_keys=True, ensure_ascii=False)
            row.update(state=state, detail=str(detail)[:4000])
            if state == "skipped":
                # A rejected download must not consume the entire round's
                # capacity while other discovered videos are still waiting.
                row["budget_reserved"] = False
            if movie_id is not None:
                row["movie_id"] = int(movie_id)
            if progress is not _UNSET:
                row["progress"] = _bounded(progress)
            if learning is not None:
                row["learning"] = _bounded(learning)
            row.update(_bounded({key: value for key, value in details.items() if key in DETAIL_FIELDS}))
            if before == json.dumps(row, sort_keys=True, ensure_ascii=False):
                return False
            self._save_candidate(db, row)
            self._event(db, type="candidate", round_id=row["round_id"], candidate_id=identifier,
                        phase=state, title=row["title"], url=row["url"], message=row["detail"],
                        progress=row.get("progress"), learning=row.get("learning"))
        return True

    def close(self):
        if self._closed:
            return
        # Closing an observer must not halt a live search owned by another app.
        self._halt("interrupted", owned_only=True, optional=True)
        self._closed = True
        self._stop.set()
