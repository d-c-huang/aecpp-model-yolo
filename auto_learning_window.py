"""A task monitor for automatic video references; the manager owns workers."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tkinter as tk
from tkinter import ttk

import theme
from online_window import ContinuousResultsPanel


STATES = {"queued": "等待分析", "running": "正在学习", "paused": "已暂停",
          "completed": "参考已更新", "partial": "部分完成，需处理", "needs_input": "需要字幕 / 模型", "failed": "失败", "cancelled": "已取消"}
ACTIVE = {"queued", "running", "paused"}


def available_movies(root):
    """Read quickly without creating a new database or changing project state."""
    path = Path(root).resolve() / "annotations.sqlite"
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.15)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT id,name,mode,source_path FROM movies ORDER BY id DESC")
                if row["source_path"] and Path(row["source_path"]).is_file()]


class AutoLearningWindow:
    def __init__(self, parent, manager, *, on_open_board, on_close=None, container=None,
                 can_write=lambda: True, on_import_helper=None, on_video_analysis=None, continuous_status=None,on_dashboard=None):
        self.manager, self.on_open_board = manager, on_open_board
        self.on_close, self.can_write = on_close, can_write
        self.on_import_helper = on_import_helper
        self.on_video_analysis = on_video_analysis
        self.on_dashboard = on_dashboard
        self.continuous_status = continuous_status
        self.embedded = container is not None
        self._closed, self._timer, self._picker = False, None, None
        self._jobs, self._by_id = [], {}
        self._reference_signature, self._reference_label = None, "尚未建立节奏参考"
        self.win = ttk.Frame(container) if self.embedded else tk.Toplevel(parent)
        if not self.embedded:
            self.win.title("自动学习 · 视频参考")
            self.win.geometry("1000x680")
            self.win.minsize(860, 560)
            self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<Destroy>", self._destroyed, add="+")
        self.summary = tk.StringVar(self.win, value="正在读取自动学习任务…")
        self.status = tk.StringVar(self.win, value="选择已入库视频，或在在线视频页面启用自动编排学习。")
        self.detail = tk.StringVar(self.win, value="选择任务查看进度；完成后可打开参考分镜继续编辑。")
        self.search = tk.StringVar(self.win)
        self.board_part = tk.StringVar(self.win)
        self._board_paths, self._board_job = [], None
        self._build()
        self._search_trace = self.search.trace_add("write", lambda *_: self._render())
        self.refresh()

    def _build(self):
        body = ttk.Frame(self.win, padding=(16, 12))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(4, weight=1)
        heading = ttk.Frame(body)
        heading.grid(row=0, column=0, sticky="ew")
        ttk.Label(heading, text="自动学习", style="Title.TLabel").pack(side="left")
        if self.on_dashboard:
            ttk.Button(heading,text="学习仪表盘",command=self.on_dashboard,style="Compact.TButton").pack(side="left",padx=12)
        self.add_button = ttk.Button(heading, text="添加现有影片…", command=self._choose_movies, style="Accent.TButton")
        self.add_button.pack(side="right")
        if self.on_video_analysis:
            ttk.Button(heading,text="视频理解与笔记",command=self._video_analysis,
                       style="Compact.TButton").pack(side="right",padx=8)
        if self.on_import_helper:
            self.import_helper_button = ttk.Button(heading, text="导入 Video Helper 转写…",
                                                   command=self._import_helper, style="Compact.TButton")
            self.import_helper_button.pack(side="right", padx=(8, 8))
        ttk.Label(body, text="自动分析字幕与画面 → 积累节奏、动作和表情参考 → 可打开分镜校正，人工确认优先",
                  style="Muted.TLabel", wraplength=920).grid(row=1, column=0, sticky="w", pady=(6, 8))
        summary_area = ttk.Frame(body)
        summary_area.grid(row=2, column=0, sticky="ew")
        summary_area.columnconfigure(0, weight=1)
        ttk.Label(summary_area, textvariable=self.summary, style="Accent.TLabel", wraplength=920).grid(row=0, column=0, sticky="ew")
        if self.continuous_status is not None:
            self.results_panel = ContinuousResultsPanel(summary_area)
            self.results_panel.grid(row=1, column=0, sticky="ew", pady=(5, 0))
        controls = ttk.Frame(body)
        controls.grid(row=3, column=0, sticky="ew", pady=(10, 8))
        controls.columnconfigure(1, weight=1)
        ttk.Label(controls, text="搜索影片").grid(row=0, column=0, padx=(0, 8))
        ttk.Entry(controls, textvariable=self.search).grid(row=0, column=1, sticky="ew")
        self.pause_button = ttk.Button(controls, text="暂停学习", command=self._toggle_pause)
        self.pause_button.grid(row=0, column=2, padx=(8, 0))
        listing = ttk.Frame(body)
        listing.grid(row=4, column=0, sticky="nsew")
        listing.columnconfigure(0, weight=1)
        listing.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(listing, columns=("title", "state", "count", "message"), show="headings",
                                 selectmode="extended", height=8)
        for key, label, width in (("title", "影片", 200), ("state", "学习状态", 140),
                                  ("count", "参考样例", 85), ("message", "当前进度", 400)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=65, stretch=key in {"title", "message"})
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(listing, command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.tag_configure("failed", foreground=theme.RED)
        self.tree.tag_configure("needs_input", foreground=theme.ACCENT)
        self.tree.tag_configure("completed", foreground=theme.TEAL)
        self.tree.bind("<<TreeviewSelect>>", lambda _: self._selection_changed())
        self.tree.bind("<Double-1>", lambda _: self._open_board())
        self.progress = ttk.Progressbar(body, mode="indeterminate")
        self.progress.grid(row=5, column=0, sticky="ew", pady=(9, 6))
        ttk.Label(body, textvariable=self.detail, wraplength=920, justify="left",
                  style="Muted.TLabel").grid(row=6, column=0, sticky="ew", pady=(0, 8))
        actions = ttk.Frame(body)
        actions.grid(row=7, column=0, sticky="ew")
        self.retry_button = ttk.Button(actions, text="重试所选", command=self._retry)
        self.retry_button.pack(side="left")
        self.reanalyze_button = ttk.Button(actions, text="重新分析所选", command=self._reanalyze)
        self.reanalyze_button.pack(side="left", padx=8)
        self.cancel_button = ttk.Button(actions, text="取消所选", command=self._cancel)
        self.cancel_button.pack(side="left")
        self.open_button = ttk.Button(actions, text="打开参考分镜", command=self._open_board, style="Accent.TButton")
        self.open_button.pack(side="right")
        self.board_part_box = ttk.Combobox(actions, textvariable=self.board_part, state="disabled", width=12)
        self.board_part_box.pack(side="right", padx=(8, 8))
        ttk.Label(body, textvariable=self.status, style="Muted.TLabel", wraplength=920,
                  justify="left").grid(row=8, column=0, sticky="ew", pady=(9, 0))
        ttk.Label(body, text="自动参考与已确认偏好分开保存；字幕时长不等于真实配音时长。关闭窗口后任务继续。",
                  style="Caption.TLabel", wraplength=920).grid(row=9, column=0, sticky="w", pady=(5, 0))

    def _perform(self, action, message=None, *, write=True):
        if write and not self.can_write():
            self.status.set("工程正在迁移或整理缓存，请完成后再开始学习。")
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

    def refresh_now(self):
        if self._timer is not None:
            self.win.after_cancel(self._timer)
            self._timer = None
        self.refresh()

    def refresh(self):
        if self._closed:
            return
        try:
            self._jobs = self.manager.list_jobs()
            self._by_id = {str(row["id"]): row for row in self._jobs}
            active = sum(row.get("status") in ACTIVE for row in self._jobs)
            complete = [row for row in self._jobs if row.get("status") == "completed"]
            partial = sum(row.get("status") == "partial" for row in self._jobs)
            needs = sum(row.get("status") in {"needs_input", "failed"} for row in self._jobs)
            label = f"{len(self._jobs)} 个任务 · {active} 个进行中 · {len(complete)} 个参考已更新 · {partial} 个部分完成 · {needs} 个需处理"
            if self.continuous_status is not None:
                self.results_panel.update_snapshot(self.continuous_status() or {})
            else:
                label += "\n" + self._reference_summary()
            self.summary.set(label)
            self.pause_button.configure(text="继续学习" if self.manager.paused else "暂停学习")
            if self.manager.busy and not self.manager.paused:
                if not getattr(self, "_animating", False):
                    self.progress.start(15)
                    self._animating = True
            else:
                self.progress.stop()
                self._animating = False
            self._render()
        except Exception as exc:
            self.status.set(f"暂时无法刷新学习任务：{exc}")
        finally:
            if not self._closed:
                self._timer = self.win.after(700, self.refresh)

    def _reference_summary(self):
        path = Path(self.manager.root) / "auto_learning" / "reference.json"
        try:
            info = path.stat()
            signature = info.st_mtime_ns, info.st_size
        except FileNotFoundError:
            self._reference_signature = None
            return "尚未建立节奏参考"
        if signature != self._reference_signature:
            from auto_learning import load_reference
            try:
                reference = load_reference(self.manager.root)
            except (OSError, ValueError, TypeError) as exc:
                self._reference_label = f"节奏参考不可用，可重新分析修复：{exc}"
                self._reference_signature = signature
                return self._reference_label
            if not reference:
                return "尚未建立节奏参考"
            validation = reference.get("validation", {})
            label = f"字幕节奏参考：{reference.get('example_count', 0)} 条样例 / {reference.get('movie_count', 0)} 部训练影片"
            label += " · 可用于新分镜" if reference.get("usable") else " · 未启用，沿用原时长"
            if validation.get("kind") == "none":
                label += " · 尚未独立验证"
            elif validation.get("mae_seconds") is not None and validation.get("baseline_mae_seconds") is not None:
                label += f" · 留出时长误差 {float(validation['mae_seconds']):.2f}s / 原规则 {float(validation['baseline_mae_seconds']):.2f}s"
            self._reference_label, self._reference_signature = label, signature
        return self._reference_label

    def _render(self):
        if self._closed:
            return
        query = self.search.get().strip().casefold()
        rows = [row for row in self._jobs if not query or query in (str(row.get("title", "")) + " " + str(row.get("message", ""))).casefold()]
        ids = {str(row["id"]) for row in rows}
        for key in self.tree.get_children():
            if key not in ids:
                self.tree.delete(key)
        for index, row in enumerate(rows):
            key, state = str(row["id"]), row.get("status", "queued")
            values = (row.get("title") or f"影片 {row.get('movie_id', '')}", STATES.get(state, state),
                      row.get("example_count", 0), row.get("message", ""))
            if self.tree.exists(key):
                self.tree.item(key, values=values, tags=(state,))
                self.tree.move(key, "", index)
            else:
                self.tree.insert("", "end", iid=key, values=values, tags=(state,))
        self._selection_changed()

    def _selected(self):
        return [self._by_id[key] for key in self.tree.selection() if key in self._by_id]

    def _paths_for(self, row):
        root = Path(self.manager.root).resolve()
        values = row.get("board_paths") or ([row["board_path"]] if row.get("board_path") else [])
        paths = []
        for value in values:
            if not isinstance(value, str):
                continue
            try:
                path = Path(value)
                path = (path if path.is_absolute() else root / path).resolve()
                if path.is_relative_to(root) and path.is_file():
                    paths.append(path)
            except (OSError, ValueError):
                continue
        return list(dict.fromkeys(paths))

    def _selection_changed(self):
        selected = self._selected()
        row = selected[0] if len(selected) == 1 else None
        paths = self._paths_for(row) if row else []
        if self._board_job != (row or {}).get("id") or paths != self._board_paths:
            self._board_paths, self._board_job = paths, (row or {}).get("id")
            self.board_part_box.configure(values=[f"第 {i+1} / {len(paths)} 份" for i in range(len(paths))],
                                          state="readonly" if len(paths) > 1 else "disabled")
            self.board_part.set(f"第 1 / {len(paths)} 份" if paths else "无分镜")
        self.open_button.configure(state="normal" if paths else "disabled")
        self.retry_button.configure(state="normal" if any(r.get("status") in {"failed", "cancelled", "needs_input", "partial"} for r in selected) else "disabled")
        self.reanalyze_button.configure(state="normal" if any(r.get("status") not in ACTIVE for r in selected) else "disabled")
        self.cancel_button.configure(state="normal" if any(r.get("status") in ACTIVE for r in selected) else "disabled")
        if self.on_import_helper:
            ready = bool(row and row.get("status") not in ACTIVE and self.can_write())
            self.import_helper_button.configure(state="normal" if ready else "disabled")
        if row:
            content = str(row.get("error") or row.get("message") or STATES.get(row.get("status"), ""))
            if row.get("status") == "needs_input":
                content += "\n请在影片资源中补充字幕或带时间码的剧本，再重新分析。"
            elif row.get("status") == "completed":
                content += "\n打开参考分镜可修正人物、动作与表情；确认后才加入个人编排偏好。"
            self.detail.set(content[:420])
        else:
            self.detail.set(f"已选择 {len(selected)} 个任务，可批量重试或重新分析。" if selected else "选择任务查看进度；完成后可打开参考分镜继续编辑。")

    def _toggle_pause(self):
        if self.manager.paused:
            self._perform(self.manager.resume, "已继续自动学习。")
        else:
            self._perform(self.manager.pause, "正在暂停；已完成的参考和模型保留。", write=False)

    def _import_helper(self):
        selected = self._selected()
        if not self.on_import_helper or len(selected) != 1 or selected[0].get("status") in ACTIVE:
            self.status.set("请选择一个未在运行、排队或暂停中的影片任务，再导入对应转写。")
            return
        self._perform(lambda: self.on_import_helper(selected[0]["movie_id"]))

    def _video_analysis(self):
        selected = self._selected()
        if self.on_video_analysis:
            self._perform(lambda:self.on_video_analysis(selected[0]["movie_id"] if len(selected)==1 else None),write=False)

    def _retry(self):
        ids = [r["id"] for r in self._selected() if r.get("status") in {"failed", "cancelled", "needs_input", "partial"}]
        if ids:
            self._perform(lambda: self.manager.retry(ids), "已重新排队；队列暂停时请点击继续学习。")

    def _reanalyze(self):
        ids = list(dict.fromkeys(r["movie_id"] for r in self._selected() if r.get("status") not in ACTIVE))
        if ids:
            self._perform(lambda: self.manager.enqueue(ids, force=True), "已按最新字幕、剧本和角色资料重新排队。")

    def _cancel(self):
        ids = [r["id"] for r in self._selected() if r.get("status") in ACTIVE]
        if ids:
            self._perform(lambda: self.manager.cancel(ids), "已请求取消所选任务；视频和已完成参考保留。", write=False)

    def _open_board(self):
        selected = self._selected()
        paths = self._paths_for(selected[0]) if len(selected) == 1 else []
        index = self.board_part_box.current()
        path = paths[max(0, index)] if paths and index < len(paths) else None
        if not path:
            self.status.set("请选择已有参考分镜的单个任务；文件缺失时可重新分析。")
            return
        try:
            if self.on_open_board(path) is not False:
                self.status.set("参考分镜已打开；检查后可确认学习或导出 AECPP。")
            else:
                self.status.set("当前工作尚未结束，请稍后打开参考分镜。")
        except Exception as exc:
            self.status.set(f"无法打开参考分镜：{exc}")

    def _choose_movies(self):
        if not self.can_write():
            self.status.set("工程正在迁移或整理缓存，请完成后再添加影片。")
            return
        if self._picker is not None and self._picker.winfo_exists():
            self._picker.lift()
            return
        try:
            movies = available_movies(self.manager.root)
        except (sqlite3.Error, OSError) as exc:
            self.status.set(f"无法读取影片：{exc}")
            return
        if not movies:
            self.status.set("没有可用的本地影片，请先导入视频或恢复缺失的原片。")
            return
        picker = self._picker = tk.Toplevel(self.win)
        picker.title("选择自动学习影片")
        picker.geometry("630x430")
        body = ttk.Frame(picker, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="选择已有视频，无需重新下载；Ctrl / Shift 可多选。", style="Muted.TLabel").pack(anchor="w", pady=(0, 8))
        tree = ttk.Treeview(body, columns=("title", "group"), show="headings", selectmode="extended")
        tree.heading("title", text="影片")
        tree.heading("group", text="数据分组")
        tree.column("title", width=370)
        tree.column("group", width=130)
        tree.pack(fill="both", expand=True)
        for row in movies:
            tree.insert("", "end", iid=str(row["id"]), values=(row["name"], {"train": "训练参考", "val": "验证影片", "time": "自动分组"}.get(row["mode"], row["mode"])))
        def accept():
            ids = [int(key) for key in tree.selection()]
            if not ids:
                return
            result = self._perform(lambda: self.manager.enqueue(ids), f"已提交 {len(ids)} 部影片；重复完成的任务会复用，可选择重新分析更新。")
            if result is not False:
                picker.destroy()
        ttk.Button(body, text="加入自动学习", command=accept, style="Accent.TButton").pack(anchor="e", pady=(10, 0))

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
        # Variable traces register Tcl commands independently of the widgets.
        # Remove them while the owning UI thread still owns the interpreter.
        if self._search_trace is not None:
            try:
                self.search.trace_remove("write", self._search_trace)
            except tk.TclError:
                pass
            self._search_trace = None
        if self._timer is not None:
            try:
                self.win.after_cancel(self._timer)
            except tk.TclError:
                pass
            self._timer = None
        try:
            self.progress.stop()
        except tk.TclError:
            pass
        if self.on_close:
            self.on_close()

    def close(self):
        if not self._closed:
            self._finish_close()
            self.win.destroy()
        return True
