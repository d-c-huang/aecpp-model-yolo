"""User-started continuous discovery with local setup, backpressure and recovery.

The GUI calls tick() with observed downstream workload. Only local model setup
runs in this manager's worker; discovery/download/analysis keep their own durable
queues. Constructing a manager never starts network activity.
"""
from collections import deque
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import queue
import shutil
import sqlite3
import threading
import time
import uuid

from discovery_learning import clean_config as clean_discovery_config
from online_jobs import _ProjectLock


SETTINGS_FILE = "continuous_learning.json"
EXTRA_DEFAULTS = {"interval_seconds": 60, "max_pending": 8, "min_free_gib": 2}
ALL_SITES = ["bilibili", "youku", "iqiyi"]
QUERY_TOPICS = {
    "animation": ("", "校园", "职场", "反转", "熊猫头", "家庭", "日常", "短篇"),
    "movie": ("", "人物对话", "喜剧片段", "动作表演", "表情", "经典桥段", "镜头", "日常生活"),
}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_config(value=None):
    if value is not None and not isinstance(value, dict):
        raise ValueError("持续学习设置格式无效")
    value = value or {}
    result = clean_discovery_config({**value, "animation_enabled": True, "movie_enabled": True,
                                     "sites": ALL_SITES, "auto_download": True})
    for key, low, high in (("interval_seconds", 10, 3600), ("max_pending", 1, 50), ("min_free_gib", 1, 1000)):
        item = value.get(key, EXTRA_DEFAULTS[key])
        if type(item) is not int or not low <= item <= high:
            raise ValueError(f"{key} 须为 {low}–{high} 的整数")
        result[key] = item
    # Do not generate more downloads than can fit in the active queue budget.
    result["max_total_downloads"] = min(result["max_total_downloads"], result["max_pending"])
    return result


class ContinuousLearningManager:
    def __init__(self, root, discovery, *, prepare=None, clock=time.monotonic, disk_usage=shutil.disk_usage):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.discovery = discovery
        if prepare is None:
            from local_learning_setup import prepare_local_learning_models
            prepare = prepare_local_learning_models
        self._prepare, self._clock, self._disk_usage = prepare, clock, disk_usage
        self._mutex = threading.RLock()
        self._stop = threading.Event()
        self._thread, self._lock = None, None
        self._results = queue.Queue()
        self._events, self._history = deque(maxlen=500), deque(maxlen=100)
        self._active, self._closed, self._prepared = False, False, False
        self._next_run, self._last_persist = 0., 0.
        self._empty_rounds = 0
        self._current_round = None
        self._owned_round_ids = []
        self._handled_rounds = set()
        self._generation = 0
        self._replacement_pending, self._replacement_due = False, 0.
        self._replacement_streak, self._last_round_started = 0, -float("inf")
        self._replacement_seen = set()
        self._site_cooldowns = {}
        self._config = clean_config(discovery.get_config())
        self._state = {"state": "idle", "phase": "idle", "message": "点击一键启动，自动准备本地模型并持续查找动画和电影",
                       "round_count": 0, "current_url": "", "model": "", "progress": {}, "last_error": ""}
        self._restore()

    @property
    def active(self):
        return self._active

    @property
    def enabled(self):
        return self._active

    @property
    def busy(self):
        return bool((self._thread and self._thread.is_alive()) or
                    (self._current_round and self.discovery.busy))

    @property
    def owned_round_ids(self):
        return list(self._owned_round_ids)

    def get_config(self):
        with self._mutex:
            return copy.deepcopy(self._config)

    def _restore(self):
        path = self.root/SETTINGS_FILE
        if not path.is_file():
            return
        try:
            if path.stat().st_size > 2*1024*1024:
                raise ValueError("持续学习状态过大")
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("version") != 1:
                raise ValueError("持续学习状态格式无效")
            self._config = clean_config(value.get("config"))
            count = value.get("round_count", 0)
            self._state["round_count"] = count if type(count) is int and 0 <= count <= 10**9 else 0
            ids = value.get("owned_round_ids", [])
            if isinstance(ids, list):
                self._owned_round_ids = [item for item in ids[-2000:] if isinstance(item, str) and len(item) <= 100]
            if value.get("state") != "idle":
                self._state.update(state="paused", phase="paused",
                    message="持续学习已暂停；点击一键启动可复用本地模型并继续，打开页面不会自动联网")
        except (OSError, ValueError, TypeError) as exc:
            self._state.update(state="paused", phase="paused", last_error=str(exc)[:500],
                               message="上次持续学习状态未能读取，点击一键启动可重新检查并开始")

    def _persist(self):
        # Only the owner writes; opening a second window must not reset an active run.
        if self._lock is None:
            return
        value = {"version": 1, "config": self._config, "state": self._state["state"],
                 "round_count": self._state["round_count"], "owned_round_ids": self._owned_round_ids[-2000:],
                 "current_round_id": self._current_round, "updated": _now()}
        target = self.root/SETTINGS_FILE
        temporary = target.with_name(target.name+"."+uuid.uuid4().hex+".tmp")
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        self._last_persist = self._clock()

    def _emit(self, kind, **data):
        event = {"type": kind, "timestamp": _now(), **data}
        self._events.append(event)
        self._history.append(event)

    def _set(self, state, message, **data):
        changes = {"state": state, "phase": state, "message": str(message)[:2000], **data}
        if any(self._state.get(key) != value for key, value in changes.items()):
            self._state.update(changes)
            self._emit("status", **changes)
            self._persist()

    def _report_setup(self, event):
        if self._stop.is_set():
            return
        if not isinstance(event, dict):
            event = {"message": str(event)}
        with self._mutex:
            # Keep observed byte counts/percent; never infer an overall model percentage.
            progress = {key: event[key] for key in ("completed", "total", "percent", "downloaded_bytes", "total_bytes", "speed", "eta")
                        if isinstance(event.get(key), (int, float)) and not isinstance(event.get(key), bool)}
            self._state.update(phase=str(event.get("phase", "preparing"))[:100],
                               message=str(event.get("message", "正在准备本地模型"))[:2000],
                               current_url=str(event.get("url", event.get("current_url", "")))[:2000],
                               model=str(event.get("model", self._state.get("model", "")))[:200], progress=progress)
            self._emit("preparation", **copy.deepcopy(self._state))

    def start(self, config=None):
        with self._mutex:
            if self._closed:
                raise ValueError("持续学习管理器已关闭")
            if self._active:
                return False
            if self.busy:
                raise ValueError("正在停止上次任务，请稍后再启动")
            if self.discovery.busy:
                raise ValueError("当前单轮查找尚未结束，请停止或等待后再启动持续学习")
            lock = _ProjectLock(self.root/".continuous-learning.lock")
            if lock.fd is None:
                raise ValueError("本工程已在另一个窗口持续学习，请勿重复启动")
            self._lock = lock
            try:
                self._config = clean_config(config if config is not None else self._config)
                self._active, self._prepared = True, False
                self._stop.clear()
                self._generation += 1
                self._current_round = None
                self._empty_rounds, self._next_run = 0, 0.
                self._replacement_pending, self._replacement_due = False, 0.
                self._replacement_streak = 0
                self._replacement_seen.clear()
                self._site_cooldowns.clear()
                self._set("preparing", "正在自动检查本地模型、语音转写与视频分析设置…", last_error="", progress={}, current_url="")
                generation = self._generation
                def work():
                    try:
                        result = self._prepare(self.root, report=self._report_setup, stop=self._stop.is_set)
                        self._results.put((generation, result, None))
                    except Exception as exc:
                        self._results.put((generation, None, str(exc) or type(exc).__name__))
                    finally:
                        if self._stop.is_set() and not self.discovery.busy:
                            with self._mutex:
                                self._release()
                self._thread = threading.Thread(target=work, name="continuous-local-setup", daemon=True)
                self._thread.start()
                return True
            except BaseException:
                self._active = False
                lock.close()
                self._lock = None
                raise

    def _release(self):
        if self._lock is not None:
            lock, self._lock = self._lock, None
            lock.close()

    def _stopped_state(self, state, message):
        """Stopping workers is more important than persisting their UI state."""
        try:
            self._set(state, message)
        except (OSError, ValueError) as exc:
            self._state.update(state=state, phase=state, message=message,
                               last_error=("停止状态未能保存："+str(exc))[:2000])
            self._emit("status", **copy.deepcopy(self._state))

    def _consume_setup(self):
        while True:
            try:
                generation, result, error = self._results.get_nowait()
            except queue.Empty:
                break
            if generation != self._generation:
                continue
            if not self._active or self._stop.is_set():
                continue
            if error:
                self._active = False
                self._set("failed", "本地模型准备未完成，可修复后再次一键启动："+error, last_error=error)
            else:
                self._prepared = True
                try:
                    self.discovery.resume_pending(self._owned_round_ids)
                except (OSError, ValueError) as exc:
                    self._active, self._prepared = False, False
                    self._set("failed", "本地模型已准备，但历史任务暂不能恢复，请稍后重试："+str(exc), last_error=str(exc))
                    continue
                self._set("waiting", "本地模型已就绪，正在接通下载与学习队列", progress={}, current_url="")
                self._emit("models-ready", result=result)

    def _round_config(self):
        config = clean_discovery_config(self._config)
        index = self._state["round_count"]
        offset = index % len(ALL_SITES)
        config["sites"] = ALL_SITES[offset:]+ALL_SITES[:offset]
        # Keep all authorized sites, but prefer sources that have not recently
        # rejected a download. A restricted site becomes eligible again later.
        now = self._clock()
        config["sites"].sort(key=lambda site: self._site_cooldowns.get(site, 0) > now)
        for category, topics in QUERY_TOPICS.items():
            topic = topics[(index // len(ALL_SITES)) % len(topics)]
            base = self._config[category+"_query"]
            # Public discovery accepts at most 200 characters.
            config[category+"_query"] = (base[:198-len(topic)]+(" "+topic if topic else "")).strip()
        return config

    def notify_skipped(self, job_id, *, site="", reason="", title="", url=""):
        """Coalesce skipped downloads into one bounded replacement search."""
        with self._mutex:
            if not self._active or self._closed or not job_id or job_id in self._replacement_seen:
                return False
            self._replacement_seen.add(job_id)
            now = self._clock()
            if site in ALL_SITES:
                self._site_cooldowns[site] = now+300
            if not self._replacement_pending:
                delay = min(300, 10 * 2**min(5, self._replacement_streak))
                self._replacement_due = max(now+delay, self._last_round_started+10)
                self._replacement_pending = True
            self._emit("skipped", job_id=job_id, site=site, title=title, url=url,
                       message="已跳过受限视频，将继续处理队列并补找其他素材："+str(reason)[:1000])
            return True

    def notify_download_success(self, *, site=""):
        with self._mutex:
            self._replacement_streak = 0
            self._site_cooldowns.pop(site, None)

    @staticmethod
    def _pending(workload):
        return sum(max(0, value) for key in ("pending_downloads", "pending_learning")
                   if type(value := workload.get(key, 0)) is int)

    def tick(self, workload=None):
        workload = workload if isinstance(workload, dict) else {}
        with self._mutex:
            self._consume_setup()
            if not self._active:
                if not self.busy:
                    if self._state["state"] == "stopping":
                        self._stopped_state("paused", "持续学习已暂停，已下载内容和学习成果保留")
                    self._release()
                return self.snapshot()
            if not self._prepared:
                return self.snapshot()
            if self.discovery.busy:
                if self._current_round:
                    row = self.discovery.get_round(self._current_round) or {}
                    activity = row.get("active", {})
                    self._set("searching", activity.get("message") or f"第 {self._state['round_count']} 轮正在查找三站动画与电影",
                              phase=activity.get("phase", "searching"), current_url=activity.get("url", ""))
                return self.snapshot()
            if self._current_round and self._current_round not in self._handled_rounds:
                row = self.discovery.get_round(self._current_round)
                if row:
                    self._handled_rounds.add(self._current_round)
                    unsuccessful = row.get("status") in {"failed", "partial"} or not row.get("candidate_count")
                    self._empty_rounds = min(5, self._empty_rounds+1) if unsuccessful else 0
                    interval = min(900, self._config["interval_seconds"]*(2**self._empty_rounds))
                    self._next_run = self._clock()+interval
                    self._set("waiting", row.get("message", "本轮查找已结束")+f"；{interval} 秒后继续查找",
                              last_error="；".join(str(value) for value in row.get("errors", {}).values())[:2000])
            pending = self._pending(workload)
            if workload.get("paused"):
                self._set("backpressure", "下载或学习队列已暂停；继续队列后会自动恢复查找")
                return self.snapshot()
            if pending >= self._config["max_pending"] or workload.get("training_busy"):
                self._set("backpressure", f"等待已有任务处理 · 下载/分析待办 {pending} 项；暂不增加下载")
                return self.snapshot()
            try:
                free = workload.get("free_bytes")
                if type(free) is not int:
                    free = self._disk_usage(self.root).free
                if free < self._config["min_free_gib"]*1024**3:
                    self._set("disk-low", f"可用空间不足 {self._config['min_free_gib']} GiB，暂停新增查找；腾出空间后自动继续")
                    return self.snapshot()
            except OSError as exc:
                self._set("disk-low", "无法检查缓存空间，暂缓新增查找："+str(exc), last_error=str(exc))
                return self.snapshot()
            due = min(self._next_run, self._replacement_due) if self._replacement_pending else self._next_run
            if self._clock() < due:
                if self._replacement_pending:
                    self._set("waiting", "已跳过受限视频，等待补找其他素材；已排队的下载和学习继续运行")
                if self._state["state"] in {"backpressure", "disk-low"}:
                    self._set("waiting", "队列和空间已恢复，等待下一轮查找")
                return self.snapshot()
            try:
                config = self._round_config()
                config["max_total_downloads"] = max(1, min(config["max_total_downloads"], self._config["max_pending"]-pending))
                identifier = self.discovery.start(config)
                self._last_round_started = self._clock()
                if self._replacement_pending:
                    self._replacement_streak = min(5, self._replacement_streak+1)
                    self._replacement_pending = False
                self._current_round = identifier
                self._owned_round_ids = [*self._owned_round_ids, identifier][-2000:]
                self._state["round_count"] += 1
                self._set("searching", f"第 {self._state['round_count']} 轮 · {config['animation_query']} / {config['movie_query']}",
                          last_error="", current_url="", progress={})
                self._emit("round-started", round_id=identifier, config=config)
            except (OSError, ValueError) as exc:
                self._next_run = self._clock()+self._config["interval_seconds"]
                if self._replacement_pending:
                    self._replacement_due = self._next_run
                self._set("waiting", "本轮暂未启动，将自动重试："+str(exc), last_error=str(exc))
            return self.snapshot()

    def stop(self):
        with self._mutex:
            self._active = False
            self._replacement_pending = False
            self._stop.set()
            if self._lock is not None:
                try:
                    try:
                        self.discovery.stop()
                    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                        self._state["last_error"] = str(exc)[:2000]
                    self._stopped_state("stopping" if self.busy else "paused", "已停止新增查找，正在暂停模型准备、下载与学习任务")
                finally:
                    if not self.busy:
                        try:
                            self._release()
                        except OSError as exc:
                            self._state["last_error"] = ("停止后释放锁失败："+str(exc))[:2000]
            return self.snapshot()

    def close(self):
        try:
            self.stop()
        finally:
            self._closed = True

    def drain_events(self):
        with self._mutex:
            result = list(self._events)
            self._events.clear()
            return result

    def snapshot(self):
        with self._mutex:
            due = min(self._next_run, self._replacement_due) if self._replacement_pending else self._next_run
            remaining = max(0, int(due-self._clock()+.999)) if self._active and self._prepared else 0
            return {**copy.deepcopy(self._state), "enabled": self._active, "active": self._active,
                    "busy": self.busy, "next_run_in": remaining, "next_run_seconds": remaining,
                    "models_ready": self._active and self._prepared,
                    "replacement_pending": self._replacement_pending,
                    "round_id": self._current_round, "current_round_id": self._current_round,
                    "owned_round_ids": self.owned_round_ids, "url": self._state["current_url"],
                    "config": copy.deepcopy(self._config), "events": list(self._history)}
