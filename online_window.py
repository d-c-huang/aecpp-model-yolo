"""A non-modal workspace for the persistent online video queue.

The manager owns all background work. This window reads snapshots on Tk's thread
and deliberately leaves completion events for the main application's trainer.
"""
from collections import OrderedDict
from contextlib import closing
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlsplit
import webbrowser

from PIL import Image, ImageTk

import theme


STATES = {"queued": "等待中", "running": "读取中", "paused": "已暂停",
          "completed": "已入库", "failed": "读取失败", "cancelled": "已取消"}
PHASES = {"resolving": "解析视频来源", "downloading": "缓存视频", "merging": "合并音画",
          "probing": "校验完整视频", "cached": "检查已有缓存", "importing": "导入学习工程",
          "completed": "视频已入库", "queued": "等待读取", "paused": "任务已暂停"}
MODES = {"自动划分": "time", "训练集": "train", "验证集": "val"}
GROUPS = {value: key for key, value in MODES.items()}
QUALITIES = {"720p · 推荐": 720, "480p · 节省空间": 480, "1080p · 高清": 1080, "原始画质": 0}
FILTERS = {"全部状态": "all", "等待中": "queued", "读取中": "running", "已暂停": "paused",
           "已入库": "completed", "读取失败": "failed", "已取消": "cancelled"}


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and value >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def format_bytes(value):
    value = number(value)
    if value is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


def format_duration(value):
    value = number(value)
    if value is None:
        return "—"
    seconds = int(value)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}"


def source_host(job):
    try:
        return urlsplit(str(job.get("url", ""))).hostname or "在线视频"
    except ValueError:
        return "在线视频"


def metadata_for(job):
    result = job.get("result") or {}
    metadata = dict(result.get("metadata") or {})
    for key in ("duration", "width", "height", "size", "size_bytes", "thumbnail_path"):
        if key in result:
            metadata.setdefault(key, result[key])
    metadata.setdefault("size", metadata.get("size_bytes") or job.get("total_bytes"))
    return metadata


def filtered_jobs(jobs, search="", state="all"):
    search = search.strip().casefold()
    return [job for job in jobs if (state == "all" or job.get("status") == state)
            and (not search or search in " ".join(str(job.get(key) or "")
                                                 for key in ("title", "url", "message")).casefold())]


def ready_movie_ids(jobs, root):
    """Recheck the database and source file before exposing a movie action."""
    candidates = []
    for job in jobs:
        movie_id = (job.get("result") or {}).get("movie_id")
        if job.get("status") == "completed" and type(movie_id) is int and movie_id > 0:
            candidates.append(movie_id)
    if not candidates:
        return []
    path = Path(root).resolve() / "annotations.sqlite"
    if not path.is_file():
        return []
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.1)) as db:
            rows = db.execute("SELECT id,source_path FROM movies").fetchall()
        existing = {movie_id for movie_id, source in rows if source and Path(source).is_file()}
    except (sqlite3.Error, OSError, ValueError):
        return []
    return list(dict.fromkeys(movie_id for movie_id in candidates if movie_id in existing))


class OnlineWindow:
    def __init__(self, parent, manager, *, on_open_movie, on_learn, on_open_training,
                 on_close=None, request_version=None,on_cache=None,on_profiles=None,
                 on_learning_settings=None,can_download=None,active_training=None,container=None):
        """Create a standalone window or an embeddable, caller-managed frame.

        With ``container``, the caller owns page placement and visibility. Hiding
        the frame leaves queue polling alive; close only disposes this view.
        """
        self.manager = manager
        self.embedded = container is not None
        self.on_open_movie = on_open_movie
        self.on_learn = on_learn
        self.on_open_training = on_open_training
        self.on_close = on_close
        self.on_cache,self.on_profiles,self.on_learning_settings = on_cache,on_profiles,on_learning_settings
        self.can_download = can_download or (lambda:True)
        from movie_usage import MovieUsageIndex
        self.usage_index = MovieUsageIndex(manager.root,active_training or (lambda:None))
        self.usage = {}
        self.request_version = request_version or (lambda: 0)
        self._closed = False
        self._timer = None
        self._sash_timer = None
        self._jobs = []
        self._by_id = {}
        self._detail_signature = None
        self._preview_cache = OrderedDict()
        self._preview_photo = None
        self._input_errors = []
        self._compact = None
        self._composer_expanded = True
        self._initial_render = True
        self._shortcut_bindings = []
        if self.embedded:
            self.win = ttk.Frame(container,style="Online.TFrame")
        else:
            self.win = tk.Toplevel(parent)
            self.win.title("在线视频学习")
            self.win.configure(bg=theme.BACKGROUND)
            self.win.minsize(900, 620)
            width = min(1140, self.win.winfo_screenwidth() - 70)
            height = min(820, self.win.winfo_screenheight() - 90)
            self.win.geometry(f"{max(900, width)}x{max(620, height)}")
            self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<Destroy>", self._destroyed, add="+")
        self._styles()
        self.status = tk.StringVar(value="支持视频网页和直链；链接逐条缓存，完成后可标注或投喂。")
        self.search = tk.StringVar()
        self.filter = tk.StringVar(value="全部状态")
        self.mode = tk.StringVar(value="自动划分")
        self.quality = tk.StringVar(value="720p · 推荐")
        self.subtitles = tk.BooleanVar(value=True)
        self.learn = tk.BooleanVar(value=True)
        self._build()
        self.win.bind("<Configure>", self._resize, add="+")
        self.search.trace_add("write", lambda *_: self._render_list())
        self.filter.trace_add("write", lambda *_: self._render_list())
        self._bind_shortcut("<Control-f>", self.search_entry.focus_set)
        self._bind_shortcut("<Control-Return>", self._add)
        if not self.embedded:
            self.win.bind("<Escape>", lambda event: self.close())
        self.refresh()

    def _bind_shortcut(self, sequence, action):
        target = self.win.winfo_toplevel()
        def invoke(event):
            if self._closed or not self.win.winfo_ismapped():
                return
            if self.embedded and not (str(event.widget)==str(self.win) or str(event.widget).startswith(str(self.win)+".")):
                return
            action()
            return "break"
        identifier = target.bind(sequence,invoke,add="+")
        self._shortcut_bindings.append((target,sequence,identifier))

    def _styles(self):
        style = ttk.Style(self.win)
        style.configure("Online.TFrame", background=theme.BACKGROUND)
        style.configure("Online.TLabel", background=theme.BACKGROUND, foreground=theme.MUTED)
        style.configure("Online.Title.TLabel", background=theme.BACKGROUND,
                        foreground=theme.TEXT, font=(theme.FONT, 20, "bold"))
        style.configure("Online.EmbeddedTitle.TLabel", background=theme.BACKGROUND,
                        foreground=theme.TEXT, font=(theme.FONT, 16, "bold"))
        style.configure("Online.Stat.TLabel", foreground=theme.TEXT, font=(theme.FONT, 22, "bold"))
        style.configure("Online.Treeview", rowheight=36)
        style.configure("Online.TNotebook", background=theme.PANEL, borderwidth=0)
        style.configure("Online.TNotebook.Tab", padding=(12, 5), background=theme.PANEL,
                        foreground=theme.MUTED)
        style.map("Online.TNotebook.Tab", background=[("selected", theme.RAISED)],
                  foreground=[("selected", theme.ACCENT)])
        style.configure("Online.Compact.TButton", padding=(7, 2), font=(theme.FONT, 9))

    @staticmethod
    def _card(parent, **kwargs):
        return tk.Frame(parent, bg=theme.PANEL, highlightbackground=theme.BORDER,
                        highlightthickness=1, **kwargs)

    @staticmethod
    def _label(parent, text, *, color=theme.TEXT, size=10, bold=False, **kwargs):
        return tk.Label(parent, text=text, bg=theme.PANEL, fg=color,
                        font=(theme.FONT, size, "bold" if bold else "normal"), **kwargs)

    def _build(self):
        shell = ttk.Frame(self.win, style="Online.TFrame",
                          padding=(12, 8, 12, 6) if self.embedded else (16, 13, 16, 8))
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(2, weight=1)
        header = ttk.Frame(shell, style="Online.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8 if self.embedded else 12))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="自动学习" if self.embedded else "在线视频学习",
                  style="Online.EmbeddedTitle.TLabel" if self.embedded else "Online.Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="添加链接 → 下载视频 → 核对资料 → 后台学习" if self.embedded else "从视频链接到学习素材，让影片持续进入创作模型。",
                  style="Online.TLabel").grid(row=1, column=0, sticky="w", pady=(3, 0))
        self.queue_state = ttk.Label(header, text="队列就绪", style="Online.TLabel")
        self.queue_state.grid(row=0, column=1, padx=(10, 15), sticky="e")
        ttk.Button(header, text="查看训练记录  ↗", command=self._open_training,
                   style="Accent.TButton").grid(row=0, column=3, rowspan=2)
        settings = ttk.Menubutton(header,text="资源 / 设置")
        settings.grid(row=0,column=2,rowspan=2,padx=(0,8))
        menu = tk.Menu(settings,tearoff=False)
        menu.add_command(label="视频缓存与存放位置",command=lambda:self._perform(self.on_cache),
                         state="normal" if self.on_cache else "disabled")
        menu.add_command(label="电影学习设置",command=self._learning_settings,
                         state="normal" if self.on_learning_settings else "disabled")
        menu.add_command(label="所选影片资料",command=self._profiles,
                         state="normal" if self.on_profiles else "disabled")
        settings.configure(menu=menu)

        stats = ttk.Frame(shell, style="Online.TFrame")
        stats.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        self.stat_values = []
        for index, (label, color) in enumerate((("视频资源", theme.TEXT), ("读取 / 等待", theme.ACCENT),
                                                 ("已入库", theme.TEAL), ("需要处理", theme.RED))):
            stats.columnconfigure(index, weight=1, uniform="stats")
            card = self._card(stats, padx=13, pady=6)
            card.grid(row=0, column=index, sticky="ew", padx=(0 if index == 0 else 8, 0))
            value = tk.StringVar(value="0")
            self.stat_values.append(value)
            self._label(card, label, color=theme.MUTED, size=9).pack(side="left")
            self._label(card, "", textvariable=value, color=color, size=22, bold=True).pack(side="right")

        panes = ttk.Panedwindow(shell, orient="horizontal")
        panes.grid(row=2, column=0, sticky="nsew")
        left = ttk.Frame(panes, style="Online.TFrame")
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        right = self._card(panes, width=310, padx=12, pady=10)
        panes.add(left, weight=3)
        panes.add(right, weight=1)
        self._build_composer(left)
        self._build_queue(left)
        self._build_details(right)
        ttk.Label(shell, textvariable=self.status, style="Online.TLabel", wraplength=1050,
                  anchor="w").grid(row=3, column=0, sticky="ew", pady=(8, 2))
        self.footer = ttk.Label(shell, text=("切换页面后任务继续  ·  资料确认后开始学习  ·  已入库不代表训练已完成" if self.embedded else
                                             "关闭此窗口后继续在后台读取  ·  暂停会保留缓存，继续时尝试续传  ·  已入库不代表训练已完成"),
                                style="Online.TLabel", font=(theme.FONT, 9))
        self.footer.grid(row=4, column=0, sticky="w")
        self._sash_timer = self.win.after_idle(lambda: self._place_sash(panes))

    def _place_sash(self, panes):
        self._sash_timer = None
        if not self._closed:
            try:
                panes.sashpos(0, max(550, panes.winfo_width() - 315))
            except tk.TclError:
                pass

    def _resize(self, event):
        if event.widget != self.win or self._closed:
            return
        compact = event.height < (760 if self.embedded else 700)
        if compact == self._compact:
            return
        self._compact = compact
        self.links.configure(height=2 if compact else 3)
        self._set_composer_expanded(False if compact else self._composer_expanded)
        if compact:
            self.preview.grid_remove()
        else:
            self.preview.grid()
        for widget in (self.search_hint, self.input_result, self.footer):
            if compact:
                widget.grid_remove()
            else:
                widget.grid()
        if self.embedded:
            self.detail_heading.grid_remove() if compact else self.detail_heading.grid()
        self._draw_preview()

    def _set_composer_expanded(self, expanded):
        self._composer_expanded = expanded
        self.composer_toggle.configure(text="添加视频  ▾" if expanded else "添加视频  ▸")
        for widget in self.composer_parts:
            widget.grid() if expanded else widget.grid_remove()
        if not expanded or self._compact:
            self.input_result.grid_remove()

    def _build_composer(self, parent):
        card = self._card(parent, padx=12, pady=9)
        card.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        card.columnconfigure(0, weight=1)
        top = ttk.Frame(card)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.composer_toggle = ttk.Button(top, text="添加视频  ▾", style="Online.Compact.TButton",
                                          command=lambda: self._set_composer_expanded(not self._composer_expanded))
        self.composer_toggle.pack(side="left")
        self._label(top, "  一行一个链接", color=theme.SOFT, size=9).pack(side="left")
        for label, action in (("清空", self._clear_input), ("导入 TXT", self._import_links), ("粘贴链接", self._paste)):
            ttk.Button(top, text=label, command=action, style="Online.Compact.TButton").pack(side="right", padx=(5, 0))
        editor = ttk.Frame(card)
        editor.grid(row=1, column=0, sticky="ew")
        editor.columnconfigure(0, weight=1)
        self.links = tk.Text(editor, height=3, wrap="none", undo=True, bg=theme.FIELD, fg=theme.TEXT,
                             insertbackground=theme.ACCENT, selectbackground=theme.SELECTED,
                             relief="flat", padx=9, pady=7, font=(theme.FONT, 10), highlightthickness=1,
                             highlightbackground=theme.BORDER, highlightcolor=theme.ACCENT)
        self.links.grid(row=0, column=0, sticky="ew")
        scroll = ttk.Scrollbar(editor, orient="vertical", command=self.links.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.links.configure(yscrollcommand=scroll.set)
        options = ttk.Frame(card)
        options.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Combobox(options, textvariable=self.mode, state="readonly", values=list(MODES),
                     width=9).pack(side="left")
        ttk.Combobox(options, textvariable=self.quality, state="readonly", values=list(QUALITIES),
                     width=17).pack(side="left", padx=(6, 0))
        ttk.Button(options, text="加入队列  +", style="Primary.TButton", command=self._add).pack(side="right")
        bottom = ttk.Frame(card)
        bottom.grid(row=3, column=0, sticky="ew", pady=(5, 0))
        ttk.Checkbutton(bottom, text="整批读取完成后自动投喂", variable=self.learn).pack(side="left")
        ttk.Checkbutton(bottom, text="同时获取字幕", variable=self.subtitles).pack(side="left", padx=(12, 0))
        self.input_result = self._label(card, "支持视频网页 / 视频直链 · Ctrl+Enter 快速加入", color=theme.SOFT, size=9,
                                        cursor="hand2", anchor="w", wraplength=490)
        self.input_result.grid(row=4, column=0, sticky="ew", pady=(3, 0))
        self.input_result.bind("<Button-1>", lambda event: self._show_input_errors())
        self.composer_parts = (editor, options, bottom, self.input_result)

    def _build_queue(self, parent):
        card = self._card(parent, padx=10, pady=9)
        card.grid(row=1, column=0, sticky="nsew")
        card.columnconfigure(0, weight=1)
        card.rowconfigure(2, weight=1)
        top = ttk.Frame(card)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 7))
        self._label(top, "学习资源队列", bold=True, size=11).pack(side="left")
        self.queue_count = self._label(top, "", color=theme.SOFT, size=9)
        self.queue_count.pack(side="left", padx=8)
        self.pause_button = ttk.Button(top, text="暂停队列", style="Online.Compact.TButton", command=self._toggle_pause)
        self.pause_button.pack(side="right")
        self.retry_failed_button = ttk.Button(top, text="重试失败", style="Online.Compact.TButton", command=self._retry_failed)
        self.retry_failed_button.pack(side="right", padx=(0, 6))
        search_row = ttk.Frame(card)
        search_row.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        search_row.columnconfigure(0, weight=1)
        self.search_entry = ttk.Entry(search_row, textvariable=self.search)
        self.search_entry.grid(row=0, column=0, sticky="ew", padx=(0, 7))
        self.search_entry.insert(0, "")
        ttk.Combobox(search_row, textvariable=self.filter, state="readonly", values=list(FILTERS),
                     width=10).grid(row=0, column=1)
        self.search_hint = self._label(search_row, "搜索标题 / 来源 · Ctrl+F", color=theme.SOFT, size=9)
        self.search_hint.grid(row=1, column=0, sticky="w", pady=(3, 0))
        listing = ttk.Frame(card)
        listing.grid(row=2, column=0, sticky="nsew")
        listing.columnconfigure(0, weight=1)
        listing.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(listing, columns=("state", "title", "progress", "size", "group","learning"),
                                 show="headings", selectmode="extended", style="Online.Treeview", height=5)
        for name, label, width, minimum in (("state", "状态", 75, 65), ("title", "视频 / 来源", 235, 110),
                                            ("progress", "进度", 65, 58), ("size", "大小", 74, 65),
                                            ("group", "分组", 75, 65),("learning","学习",80,70)):
            self.tree.heading(name, text=label)
            self.tree.column(name, width=width, minwidth=minimum, stretch=name == "title",
                             anchor="w" if name == "title" else "center")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(listing, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        hscroll = ttk.Scrollbar(listing, orient="horizontal", command=self.tree.xview)
        hscroll.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=scrollbar.set, xscrollcommand=hscroll.set)
        self.tree.tag_configure("completed", foreground=theme.TEAL)
        self.tree.tag_configure("running", foreground=theme.ACCENT)
        self.tree.tag_configure("failed", foreground=theme.RED)
        self.tree.tag_configure("cancelled", foreground=theme.SOFT)
        self.tree.bind("<<TreeviewSelect>>", lambda event: self._selection_changed())
        self.tree.bind("<Double-1>", lambda event: self._open_movie())
        self.tree.bind("<Control-a>", self._select_all)
        self.tree.bind("<Button-3>", self._context_menu)
        self.tree.bind("<Delete>", lambda event: self._remove())
        actions = ttk.Frame(card)
        actions.grid(row=3, column=0, sticky="ew", pady=(7, 0))
        ttk.Button(actions, text="全选", command=self._select_all, style="Online.Compact.TButton").pack(side="left")
        self.cancel_button = ttk.Button(actions, text="取消所选", command=self._cancel, style="Online.Compact.TButton")
        self.cancel_button.pack(side="left", padx=(5, 0))
        self.remove_button = ttk.Button(actions, text="移除记录", command=self._remove, style="Online.Compact.TButton")
        self.remove_button.pack(side="left", padx=(5, 0))
        ttk.Button(actions, text="导出链接", command=self._export_links, style="Online.Compact.TButton").pack(side="left", padx=(5, 0))
        self.feed_button = ttk.Button(actions, text="投喂已完成资源", command=self._feed, style="Accent.TButton")
        self.feed_button.pack(side="right")
        self.selection_note = self._label(card, "Shift / Ctrl 多选 · 右键查看更多操作 · 移除记录保留视频", color=theme.SOFT, size=9, anchor="w")
        self.selection_note.grid(row=4, column=0, sticky="ew", pady=(5, 0))

    def _build_details(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(5, weight=1)
        self.detail_heading = self._label(parent, "资源详情", bold=True, size=11)
        self.detail_heading.grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.preview = tk.Canvas(parent, width=270, height=120, bg=theme.MONITOR, highlightthickness=0)
        self.preview.grid(row=1, column=0, sticky="ew")
        self.preview.bind("<Configure>", lambda event: self._draw_preview())
        self.detail_title = self._label(parent, "从左侧选择一个视频", size=11, bold=True,
                                       anchor="w", justify="left", wraplength=270)
        self.detail_title.grid(row=2, column=0, sticky="ew", pady=(9, 4))
        self.detail_meta = self._label(parent, "首帧预览会在完整缓存后显示", color=theme.MUTED,
                                      size=9, anchor="w", justify="left", wraplength=270)
        self.detail_meta.grid(row=3, column=0, sticky="ew", pady=(0, 7))
        progress_area = ttk.Frame(parent)
        progress_area.grid(row=4, column=0, sticky="ew", pady=(0, 7))
        self.progress = ttk.Progressbar(progress_area, mode="determinate", maximum=100)
        self.progress.pack(fill="x")
        self.detail_phase = self._label(progress_area, "等待选择", color=theme.ACCENT, size=9,
                                        anchor="w", wraplength=270, justify="left")
        self.detail_phase.pack(fill="x", pady=(5, 0))
        notebook = ttk.Notebook(parent, style="Online.TNotebook")
        notebook.grid(row=5, column=0, sticky="nsew")
        self.details_text = self._text_tab(notebook, "详情")
        self.log_text = self._text_tab(notebook, "任务日志")
        actions = ttk.Frame(parent)
        actions.grid(row=6, column=0, sticky="ew", pady=(9, 0))
        actions.columnconfigure((0, 1), weight=1)
        self.annotate_button = ttk.Button(actions, text="进入标注  →", command=self._open_movie, style="Primary.TButton")
        self.annotate_button.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.open_file_button = ttk.Button(actions, text="打开视频", command=self._open_file, style="Online.Compact.TButton")
        self.open_file_button.grid(row=1, column=0, sticky="ew", padx=(0, 3))
        self.open_folder_button = ttk.Button(actions, text="视频文件夹", command=lambda: self._open_file(folder=True), style="Online.Compact.TButton")
        self.open_folder_button.grid(row=1, column=1, sticky="ew", padx=(3, 0))
        self.copy_button = ttk.Button(actions, text="复制链接", command=self._copy_link, style="Online.Compact.TButton")
        self.copy_button.grid(row=2, column=0, sticky="ew", padx=(0, 3), pady=(6, 0))
        self.web_button = ttk.Button(actions, text="浏览器打开", command=self._open_web, style="Online.Compact.TButton")
        self.web_button.grid(row=2, column=1, sticky="ew", padx=(3, 0), pady=(6, 0))

    def _text_tab(self, notebook, label):
        frame = ttk.Frame(notebook)
        notebook.add(frame, text=label)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        text = tk.Text(frame, width=25, height=4, wrap="word", bg=theme.FIELD, fg=theme.MUTED,
                       relief="flat", padx=9, pady=8, font=(theme.FONT, 9), state="disabled",
                       selectbackground=theme.SELECTED)
        text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=scroll.set)
        return text

    def _set_text(self, widget, value):
        value = str(value or "")
        if widget.get("1.0", "end-1c") == value:
            return
        position = widget.yview()[0]
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")
        widget.yview_moveto(position)

    def _selected(self):
        return [self._by_id[iid] for iid in self.tree.selection() if iid in self._by_id]

    def _current(self):
        selected = self._selected()
        focused = self._by_id.get(self.tree.focus())
        return focused if focused in selected else (selected[0] if selected else None)

    @staticmethod
    def _enable(button, enabled):
        button.state(["!disabled" if enabled else "disabled"])

    def refresh(self):
        if self._closed:
            return
        self._timer = None
        try:
            self.usage = self.usage_index.snapshot()
            self._jobs = self.manager.list_jobs()
            self._by_id = {str(job["id"]): job for job in self._jobs}
            counts = [len(self._jobs), sum(j.get("status") in ("running", "queued") for j in self._jobs),
                      sum(j.get("status") == "completed" for j in self._jobs),
                      sum(j.get("status") == "failed" for j in self._jobs)]
            for value, count in zip(self.stat_values, counts):
                value.set(str(count))
            paused = self.manager.paused
            self.pause_button.configure(text="继续队列" if paused else "暂停队列")
            self.queue_state.configure(text="● 队列已暂停" if paused else "● 正在读取" if self.manager.busy else "● 队列就绪",
                                       foreground=theme.ACCENT if self.manager.busy else theme.MUTED)
            self._enable(self.pause_button, bool(self.manager.has_pending or paused))
            self._enable(self.retry_failed_button, any(j.get("status") == "failed" for j in self._jobs))
            self._render_list()
            if self._initial_render:
                self._initial_render = False
                if self._jobs:
                    self._set_composer_expanded(False)
        except Exception as exc:
            self.status.set(f"暂时无法刷新队列：{exc}")
        finally:
            if not self._closed:
                self._timer = self.win.after(700, self.refresh)

    def _refresh_now(self):
        if self._timer is not None:
            self.win.after_cancel(self._timer)
            self._timer = None
        self.refresh()

    def _render_list(self):
        if self._closed:
            return
        visible = filtered_jobs(self._jobs, self.search.get(), FILTERS.get(self.filter.get(), "all"))
        identifiers = {str(job["id"]) for job in visible}
        for iid in self.tree.get_children():
            if iid not in identifiers:
                self.tree.delete(iid)
        for index, job in enumerate(reversed(visible)):
            iid = str(job["id"])
            progress = number(job.get("progress"))
            state = job.get("status", "queued")
            metadata = metadata_for(job)
            title = job.get("title") or (job.get("result") or {}).get("title") or source_host(job)
            usage = self.usage.get((job.get("result") or {}).get("movie_id"),{})
            learned = ("训练中" if usage.get("last_state")=="running" else
                       f"已学 {usage['completed']} 批" if usage.get("completed") else "未学习")
            values = (STATES.get(state, state), title,
                      "100%" if state == "completed" else f"{min(100, progress):.0f}%" if progress is not None else "—",
                      format_bytes(metadata.get("size")), GROUPS.get(job.get("mode") or (job.get("config") or {}).get("mode"), "自动划分"),learned)
            if self.tree.exists(iid):
                if tuple(self.tree.item(iid, "values")) != tuple(str(v) for v in values):
                    self.tree.item(iid, values=values, tags=(state,))
                self.tree.move(iid, "", index)
            else:
                self.tree.insert("", index, iid=iid, values=values, tags=(state,))
        self.queue_count.configure(text=f"{len(visible)} / {len(self._jobs)}")
        self._selection_changed()

    def _selection_changed(self):
        if self._closed:
            return
        selected = self._selected()
        ready = ready_movie_ids(selected, self.manager.root)
        self._enable(self.feed_button, bool(ready))
        self.feed_button.configure(text=f"投喂已完成资源 ({len(ready)})" if ready else "投喂已完成资源")
        self._enable(self.cancel_button, any(j.get("status") in ("queued", "running", "paused") for j in selected))
        self._enable(self.remove_button, any(j.get("status") not in ("queued", "running", "paused") for j in selected))
        self.selection_note.configure(text=f"已选 {len(selected)} 条 · 可投喂 {len(ready)} 条 · 移除记录保留视频" if selected
                                      else "Shift / Ctrl 多选 · 右键查看更多操作 · 移除记录保留视频")
        job = self._current()
        self._enable(self.annotate_button, bool(job and ready_movie_ids([job], self.manager.root)))
        local = self._local_path(job)
        self._enable(self.open_file_button, bool(local))
        self._enable(self.open_folder_button, bool(local))
        self._enable(self.copy_button, bool(selected))
        self._enable(self.web_button, bool(job))
        self._render_details(job)

    def _render_details(self, job):
        movie_id = ((job or {}).get("result") or {}).get("movie_id")
        usage = self.usage.get(movie_id,{})
        signature = repr((job,usage))
        if signature == self._detail_signature:
            return
        self._detail_signature = signature
        if not job:
            self.detail_title.configure(text="从左侧选择一个视频")
            self.detail_meta.configure(text="首帧预览会在完整缓存后显示")
            self.detail_phase.configure(text="等待选择")
            self._set_progress(0)
            self._set_text(self.details_text, "加入视频链接后，可在这里查看缓存进度、字幕导入结果和来源。\n\n选择多条已入库资源，可一起投喂后台学习。")
            self._set_text(self.log_text, "任务日志会显示在这里。")
        else:
            metadata = metadata_for(job)
            result = job.get("result") or {}
            config = job.get("config") or {}
            self.detail_title.configure(text=job.get("title") or result.get("title") or source_host(job))
            resolution = f"{metadata['width']} × {metadata['height']}" if metadata.get("width") and metadata.get("height") else "清晰度待解析"
            self.detail_meta.configure(text=f"{format_duration(metadata.get('duration'))}  ·  {resolution}  ·  {format_bytes(metadata.get('size'))}")
            value = number(job.get("progress"))
            self._set_progress(100 if job.get("status") == "completed" else value,
                               active=job.get("status") == "running")
            phase = PHASES.get(job.get("phase"), STATES.get(job.get("status"), "等待读取"))
            speed = number(job.get("speed"))
            eta = number(job.get("eta"))
            if speed:
                phase += f" · {format_bytes(speed)}/s"
            if eta is not None:
                phase += f" · 剩余 {format_duration(eta)}"
            self.detail_phase.configure(text=phase, foreground=theme.RED if job.get("status") == "failed" else theme.ACCENT)
            height = job.get("height", config.get("height", 720))
            details = [f"状态    {STATES.get(job.get('status'), '等待中')}", f"来源    {source_host(job)}",
                       f"分组    {GROUPS.get(job.get('mode', config.get('mode')), '自动划分')}",
                       f"目标画质    {str(height)+'p' if height else '原始画质'}",
                       f"字幕    {'尝试获取可用字幕' if job.get('subtitles', config.get('subtitles')) else '未请求'}",
                       f"学习    {'本批读取完成后自动投喂' if job.get('learn', config.get('learn')) else '手动选择后投喂'}"]
            if movie_id:
                last = {"completed":"已完成","running":"训练中","failed":"失败","stopped":"已停止",
                        "unfinished":"上次未结束","unknown":"暂无记录"}.get(usage.get("last_state"),"暂无记录")
                details.extend((f"学习记录    已完成 {usage.get('completed',0)} 批 · 最近：{last}",
                                "可再次投喂继续训练，或补充资料、字幕及截图标注后学习。"))
            if job.get("downloaded_bytes") is not None:
                details.append(f"已缓存    {format_bytes(job['downloaded_bytes'])}")
            if result.get("subtitle_report"):
                details.extend(("", "字幕导入", result["subtitle_report"]))
            if result.get("reused"):
                details.append("已复用完整缓存")
            details.extend(("", "来源链接", job.get("url", "")))
            if result.get("path"):
                details.extend(("", "本地视频", result["path"]))
            if job.get("error"):
                details.extend(("", "失败原因", job["error"], "", "可使用“重试失败”；原缓存会保留。"))
            elif job.get("message"):
                details.extend(("", str(job["message"])))
            self._set_text(self.details_text, "\n".join(details))
            logs = job.get("log_tail") or job.get("message") or "尚无日志。"
            if isinstance(logs, list):
                logs = "\n".join(str(line) for line in logs)
            if job.get("error") and job["error"] not in str(logs):
                logs = str(logs) + "\n\n" + job["error"]
            self._set_text(self.log_text, logs)
        self._draw_preview()

    def _set_progress(self, value, active=False):
        if value is None and active:
            if str(self.progress.cget("mode")) != "indeterminate":
                self.progress.configure(mode="indeterminate")
                self.progress.start(35)
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate", value=min(100, value or 0))

    def _draw_preview(self):
        if self._closed:
            return
        self.preview.delete("all")
        width, height = max(270, self.preview.winfo_width()), self.preview.winfo_height()
        height = height if height > 1 else 120
        job = self._current()
        metadata = metadata_for(job) if job else {}
        path = metadata.get("thumbnail_path")
        self._preview_photo = None
        if path:
            try:
                path = Path(path)
                # Only local files supplied by the completed media importer are read.
                if path.is_file():
                    key = (str(path), path.stat().st_mtime_ns, width, height)
                    photo = self._preview_cache.get(key)
                    if photo is None:
                        with Image.open(path) as source:
                            source.thumbnail((width, height), Image.Resampling.LANCZOS)
                            photo = ImageTk.PhotoImage(source.copy(), master=self.win)
                        self._preview_cache[key] = photo
                        while len(self._preview_cache) > 10:
                            self._preview_cache.popitem(last=False)
                    self._preview_cache.move_to_end(key)
                    self._preview_photo = photo
                    self.preview.create_image(width // 2, height // 2, image=photo)
                    return
            except (OSError, ValueError, tk.TclError):
                pass
        self.preview.create_rectangle(width // 2 - 18, height // 2 - 25, width // 2 + 18, height // 2 + 5,
                                      outline=theme.BORDER, width=2)
        self.preview.create_polygon(width // 2 - 4, height // 2 - 18, width // 2 - 4, height // 2 - 2,
                                    width // 2 + 9, height // 2 - 10, fill=theme.ACCENT)
        self.preview.create_text(width // 2, height // 2 + 27, text="视频首帧预览" if not job else "暂无首帧预览" if job.get("status") == "completed" else "缓存完成后显示首帧",
                                 fill=theme.SOFT, font=(theme.FONT, 9))

    def _perform(self, action, success=None):
        try:
            result = action()
            if not self._closed:
                if success:
                    self.status.set(success)
                self._refresh_now()
            return result
        except Exception as exc:
            if not self._closed:
                self.status.set(str(exc))
                messagebox.showerror("在线视频学习", str(exc), parent=self.win)
            return None

    def _clear_input(self):
        self.links.delete("1.0", "end")
        self.input_result.configure(text="Ctrl+Enter 快速加入", fg=theme.SOFT)
        self._input_errors = []

    def _append_input(self, value):
        self._set_composer_expanded(True)
        existing = self.links.get("1.0", "end-1c")
        if existing and not existing.endswith("\n"):
            self.links.insert("end", "\n")
        self.links.insert("end", value.strip() + "\n")
        self.links.see("end")
        self.links.focus_set()

    def _paste(self):
        try:
            self._append_input(self.win.clipboard_get())
        except tk.TclError:
            self.status.set("剪贴板中没有可粘贴的文本。")

    def _import_links(self):
        path = filedialog.askopenfilename(parent=self.win, title="导入视频链接", filetypes=[("链接文本", "*.txt"), ("所有文件", "*.*")])
        if not path:
            return
        def action():
            content = Path(path).read_bytes()
            try:
                value = content.decode("utf-8-sig")
            except UnicodeDecodeError:
                value = content.decode("gb18030")
            self._append_input(value)
        self._perform(action, "已载入文本；检查画质和学习选项后加入队列。")

    def _add(self):
        if not self.can_download():
            self.status.set("缓存管理正在执行，请完成后再添加或继续读取。")
            return
        text = self.links.get("1.0", "end-1c")
        if not text.strip():
            self.status.set("先粘贴一个或多个视频网页 / 视频直链。")
            self.links.focus_set()
            return
        def action():
            outcome = self.manager.add(text, mode=MODES[self.mode.get()], height=QUALITIES[self.quality.get()],
                                       subtitles=self.subtitles.get(), learn=self.learn.get(), request_version=self.request_version())
            self._input_errors = outcome.get("errors") or []
            added = outcome.get("added") or []
            duplicate_count = len(outcome.get("duplicates") or [])
            message = f"加入 {len(added)} 条"
            if duplicate_count:
                message += f" · 跳过重复 {duplicate_count} 条"
            if self._input_errors:
                message += f" · 错误 {len(self._input_errors)} 行（点击查看）"
            self.input_result.configure(text=message, fg=theme.RED if self._input_errors else theme.TEAL)
            self.links.delete("1.0", "end")
            self.links.insert("1.0", "\n".join(str(item.get("text", "")) for item in self._input_errors))
            self.status.set(message + ("；队列暂停中，点击继续队列开始读取。" if self.manager.paused else "。"))
            if self._input_errors:
                self.input_result.grid()
            if added and not self.manager.paused:
                self.manager.start()
            self._jobs = self.manager.list_jobs()
            self._by_id = {str(job["id"]): job for job in self._jobs}
            self._render_list()
            if added and self.tree.exists(str(added[-1])):
                self.tree.selection_set(str(added[-1]))
                self.tree.focus(str(added[-1]))
                self.tree.see(str(added[-1]))
            if added and not self._input_errors:
                self._set_composer_expanded(False)
        self._perform(action)

    def _show_input_errors(self):
        if self._input_errors:
            messagebox.showinfo("链接需要修正", "\n\n".join(f"第 {item.get('line', '?')} 行：{item.get('message', '链接无效')}\n{item.get('text', '')}"
                                                        for item in self._input_errors), parent=self.win)

    def _toggle_pause(self):
        paused = self.manager.paused
        if paused and not self.can_download():
            self.status.set("缓存管理正在执行，请完成后再继续队列。")
            return
        self._perform((lambda: self.manager.resume(request_version=self.request_version())) if paused else self.manager.pause,
                      "队列已继续，已有缓存会尽量复用。" if paused else "正在暂停队列，已缓存文件会保留。")

    def _retry_failed(self):
        self._retry([j for j in self._jobs if j.get("status") == "failed"])

    def _retry(self, jobs=None):
        if not self.can_download():
            self.status.set("缓存管理正在执行，请完成后再重试。")
            return
        jobs = self._selected() if jobs is None else jobs
        ids = [j["id"] for j in jobs if j.get("status") in ("failed", "cancelled")]
        if not ids:
            return
        def action():
            self.manager.retry(ids, request_version=self.request_version())
            if not self.manager.paused:
                self.manager.start()
        self._perform(action, f"已将 {len(ids)} 条资源重新排队。" + ("点击继续队列开始读取。" if self.manager.paused else ""))

    def _cancel(self):
        ids = [j["id"] for j in self._selected() if j.get("status") in ("queued", "running", "paused")]
        if ids:
            self._perform(lambda: self.manager.cancel(ids), f"已请求取消 {len(ids)} 条任务；缓存保留，可右键重试。")

    def _remove(self):
        ids = [j["id"] for j in self._selected() if j.get("status") not in ("queued", "running", "paused")]
        if ids:
            self._perform(lambda: self.manager.remove(ids), f"已移除 {len(ids)} 条队列记录，视频、标注和缓存均保留。")
        else:
            self.status.set("正在读取或等待的任务请先取消，再移除记录。")

    def _select_all(self, event=None):
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def _export_links(self):
        jobs = self._selected() or filtered_jobs(self._jobs, self.search.get(), FILTERS.get(self.filter.get(), "all"))
        if not jobs:
            self.status.set("当前没有可导出的链接。")
            return
        path = filedialog.asksaveasfilename(parent=self.win, title="导出所选链接" if self._selected() else "导出当前列表链接",
                                           defaultextension=".txt", initialfile="视频学习链接.txt", filetypes=[("文本", "*.txt")])
        if path:
            self._perform(lambda: Path(path).write_text("\n".join(j["url"] for j in jobs) + "\n", encoding="utf-8-sig"),
                          f"已导出 {len(jobs)} 条视频链接。")

    @staticmethod
    def _local_path(job):
        if job and job.get("status") == "completed":
            path = (job.get("result") or {}).get("path")
            if path and Path(path).is_file():
                return Path(path)
        return None

    def _open_file(self, folder=False):
        path = self._local_path(self._current())
        if not path:
            self.status.set("视频尚未完整入库，或本地文件已不存在。")
            return
        if folder:
            path = path.parent
        def action():
            if sys.platform == "win32":
                os.startfile(str(path))
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
        self._perform(action)

    def _copy_link(self):
        jobs = self._selected()
        if jobs:
            self.win.clipboard_clear()
            self.win.clipboard_append("\n".join(job["url"] for job in jobs))
            self.status.set(f"已复制 {len(jobs)} 条来源链接。")

    def _open_web(self):
        job = self._current()
        if not job:
            return
        def action():
            from online_video import validate_url
            webbrowser.open(validate_url(job["url"]), new=2)
        self._perform(action)

    def _open_movie(self):
        job = self._current()
        ids = ready_movie_ids([job] if job else [], self.manager.root)
        if not ids:
            self.status.set("请先选择已入库且本地视频仍存在的资源。")
            return
        try:
            if self.on_open_movie(ids[0]) is not False:
                if not self.embedded:
                    self.close()
            else:
                self.status.set("当前任务尚未结束，请稍后重试。")
        except Exception as exc:
            self.status.set(str(exc))
            messagebox.showerror("进入标注", str(exc), parent=self.win)

    def _feed(self):
        ids = ready_movie_ids(self._selected(), self.manager.root)
        if not ids:
            self.status.set("请先选择已入库且本地视频仍存在的资源。")
            return
        def action():
            accepted = self.on_learn(ids)
            if accepted=="waiting-profiles":
                self.status.set("学习前请在影片资料窗口核对演员、角色和剧情，确认后会继续。")
            elif accepted is not False:
                self.status.set(f"已提交 {len(ids)} 部视频的学习请求；训练进度请查看训练记录。")
            else:
                self.status.set("当前任务尚未结束，请稍后重试。")
        self._perform(action)

    def _open_training(self):
        self._perform(self.on_open_training)

    def _profiles(self):
        ids = list(dict.fromkeys((job.get("result") or {}).get("movie_id") for job in self._selected()
                                if job.get("status")=="completed" and (job.get("result") or {}).get("movie_id")))
        if self.on_profiles and ids:
            self._perform(lambda:self.on_profiles(ids))
        else:
            self.status.set("请选择已入库资源，再查看或补充影片资料。")

    def _learning_settings(self):
        if self.on_learning_settings:
            self._perform(self.on_learning_settings)

    def _context_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        self.tree.focus(iid)
        self._selection_changed()
        selected = self._selected()
        job = self._current()
        ready = ready_movie_ids(selected, self.manager.root)
        menu = tk.Menu(self.win, tearoff=False, bg=theme.RAISED, fg=theme.TEXT,
                       activebackground=theme.SELECTED, activeforeground=theme.ACCENT,
                       disabledforeground=theme.DISABLED, relief="flat")
        def add(label, command, enabled=True):
            menu.add_command(label=label, command=command, state="normal" if enabled else "disabled")
        add("进入标注", self._open_movie, bool(ready_movie_ids([job], self.manager.root)))
        add("投喂所选已完成资源", self._feed, bool(ready))
        add("演员、角色与剧情资料",self._profiles,bool(ready and self.on_profiles))
        add("电影学习设置 / 从头重训",self._learning_settings,bool(self.on_learning_settings))
        add("管理缓存 / 切换存放位置",lambda:self._perform(self.on_cache),bool(self.on_cache))
        menu.add_separator()
        add("重试所选失败 / 取消任务", self._retry, any(j.get("status") in ("failed", "cancelled") for j in selected))
        add("取消所选任务", self._cancel, any(j.get("status") in ("queued", "running", "paused") for j in selected))
        add("移除所选记录（保留视频）", self._remove, any(j.get("status") not in ("queued", "running", "paused") for j in selected))
        menu.add_separator()
        add("打开本地视频", self._open_file, bool(self._local_path(job)))
        add("打开视频文件夹", lambda: self._open_file(folder=True), bool(self._local_path(job)))
        add("复制所选链接", self._copy_link)
        add("导出所选链接", self._export_links)
        add("在浏览器中打开来源", self._open_web)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def show(self):
        if not self._closed:
            if not self.embedded:
                self.win.deiconify()
            self.win.lift()
            self.win.focus_set()

    def _destroyed(self, event):
        if event.widget == self.win:
            self._finish_close()

    def _finish_close(self):
        if self._closed:
            return
        self._closed = True
        self.usage_index.close()
        for target,sequence,identifier in self._shortcut_bindings:
            try:
                target.unbind(sequence,identifier)
            except tk.TclError:
                pass
        self._shortcut_bindings.clear()
        try:
            self.progress.stop()
        except tk.TclError:
            pass
        if self._timer is not None:
            try:
                self.win.after_cancel(self._timer)
            except tk.TclError:
                pass
            self._timer = None
        if self._sash_timer is not None:
            try:
                self.win.after_cancel(self._sash_timer)
            except tk.TclError:
                pass
            self._sash_timer = None
        self._preview_cache.clear()
        self._preview_photo = None
        if self.on_close:
            self.on_close()

    def close(self):
        if not self._closed:
            self._finish_close()
            self.win.destroy()
