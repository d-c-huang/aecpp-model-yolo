"""Read-only progress snapshots for bounded online discovery and learning."""
from datetime import datetime
import math
import tkinter as tk
from tkinter import ttk
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import webbrowser

import theme
from online_window import (format_bytes, format_duration, continuous_active, continuous_summary, ContinuousResultsPanel)


PROVIDERS = {"网页搜索": "web"}
SITES = {"bilibili": "哔哩哔哩", "youku": "优酷", "iqiyi": "爱奇艺"}
CATEGORIES = {"animation": "沙雕动画", "movie": "电影"}
STATES = {"discovered": "已找到", "dispatching": "提交下载", "queued": "等待下载", "downloading": "正在下载",
          "downloaded": "已入库", "learning": "正在学习", "completed": "学习完成",
          "needs_input": "需要补充", "failed": "失败", "ignored": "未加入", "cancelled": "已取消",
          "paused": "已暂停", "waiting": "等待处理", "searching": "正在搜索",
          "running": "正在处理", "stopped": "已停止", "interrupted": "上次已中断",
          "skipped": "已跳过", "partial": "部分完成"}
FILTERS = {"全部状态": None, **{label: state for state, label in STATES.items()}}
PHASES = {"query": "提交搜索", "fetch": "读取来源", "parse": "整理结果", "searching": "查找视频",
          "discovered": "发现素材", "resolving": "解析视频", "downloading": "下载视频",
          "merging": "合并音画", "probing": "校验视频", "importing": "导入工程",
          "analysing": "分析字幕与画面", "snapshot": "准备学习样本", "learning": "学习字幕节奏",
          "training": "电影时序训练", "queued": "等待处理", "completed": "完成",
          "failed": "失败", "stopped": "已停止", "needs_input": "需要补充资料",
          "skipped": "已跳过", "restricted": "访问受限", "partial": "部分完成"}
LEARNING = {"story-reference": "字幕节奏参考", "movie-temporal": "电影时序训练",
            "story-and-movie": "字幕节奏参考 + 电影时序训练"}


def safe_url(value):
    try:
        parsed = urlsplit(str(value or ""))
        return str(value) if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password else ""
    except ValueError:
        return ""


def display_url(value):
    """Show the source without copying credentials from signed query strings."""
    value = safe_url(value)
    if not value:
        return "—"
    parsed = urlsplit(value)
    query = [(key, "已隐藏" if any(word in key.lower() for word in ("token", "signature", "secret", "password", "auth", "api_key")) else content)
             for key, content in parse_qsl(parsed.query, keep_blank_values=True)]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def finite(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None


def progress_text(record):
    percent = finite(record.get("percent", record.get("progress")))
    done, total, speed = (finite(record.get(key)) for key in ("downloaded_bytes", "total_bytes", "speed"))
    fields = []
    if percent is not None:
        fields.append(f"{min(percent, 100):.1f}%")
    if done is not None:
        fields.append(format_bytes(done) + (" / " + format_bytes(total) if total else ""))
    if speed:
        fields.append(format_bytes(speed)+"/s")
    eta = finite(record.get("eta"))
    if eta is not None:
        fields.append("剩余 "+format_duration(eta))
    return " · ".join(fields)


def video_site(record):
    if record.get("site") in SITES:
        return SITES[record["site"]]
    host = urlsplit(safe_url(record.get("url"))).hostname or ""
    for key, domain in (("bilibili", "bilibili.com"), ("youku", "youku.com"), ("iqiyi", "iqiyi.com")):
        if host == domain or host.endswith("."+domain):
            return SITES[key]
    return host


def event_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")
    except (ValueError, TypeError):
        return str(value or "")[:19]


class DiscoveryWindow:
    def __init__(self, parent, manager, *, on_close=None, on_open_url=None, on_open_learning=None,
                 on_queue_selected=None, can_write=lambda: True, activity=lambda: {}, container=None,
                 on_start_continuous=None, on_stop_continuous=None, continuous_status=lambda:{},on_dashboard=None):
        self.manager, self.on_close = manager, on_close
        self.on_dashboard = on_dashboard
        self.on_open_url, self.on_open_learning = on_open_url, on_open_learning
        self.on_queue_selected, self.can_write, self.activity = on_queue_selected, can_write, activity
        self.on_start_continuous, self.on_stop_continuous = on_start_continuous, on_stop_continuous
        self.continuous_status, self._continuous_snapshot = continuous_status, {}
        self._single_round_expanded = not bool(on_start_continuous)
        self.embedded = container is not None
        self._closed, self._timer = False, None
        self._snapshot, self._candidates, self._events = {}, {}, {}
        self._rows_signature = self._events_signature = self._detail_signature = None
        self.win = ttk.Frame(container) if self.embedded else tk.Toplevel(parent)
        if not self.embedded:
            self.win.title("联网找素材 · 来源与学习进度")
            self.win.geometry("1100x780")
            self.win.minsize(960, 700)
            self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<Destroy>", self._destroyed, add="+")
        defaults = {"animation_enabled": True, "movie_enabled": True,
                    "animation_query": "沙雕动画 搞笑", "movie_query": "喜剧电影",
                    "animation_provider": "web", "movie_provider": "web", "sites": list(SITES),
                    "max_per_category": 3, "max_total_downloads": 4, "auto_download": True}
        try:
            defaults.update(manager.get_config() or {})
        except (AttributeError, OSError, ValueError):
            pass
        self.animation_enabled = tk.BooleanVar(self.win, bool(defaults["animation_enabled"]))
        self.movie_enabled = tk.BooleanVar(self.win, bool(defaults["movie_enabled"]))
        self.animation_query = tk.StringVar(self.win, str(defaults["animation_query"]))
        self.movie_query = tk.StringVar(self.win, str(defaults["movie_query"]))
        self.sites = {key: tk.BooleanVar(self.win, key in defaults.get("sites", list(SITES))) for key in SITES}
        self.max_per_category = tk.StringVar(self.win, str(defaults["max_per_category"]))
        self.max_total_downloads = tk.StringVar(self.win, str(defaults["max_total_downloads"]))
        self.auto_download = tk.BooleanVar(self.win, bool(defaults["auto_download"]))
        self.summary = tk.StringVar(self.win, "尚未开始查找")
        self.status = tk.StringVar(self.win, "设置关键词后开始。每轮按数量上限查找，自动下载后进入对应学习任务。")
        self.current_source = tk.StringVar(self.win, "当前来源：尚未访问")
        self.current_url = tk.StringVar(self.win, "")
        self.download_activity = tk.StringVar(self.win, "当前没有下载任务")
        self.learning_activity = tk.StringVar(self.win, "当前没有学习任务")
        self.continuous_summary = tk.StringVar(self.win, "自动准备本地模型，持续从三站查找和学习。")
        self.search = tk.StringVar(self.win)
        self.filter = tk.StringVar(self.win, "全部状态")
        self._build()
        self._search_trace = self.search.trace_add("write", lambda *_: self._render_candidates())
        self._filter_trace = self.filter.trace_add("write", lambda *_: self._render_candidates())
        self.refresh()

    def _build(self):
        body = ttk.Frame(self.win, padding=(14, 10))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(4, weight=1)
        heading = ttk.Frame(body)
        heading.grid(row=0, column=0, sticky="ew")
        heading.columnconfigure(0, weight=1)
        ttk.Label(heading, text="联网找素材", style="Title.TLabel").grid(row=0,column=0,sticky="w")
        if self.on_dashboard:
            ttk.Button(heading,text="学习仪表盘",command=self.on_dashboard).grid(row=0,column=2,padx=(10,0))
        ttk.Label(heading, textvariable=self.summary, style="Muted.TLabel").grid(row=0,column=1,sticky="e")
        if self.on_start_continuous:
            bar = ttk.Frame(heading)
            bar.grid(row=1,column=0,columnspan=2,sticky="ew",pady=(7,3))
            self.continuous_start_button = ttk.Button(bar,text="一键启动自动学习",command=self._start_continuous,style="Primary.TButton")
            self.continuous_start_button.pack(side="left")
            self.continuous_stop_button = ttk.Button(bar,text="停止自动学习",command=self._stop_continuous,state="disabled")
            self.continuous_stop_button.pack(side="left",padx=(8,0))
            self.single_round_toggle = ttk.Button(bar,text="单轮查找设置 ▸",command=self._toggle_single_round,style="Compact.TButton")
            self.single_round_toggle.pack(side="right")
            ttk.Label(heading,textvariable=self.continuous_summary,style="Muted.TLabel",wraplength=920,
                      justify="left").grid(row=2,column=0,columnspan=2,sticky="ew",pady=(2,4))
            self.results_panel = ContinuousResultsPanel(heading)
            self.results_panel.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(2, 3))
        form = ttk.Frame(body)
        self.single_round_form = form
        form.grid(row=1, column=0, sticky="ew", pady=(8, 7))
        form.columnconfigure(1, weight=1)
        self.setting_controls = []
        for row, (label, enabled, query) in enumerate((
                ("沙雕动画", self.animation_enabled, self.animation_query),
                ("电影", self.movie_enabled, self.movie_query))):
            check = ttk.Checkbutton(form, text=label, variable=enabled)
            check.grid(row=row, column=0, sticky="w", padx=(0, 10), pady=3)
            entry = ttk.Entry(form, textvariable=query)
            entry.grid(row=row, column=1, columnspan=2, sticky="ew", pady=3)
            self.setting_controls.extend((check, entry))
        sites = ttk.Frame(form)
        sites.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        ttk.Label(sites, text="视频网站").pack(side="left", padx=(0, 12))
        for key, label in SITES.items():
            check = ttk.Checkbutton(sites, text=label, variable=self.sites[key])
            check.pack(side="left", padx=(0, 16))
            self.setting_controls.append(check)
        ttk.Label(sites, text="搜索引擎用于查找这三个站点的视频原页", style="Muted.TLabel").pack(side="left")
        limits = ttk.Frame(form)
        limits.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(5, 0))
        ttk.Label(limits, text="每类最多").pack(side="left")
        self.limit_box = ttk.Spinbox(limits, from_=1, to=10, textvariable=self.max_per_category, width=3)
        self.limit_box.pack(side="left", padx=(5, 10))
        ttk.Label(limits, text="本轮最多下载").pack(side="left")
        self.total_box = ttk.Spinbox(limits, from_=1, to=20, textvariable=self.max_total_downloads, width=3)
        self.total_box.pack(side="left", padx=(5, 10))
        self.download_check = ttk.Checkbutton(limits, text="自动下载并学习", variable=self.auto_download)
        self.download_check.pack(side="left")
        self.start_button = ttk.Button(limits, text="开始自动找", command=self._start, style="Primary.TButton")
        self.start_button.pack(side="right")
        self.stop_button = ttk.Button(limits, text="停止查找 / 新下载", command=self._stop)
        self.stop_button.pack(side="right", padx=(8, 8))
        self.setting_controls.extend((self.limit_box, self.total_box, self.download_check))
        if not self._single_round_expanded:
            form.grid_remove()
        source = ttk.Frame(body)
        source.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        source.columnconfigure(0, weight=1)
        ttk.Label(source, textvariable=self.current_source, style="Accent.TLabel").grid(row=0, column=0, sticky="w")
        self.current_url_entry = ttk.Entry(source, textvariable=self.current_url, state="readonly")
        self.current_url_entry.grid(row=1, column=0, sticky="ew", pady=(3, 0))
        self.source_button = ttk.Button(source, text="打开当前来源", command=self._open_current_source, style="Compact.TButton")
        self.source_button.grid(row=1, column=1, padx=(8, 0), pady=(3, 0))
        running = ttk.Frame(body)
        running.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        running.columnconfigure(0, weight=1, uniform="activity")
        running.columnconfigure(1, weight=1, uniform="activity")
        ttk.Label(running, textvariable=self.download_activity, style="Muted.TLabel", wraplength=450,
                  justify="left").grid(row=0, column=0, sticky="new", padx=(0, 10))
        ttk.Label(running, textvariable=self.learning_activity, style="Muted.TLabel", wraplength=450,
                  justify="left").grid(row=0, column=1, sticky="new")
        self.notebook = ttk.Notebook(body)
        self.notebook.grid(row=4, column=0, sticky="nsew")
        materials = ttk.Frame(self.notebook)
        materials.rowconfigure(1, weight=1)
        materials.columnconfigure(0, weight=1)
        self.notebook.add(materials, text="发现素材与进度")
        find = ttk.Frame(materials, padding=(0, 5))
        find.grid(row=0, column=0, sticky="ew")
        ttk.Label(find, text="筛选标题 / 来源").pack(side="left", padx=(4, 8))
        ttk.Entry(find, textvariable=self.search).pack(side="left", fill="x", expand=True)
        ttk.Combobox(find, textvariable=self.filter, values=list(FILTERS), state="readonly", width=11).pack(side="left", padx=(8, 0))
        self.queue_button = ttk.Button(find, text="加入所选", command=self._queue_selected, style="Accent.TButton")
        self.queue_button.pack(side="right", padx=(8, 0))
        listing = ttk.Frame(materials)
        listing.grid(row=1, column=0, sticky="nsew")
        listing.columnconfigure(0, weight=1)
        listing.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(listing, columns=("category", "title", "provider", "state", "progress"),
                                 show="headings", selectmode="extended", height=5)
        for key, label, width in (("category", "类型", 75), ("title", "视频 / 电影", 245), ("provider", "视频网站", 115),
                                  ("state", "当前状态", 110), ("progress", "下载 / 学习进度", 330)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=65, stretch=key in {"title", "progress"})
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(listing, command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)
        for state, color in (("failed", theme.RED), ("completed", theme.TEAL), ("needs_input", theme.ACCENT),
                             ("partial", theme.ACCENT), ("skipped", theme.MUTED)):
            self.tree.tag_configure(state, foreground=color)
        self.tree.bind("<<TreeviewSelect>>", lambda _: self._selection_changed())
        logs = ttk.Frame(self.notebook)
        logs.columnconfigure(0, weight=1)
        logs.rowconfigure(0, weight=1)
        self.notebook.add(logs, text="访问与任务日志")
        self.log_tree = ttk.Treeview(logs, columns=("time", "phase", "query", "source", "message"), show="headings", height=5)
        for key, label, width in (("time", "时间", 78), ("phase", "阶段", 105), ("query", "关键词 / 标题", 180),
                                  ("source", "访问来源", 250), ("message", "说明", 265)):
            self.log_tree.heading(key, text=label)
            self.log_tree.column(key, width=width, minwidth=65, stretch=key in {"source", "message"})
        self.log_tree.grid(row=0, column=0, sticky="nsew")
        logs_scroll = ttk.Scrollbar(logs, command=self.log_tree.yview)
        logs_scroll.grid(row=0, column=1, sticky="ns")
        self.log_tree.configure(yscrollcommand=logs_scroll.set)
        self.log_tree.bind("<<TreeviewSelect>>", lambda _: self._event_selected())
        self.notebook.bind("<<NotebookTabChanged>>", lambda _: self._tab_changed())
        detail = ttk.Frame(body)
        detail.grid(row=5, column=0, sticky="ew", pady=(7, 0))
        detail.columnconfigure(0, weight=1)
        self.details = tk.Text(detail, height=4, wrap="word", bg=theme.FIELD, fg=theme.MUTED, relief="flat",
                               padx=8, pady=5, state="disabled", font=(theme.FONT, 9))
        self.details.grid(row=0, column=0, sticky="ew")
        detail_scroll = ttk.Scrollbar(detail, command=self.details.yview)
        detail_scroll.grid(row=0, column=1, sticky="ns")
        self.details.configure(yscrollcommand=detail_scroll.set)
        actions = ttk.Frame(body)
        actions.grid(row=6, column=0, sticky="ew", pady=(6, 0))
        self.open_video_button = ttk.Button(actions, text="打开视频原页", command=lambda: self._open_selected("url"), style="Compact.TButton")
        self.open_video_button.pack(side="left")
        self.open_source_button = ttk.Button(actions, text="查看发现出处", command=lambda: self._open_selected("source_url"), style="Compact.TButton")
        self.open_source_button.pack(side="left", padx=8)
        self.copy_button = ttk.Button(actions, text="复制来源链接", command=self._copy_selected, style="Compact.TButton")
        self.copy_button.pack(side="left")
        self.learning_button = ttk.Button(actions, text="查看学习任务", command=self._open_learning, style="Compact.TButton")
        self.learning_button.pack(side="right")
        ttk.Label(body, textvariable=self.status, style="Muted.TLabel", wraplength=920,
                  justify="left").grid(row=7, column=0, sticky="ew", pady=(7, 0))

    def _perform(self, action, message=None, *, write=True):
        if write and not self.can_write():
            self.status.set("工程正在迁移或整理缓存，请完成后再开始。")
            return False
        try:
            result = action()
            if message:
                self.status.set(message)
            self.refresh_now()
            return result
        except Exception as exc:
            self.status.set(f"操作未完成：{exc}")
            return False

    def _start(self):
        if continuous_active(self.continuous_status() or {}):
            self.status.set("持续自动学习正在运行；停止后可使用单轮查找设置。")
            return False
        def start():
            config = {"animation_enabled": bool(self.animation_enabled.get()), "movie_enabled": bool(self.movie_enabled.get()),
                      "animation_query": self.animation_query.get().strip(), "movie_query": self.movie_query.get().strip(),
                      "animation_provider": "web", "movie_provider": "web",
                      "sites": [key for key, value in self.sites.items() if value.get()],
                      "max_per_category": int(self.max_per_category.get()), "max_total_downloads": int(self.max_total_downloads.get()),
                      "auto_download": bool(self.auto_download.get())}
            if not 1 <= config["max_per_category"] <= 10 or not 1 <= config["max_total_downloads"] <= 20:
                raise ValueError("每类数量须为 1–10，本轮下载上限须为 1–20")
            if not config["animation_enabled"] and not config["movie_enabled"]:
                raise ValueError("请至少选择沙雕动画或电影")
            if not config["sites"]:
                raise ValueError("请至少选择一个视频网站")
            for category in ("animation", "movie"):
                if config[category+"_enabled"] and not config[category+"_query"]:
                    raise ValueError("请填写所选类型的搜索关键词")
            return self.manager.start(config)
        self._perform(start, "已开始本轮查找；来源与进度会持续更新。")

    def _stop(self):
        if continuous_active(self.continuous_status() or {}) and self.on_stop_continuous:
            return self._stop_continuous()
        self._perform(self.manager.stop, "已停止查找及尚未派发的新下载；已运行的下载和学习可在各自任务中停止。", write=False)

    def _toggle_single_round(self):
        if continuous_active(self.continuous_status() or {}):
            return
        self._single_round_expanded = not self._single_round_expanded
        self.single_round_form.grid() if self._single_round_expanded else self.single_round_form.grid_remove()
        self.single_round_toggle.configure(text="单轮查找设置 ▾" if self._single_round_expanded else "单轮查找设置 ▸")

    def _start_continuous(self):
        if not self.on_start_continuous or continuous_active(self.continuous_status() or {}):
            return False
        return self._perform(self.on_start_continuous)

    def _stop_continuous(self):
        if not self.on_stop_continuous:
            return False
        return self._perform(self.on_stop_continuous,
            "正在停止自动学习；下载与分析将暂停，已下载内容和学习成果保留。",write=False)

    def refresh_now(self):
        if self._timer is not None:
            self.win.after_cancel(self._timer)
            self._timer = None
        self.refresh()

    def refresh(self):
        if self._closed:
            return
        try:
            self._snapshot = self.manager.snapshot()
            candidates = self._snapshot.get("candidates", [])
            self._candidates = {str(row["id"]): row for row in candidates}
            active = self._snapshot.get("active") or {}
            busy = bool(self._snapshot.get("busy", self.manager.busy))
            continuous = self.continuous_status() or {}
            self._continuous_snapshot = continuous
            automated = continuous_active(continuous)
            if self.on_start_continuous:
                self.continuous_summary.set(continuous_summary(continuous))
                self.results_panel.update_snapshot(continuous)
                self.continuous_start_button.configure(state="disabled" if automated or not self.can_write() else "normal",
                    text="继续自动学习" if continuous.get("state") in {"paused","failed"} else "一键启动自动学习")
                self.continuous_stop_button.configure(state="normal" if automated and self.on_stop_continuous else "disabled")
                self.single_round_toggle.configure(state="disabled" if automated else "normal")
                if automated:
                    self._single_round_expanded = False
                    self.single_round_form.grid_remove()
                    self.single_round_toggle.configure(text="单轮查找设置 ▸")
            states = [row.get("state", "discovered") for row in candidates]
            self.summary.set(f"{len(candidates)} 条素材 · {states.count('downloading')} 下载中 · {states.count('learning')} 学习中 · {states.count('completed')} 已完成"
                             + (f" · {states.count('partial')} 部分完成" if "partial" in states else "")
                             + (f" · {states.count('skipped')} 已跳过" if "skipped" in states else ""))
            self.start_button.configure(state="disabled" if busy or automated or not self.can_write() else "normal")
            for widget in self.setting_controls:
                widget.configure(state="disabled" if busy or automated else "readonly" if isinstance(widget, ttk.Combobox) else "normal")
            self.stop_button.configure(state="normal" if busy or any(state in {"discovered", "queued"} for state in states) else "disabled")
            phase = active.get("phase", "")
            provider = active.get("provider", "")
            label = next((label for label, key in PROVIDERS.items() if key == provider), provider)
            text = " · ".join(str(value) for value in (PHASES.get(phase, phase), label,
                SITES.get(active.get("site"), ""), active.get("query") or active.get("title")) if value)
            self.current_source.set(("当前来源：" if busy else "最近访问：")+(text or "尚未开始"))
            self._active_url = safe_url(active.get("url") or active.get("source_url"))
            if automated and continuous.get("state") in {"preparing","stopping"}:
                self.current_source.set("当前阶段："+str(continuous.get("message") or continuous.get("phase") or "准备本地模型")[:120])
                self._active_url = safe_url(continuous.get("current_url") or continuous.get("url"))
            self.current_url.set(display_url(self._active_url) if self._active_url else "")
            self.source_button.configure(state="normal" if self._active_url else "disabled")
            activity = self.activity() or {}
            self.download_activity.set(self._activity_text("正在下载", activity.get("current_downloads", [])))
            self.learning_activity.set(self._activity_text("正在学习", activity.get("current_learning", []), learning=True))
            self._render_candidates()
            if automated:
                self.queue_button.configure(state="disabled")
            self._render_events(self._snapshot.get("events", []))
            self.learning_button.configure(state="normal" if self.on_open_learning else "disabled")
        except Exception as exc:
            self.status.set(f"暂时无法刷新进度：{exc}")
        finally:
            if not self._closed:
                self._timer = self.win.after(700, self.refresh)

    @staticmethod
    def _activity_text(label, rows, *, learning=False):
        if isinstance(rows, dict):
            rows = [rows]
        if not rows:
            return label+"：当前没有任务"
        lines = []
        for row in rows[:2]:
            kind = LEARNING.get(row.get("kind", row.get("learning_kind", "")), "") if learning else ""
            title = str(row.get("title") or "未命名任务")[:70]
            message = progress_text(row) or str(row.get("message") or PHASES.get(row.get("phase"), row.get("phase", "")))
            source = safe_url(row.get("source_url"))
            host = urlsplit(source).hostname if source else ""
            lines.append(" · ".join(value for value in (title, kind or host) if value))
            lines.append(message[:110])
        if len(rows) > 2:
            lines.append(f"另有 {len(rows)-2} 项任务，详见素材进度。")
        return label+"：\n"+"\n".join(lines)

    def _render_candidates(self):
        if self._closed:
            return
        needle = self.search.get().strip().casefold()
        state_filter = FILTERS.get(self.filter.get())
        rows = [row for row in self._candidates.values()
                if (state_filter is None or row.get("state") == state_filter) and (not needle or needle in
                " ".join(str(row.get(key, "")) for key in ("title", "url", "source_url", "provider", "detail", "skip_reason", "message")).casefold())]
        wanted = {str(row["id"]) for row in rows}
        for iid in self.tree.get_children():
            if iid not in wanted:
                self.tree.delete(iid)
        for index, row in enumerate(rows):
            iid, state = str(row["id"]), row.get("state", "discovered")
            provider = video_site(row)
            detail = progress_text(row) or row.get("detail") or row.get("message", "")
            if state == "skipped":
                detail = row.get("skip_reason") or row.get("detail") or row.get("message") or "等待跳过原因"
            elif state == "partial":
                detail = row.get("detail") or row.get("message") or "已有部分成果，尚需处理"
            values = (CATEGORIES.get(row.get("category"), row.get("category", "")), row.get("title") or row.get("url", ""),
                      provider, STATES.get(state, state), detail)
            if self.tree.exists(iid):
                if tuple(self.tree.item(iid, "values")) != tuple(str(value) for value in values):
                    self.tree.item(iid, values=values, tags=(state,))
                self.tree.move(iid, "", index)
            else:
                self.tree.insert("", index, iid=iid, values=values, tags=(state,))
        self._selection_changed()

    def _render_events(self, events):
        signature = repr(events)
        if signature == self._events_signature:
            return
        self._events_signature = signature
        selected = self.log_tree.selection()
        self._events = {}
        self.log_tree.delete(*self.log_tree.get_children())
        for index, event in enumerate(events):
            iid = str(event.get("id", index))
            self._events[iid] = event
            phase = event.get("phase", event.get("state", ""))
            self.log_tree.insert("", "end", iid=iid, values=(event_time(event.get("timestamp", event.get("created", ""))),
                PHASES.get(phase, phase), event.get("query") or event.get("title", ""),
                display_url(event.get("url") or event.get("source_url")), event.get("message", "")))
        if selected and self.log_tree.exists(selected[0]):
            self.log_tree.selection_set(selected[0])

    def _selected(self):
        return [self._candidates[iid] for iid in self.tree.selection() if iid in self._candidates]

    def _selection_changed(self):
        selected = self._selected()
        row = selected[0] if selected else None
        self.open_video_button.configure(state="normal" if row and safe_url(row.get("url")) else "disabled")
        self.open_source_button.configure(state="normal" if row and safe_url(row.get("source_url")) else "disabled")
        self.copy_button.configure(state="normal" if row and safe_url(row.get("url")) else "disabled")
        eligible = [row for row in selected if row.get("state") in {"discovered", "ignored", "failed", "cancelled"}]
        self.queue_button.configure(state="normal" if eligible and self.on_queue_selected and self.can_write()
                                    and not continuous_active(self._continuous_snapshot) else "disabled")
        if self.notebook.index("current") == 1 and self.log_tree.selection():
            return
        if row:
            lines = [str(row.get("title", "")), "类型："+CATEGORIES.get(row.get("category"), str(row.get("category", ""))),
                     "状态："+STATES.get(row.get("state"), str(row.get("state", ""))),
                     "学习内容："+LEARNING.get(row.get("learning_kind"), "字幕节奏参考 + 电影时序训练" if row.get("category")=="movie" else "字幕节奏参考"),
                     "视频网站："+video_site(row),
                     "视频原页："+display_url(row.get("url")), "发现出处："+display_url(row.get("source_url"))]
            if row.get("query"):
                lines.append("搜索关键词："+str(row["query"]))
            if row.get("detail") or row.get("message"):
                lines.append(str(row.get("detail") or row.get("message")))
            if row.get("state") == "skipped" and row.get("skip_reason"):
                lines.append("跳过原因：" + str(row["skip_reason"]))
            self._set_detail("\n".join(lines))
        elif not self.log_tree.selection():
            self._set_detail("选择素材查看原网页、发现出处和当前学习内容；访问日志保留每一步的实际来源与结果。")

    def _set_detail(self, value):
        if value == self._detail_signature:
            return
        self._detail_signature = value
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", value)
        self.details.configure(state="disabled")

    def _event_selected(self):
        selection = self.log_tree.selection()
        event = self._events.get(selection[0]) if selection else None
        if event:
            self._set_detail("\n".join(str(value) for value in (event.get("timestamp", event.get("created", "")),
                event.get("phase", ""), event.get("query") or event.get("title"),
                display_url(event.get("url") or event.get("source_url")), event.get("message")) if value))

    def _tab_changed(self):
        if self._closed or not hasattr(self, "details"):
            return
        if self.notebook.index("current") == 1:
            self._event_selected()
        else:
            self._selection_changed()

    def _queue_selected(self):
        if continuous_active(self.continuous_status() or {}):
            self.status.set("持续模式会自动安排下载；停止后可手动加入所选素材。")
            return False
        ids = [str(row["id"]) for row in self._selected()
               if row.get("state") in {"discovered", "ignored", "failed", "cancelled"}]
        if ids and self.on_queue_selected:
            self._perform(lambda: self.on_queue_selected(ids), "所选素材已提交；下载与学习状态会继续更新。")

    def _open_url(self, value):
        value = safe_url(value)
        if value:
            self._perform(lambda: (self.on_open_url or webbrowser.open)(value), write=False)

    def _open_current_source(self):
        self._open_url(getattr(self, "_active_url", ""))

    def _open_selected(self, key):
        rows = self._selected()
        if rows:
            self._open_url(rows[0].get(key))

    def _copy_selected(self):
        rows = self._selected()
        if rows:
            self.win.clipboard_clear()
            self.win.clipboard_append(display_url(rows[0].get("url")))
            self.status.set("已复制来源链接。")

    def _open_learning(self):
        if self.on_open_learning:
            self._perform(self.on_open_learning, write=False)

    def show(self):
        if not self._closed:
            if not self.embedded:
                self.win.deiconify()
            self.win.lift()

    def _destroyed(self, event):
        if event.widget == self.win:
            self._finish_close()

    def _finish_close(self):
        if self._closed:
            return
        self._closed = True
        if self._timer is not None:
            try:
                self.win.after_cancel(self._timer)
            except tk.TclError:
                pass
            self._timer = None
        if getattr(self, "_search_trace", None):
            try:
                self.search.trace_remove("write", self._search_trace)
            except tk.TclError:
                pass
            self._search_trace = None
        if getattr(self, "_filter_trace", None):
            try:
                self.filter.trace_remove("write", self._filter_trace)
            except tk.TclError:
                pass
            self._filter_trace = None
        if self.on_close:
            self.on_close()

    def close(self):
        if not self._closed:
            self._finish_close()
            self.win.destroy()
        return True
