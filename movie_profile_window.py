"""Non-modal, review-first movie research and training preparation."""
import json
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

import movie_profiles as profiles
import theme


STATUS_NAMES = {"draft": "待核对", "confirmed": "已确认", "manual-needed": "待填写"}


class MovieProfileWindow:
    def __init__(self, parent, root, movie_ids, *, on_ready=None, on_close=None):
        self.root = root
        self.on_ready, self.on_close = on_ready, on_close
        self.movie_ids = list(dict.fromkeys(movie_ids))
        if not self.movie_ids:
            raise ValueError("请先选择电影")
        profiles.ensure_schema(root)
        self._closed, self._loading, self._dirty, self._notified = False, False, False, False
        self._timer, self._search_token, self._searching = None, 0, False
        self._events = queue.Queue()
        self._current_id, self._profile, self._candidates = None, None, []
        self.win = tk.Toplevel(parent)
        self.win.title("影片资料 · 学习准备")
        self.win.configure(background=theme.BACKGROUND)
        self.win.geometry("1080x760")
        self.win.minsize(920, 640)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<Destroy>", self._destroyed, add="+")
        self.title = tk.StringVar()
        self.year = tk.StringVar()
        self.allow_missing = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="学习前先核对影片资料；演员、角色和剧情将保存在本工程中。")
        style = ttk.Style(self.win)
        style.configure("Profile.TNotebook", background=theme.PANEL, borderwidth=0)
        style.configure("Profile.TNotebook.Tab", background=theme.PANEL, foreground=theme.MUTED, padding=(12, 5))
        style.map("Profile.TNotebook.Tab", background=[("selected", theme.RAISED)], foreground=[("selected", theme.ACCENT)])
        self._build()
        self.title.trace_add("write", self._changed)
        self.year.trace_add("write", self._changed)
        self.allow_missing.trace_add("write", self._changed)
        self._refresh_movies()
        missing = profiles.required_missing(root, self.movie_ids)
        self._load(missing[0] if missing else self.movie_ids[0])
        self._timer = self.win.after(120, self._poll)

    def _build(self):
        shell = tk.Frame(self.win, bg=theme.BACKGROUND, padx=16, pady=12)
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(1, weight=1)
        shell.rowconfigure(1, weight=1)
        heading = tk.Frame(shell, bg=theme.BACKGROUND)
        heading.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        tk.Label(heading, text="影片资料", bg=theme.BACKGROUND, fg=theme.TEXT,
                 font=(theme.FONT, 21, "bold")).pack(side="left")
        tk.Label(heading, text="先查资料，再核对，最后用于学习", bg=theme.BACKGROUND,
                 fg=theme.MUTED, font=(theme.FONT, 10)).pack(side="left", padx=20)
        self.readiness = tk.Label(heading, text="", bg=theme.BACKGROUND, fg=theme.ACCENT)
        self.readiness.pack(side="right")
        sidebar = ttk.Frame(shell, padding=(10, 10))
        sidebar.grid(row=1, column=0, sticky="nsew", padx=(0, 10))
        sidebar.rowconfigure(1, weight=1)
        sidebar.columnconfigure(0, weight=1)
        ttk.Label(sidebar, text="本次学习影片", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.movies = ttk.Treeview(sidebar, columns=("name",), show="tree", selectmode="browse", height=8)
        self.movies.column("#0", width=175, minwidth=140, stretch=False)
        self.movies.column("name", width=0, minwidth=0, stretch=False)
        self.movies.grid(row=1, column=0, sticky="nsew")
        self.movies.tag_configure("confirmed", foreground=theme.TEAL)
        self.movies.tag_configure("manual-needed", foreground=theme.RED)
        self.movies.bind("<<TreeviewSelect>>", self._selected)
        ttk.Label(sidebar, text="已学习的影片也可补充资料，\n再次学习时会使用新快照。",
                  style="Caption.TLabel").grid(row=2, column=0, sticky="w", pady=(9, 0))
        main = ttk.Frame(shell, padding=(12, 10))
        main.grid(row=1, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(3, weight=1)
        identity = ttk.Frame(main)
        identity.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        identity.columnconfigure(1, weight=1)
        ttk.Label(identity, text="片名").grid(row=0, column=0, padx=(0, 8))
        ttk.Entry(identity, textvariable=self.title).grid(row=0, column=1, sticky="ew")
        ttk.Label(identity, text="年份").grid(row=0, column=2, padx=(12, 8))
        ttk.Entry(identity, textvariable=self.year, width=7).grid(row=0, column=3)
        tools = ttk.Frame(main)
        tools.grid(row=1, column=0, sticky="ew", pady=(0, 9))
        self.search_button = ttk.Button(tools, text="联网查找资料", command=self._search, style="Accent.TButton")
        self.search_button.pack(side="left")
        ttk.Button(tools, text="浏览器中查询 ↗", command=self._browser_search).pack(side="left", padx=6)
        ttk.Button(tools, text="导入资料 JSON", command=self._import_json, style="Compact.TButton").pack(side="right")
        ttk.Button(tools, text="导出资料", command=self._export_json, style="Compact.TButton").pack(side="right", padx=6)
        self.lookup = ttk.Frame(main)
        self.lookup.grid(row=2, column=0, sticky="ew", pady=(0, 9))
        self.lookup.columnconfigure(0, weight=1)
        self.candidates = ttk.Treeview(self.lookup, columns=("title", "note"), show="headings", height=2)
        self.candidates.heading("title", text="网上查询候选 · 请核对同名电影")
        self.candidates.heading("note", text="年份 / 说明")
        self.candidates.column("title", width=230, minwidth=140)
        self.candidates.column("note", width=260, minwidth=150)
        self.candidates.grid(row=0, column=0, sticky="ew")
        candidate_scroll = ttk.Scrollbar(self.lookup, command=self.candidates.yview)
        candidate_scroll.grid(row=0, column=1, sticky="ns")
        self.candidates.configure(yscrollcommand=candidate_scroll.set)
        self.candidates.bind("<Double-1>", lambda _: self._fetch())
        self.apply_candidate = ttk.Button(self.lookup, text="读取所选候选", command=self._fetch, state="disabled")
        self.apply_candidate.grid(row=0, column=2, sticky="ns", padx=(8, 0))
        self.candidates.bind("<<TreeviewSelect>>", lambda _: self.apply_candidate.configure(
            state="normal" if self.candidates.selection() and not self._searching else "disabled"))
        self.lookup.grid_remove()
        self.tabs = ttk.Notebook(main, style="Profile.TNotebook")
        self.tabs.grid(row=3, column=0, sticky="nsew")
        self.cast = self._editor_tab("演员与角色", "每行：角色 | 演员 | 角色别名（多个用逗号）\n只知道演员可写： | 演员名；演员不会被当成角色。")
        self.synopsis = self._editor_tab("故事情节", "填写或修改整部电影的剧情梗概。网页结果仅为待核对资料，请确认对应的是本片。")
        self.events = self._editor_tab("剧情时间段", "可选，每行：开始秒数-结束秒数 | 该片段剧情 | 角色\n例如：120-165 | 两位角色在客栈争论 | 角色甲,角色乙。需按当前视频版本核对时间。")
        self.sources = self._editor_tab("资料来源", "每行一个来源网页链接。网络读取结果保留来源与许可；手工查到资料后也请记录链接。")
        review = ttk.Frame(main)
        review.grid(row=4, column=0, sticky="ew", pady=(9, 6))
        ttk.Checkbutton(review, text="我已核查；暂缺的演员、角色或剧情资料留空，仍继续学习",
                        variable=self.allow_missing).pack(anchor="w")
        ttk.Label(review, text="保存草稿不会进入训练；确认后角色可在标注时点选，修改后的资料用于下一次学习。",
                  style="Caption.TLabel", wraplength=680).pack(anchor="w", pady=(3, 0))
        actions = ttk.Frame(main)
        actions.grid(row=5, column=0, sticky="ew")
        ttk.Button(actions, text="保存草稿", command=lambda: self._save(False)).pack(side="left")
        self.confirm_button = ttk.Button(actions, text="确认资料并继续", command=lambda: self._save(True), style="Primary.TButton")
        self.confirm_button.pack(side="right")
        ttk.Label(shell, textvariable=self.status, style="Muted.TLabel", wraplength=1000).grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))

    def _editor_tab(self, title, hint):
        frame = ttk.Frame(self.tabs, padding=10)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        ttk.Label(frame, text=hint, style="Caption.TLabel", wraplength=650).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 7))
        text = tk.Text(frame, height=7, wrap="word", undo=True, bg=theme.FIELD, fg=theme.TEXT,
                       insertbackground=theme.ACCENT, selectbackground=theme.SELECTED,
                       font=(theme.FONT, 10), padx=9, pady=7, relief="flat", highlightthickness=0)
        text.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, command=text.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        text.configure(yscrollcommand=scroll.set)
        text.bind("<<Modified>>", lambda event, widget=text: self._text_changed(widget))
        self.tabs.add(frame, text=title)
        return text

    def _changed(self, *_):
        if not self._loading:
            self._dirty = True

    def _text_changed(self, widget):
        if widget.edit_modified():
            self._changed()
            widget.edit_modified(False)

    def _refresh_movies(self):
        for movie_id in self.movie_ids:
            profile = profiles.get(self.root, movie_id)
            title = profile["title"]
            text = f"{STATUS_NAMES.get(profile['status'], '待核对')} · {title}"
            if self.movies.exists(str(movie_id)):
                self.movies.item(str(movie_id), text=text, tags=(profile["status"],))
            else:
                self.movies.insert("", "end", iid=str(movie_id), text=text, tags=(profile["status"],))
        missing = profiles.required_missing(self.root, self.movie_ids)
        self.readiness.configure(text=f"已确认 {len(self.movie_ids)-len(missing)} / {len(self.movie_ids)}")

    def add_movies(self, movie_ids):
        self.movie_ids = list(dict.fromkeys([*self.movie_ids, *movie_ids]))
        self._notified = False
        self._refresh_movies()
        self.show()

    def _selected(self, *_):
        selected = self.movies.selection()
        if not selected or int(selected[0]) == self._current_id:
            return
        self._load(int(selected[0]))

    def _load(self, movie_id):
        if self._dirty and self._current_id is not None and not self._save(False, advance=False):
            self.movies.selection_set(str(self._current_id))
            return
        self._search_token += 1
        self._searching = False
        self.search_button.configure(state="normal")
        self.apply_candidate.configure(state="disabled")
        self.candidates.delete(*self.candidates.get_children())
        self.lookup.grid_remove()
        self._candidates = []
        self._current_id = movie_id
        self._set_fields(profiles.get(self.root, movie_id))
        self.movies.selection_set(str(movie_id))
        self.movies.see(str(movie_id))
        self.status.set(self._profile.get("research", {}).get("message") or "核对角色、演员和剧情后确认；未知资料可以手工填写。")
        if not profiles.is_ready(self._profile) and not self._profile.get("research", {}).get("attempted_at"):
            self._search()

    def _set_fields(self, profile):
        self._loading = True
        try:
            self._profile = profile
            self.title.set(profile["title"])
            self.year.set(profile.get("year", ""))
            self.allow_missing.set(profile.get("allow_missing", False))
            for widget, text in ((self.cast, profiles.format_cast(profile.get("cast", []))),
                                 (self.synopsis, profile.get("synopsis", "")),
                                 (self.events, profiles.format_events(profile.get("events", []))),
                                 (self.sources, "\n".join(source["url"] for source in profile.get("sources", [])))):
                widget.delete("1.0", "end")
                widget.insert("1.0", text)
                widget.edit_modified(False)
        finally:
            self._loading = False
            self._dirty = False

    def _fields(self):
        prior = {source["url"]: source for source in self._profile.get("sources", [])}
        return {**self._profile, "title": self.title.get(), "year": self.year.get(),
                "cast": profiles.parse_cast(self.cast.get("1.0", "end-1c")),
                "synopsis": self.synopsis.get("1.0", "end-1c"),
                "events": profiles.parse_events(self.events.get("1.0", "end-1c")),
                "sources": [prior.get(url, {"url": url}) for line in self.sources.get("1.0", "end-1c").splitlines() if (url := line.strip())]}

    def _save(self, confirm, *, advance=True):
        try:
            profile = profiles.save(self.root, self._current_id, self._fields(), confirm=confirm,
                                    allow_missing=self.allow_missing.get())
            self._profile, self._dirty = profile, False
            self._refresh_movies()
            self.status.set("资料已确认，将用于之后的学习。" if confirm else "草稿已保存；确认后才能进入训练。")
            if confirm and advance:
                missing = profiles.required_missing(self.root, self.movie_ids)
                if missing:
                    self._load(missing[0])
                elif self.on_ready and not self._notified:
                    self._notified = True
                    try:
                        self.on_ready()
                    except Exception:
                        self._notified = False
                        raise
            return True
        except Exception as exc:
            self.status.set(str(exc))
            messagebox.showerror("影片资料", str(exc), parent=self.win)
            return False

    def _worker(self, kind, action):
        self._search_token += 1
        token, movie_id = self._search_token, self._current_id
        self._searching = True
        self.search_button.configure(state="disabled")
        self.apply_candidate.configure(state="disabled")
        def work():
            try:
                result = action()
                self._events.put((token, movie_id, kind, result, None))
            except Exception as exc:
                self._events.put((token, movie_id, kind, None, str(exc)))
        threading.Thread(target=work, name="film-profile-research", daemon=True).start()

    def _search(self):
        if self._searching or not self.title.get().strip():
            return
        if self._dirty and not self._save(False, advance=False):
            return
        title, year = self.title.get(), self.year.get()
        self.status.set("正在查询公共影片资料… 可继续编辑；网络失败时可手动填写。")
        self._worker("search", lambda: profiles.search_online(title, year))

    def _fetch(self):
        selected = self.candidates.selection()
        if self._searching or not selected:
            return
        candidate = self._candidates[int(selected[0])]
        self.status.set("正在读取所选电影的演员、角色与剧情；读取后仍需人工核对。")
        self._worker("fetch", lambda: profiles.fetch_candidate(candidate))

    def _poll(self):
        if self._closed:
            return
        self._timer = None
        try:
            self._receive()
        except Exception as exc:
            self.status.set("资料未应用，请检查当前输入后重试："+str(exc))
        finally:
            if not self._closed:
                self._timer = self.win.after(120, self._poll)

    def _receive(self):
        while not self._events.empty():
            token, movie_id, kind, result, error = self._events.get_nowait()
            if token != self._search_token or movie_id != self._current_id:
                continue
            self._searching = False
            self.search_button.configure(state="normal")
            if error:
                result = {"query": self.title.get(), "status": "failed", "message": "在线资料读取失败："+error+"；请手动填写或重试。"}
                kind = "search"
            if kind == "search":
                self._candidates = result.get("candidates", [])
                self.candidates.delete(*self.candidates.get_children())
                for index, candidate in enumerate(self._candidates):
                    self.candidates.insert("", "end", iid=str(index), values=(candidate["title"], candidate["description"]))
                self.lookup.grid() if self._candidates else self.lookup.grid_remove()
                recorded = profiles.record_research(self.root, movie_id, result)
                self._profile["research"] = recorded["research"]
                self._profile["status"] = recorded["status"]
                self.status.set(result["message"])
            else:
                # Network suggestions never erase unsaved edits or silently replace a confirmed profile.
                current = self._fields()
                has_content = self._dirty or bool(current.get("cast") or current.get("synopsis") or current.get("events"))
                if has_content and not messagebox.askyesno("采用网上资料", "将用所选候选的演员表和剧情替换当前编辑内容。是否采用？\n已有训练快照不受影响。", parent=self.win):
                    self.status.set("已保留当前编辑内容。可以打开候选来源核对后手工补充。")
                    continue
                result["movie_id"] = movie_id
                # Time alignment is authored for this video version, not supplied by Wikidata.
                result["events"] = current.get("events", [])
                self._set_fields(result)
                self.lookup.grid_remove()
                self._dirty = True
                self._save(False, advance=False)
                self.status.set(result["research"]["message"])
            self._refresh_movies()

    def _browser_search(self):
        webbrowser.open(profiles.search_url(self.title.get(), self.year.get()), new=2)

    def _import_json(self):
        path = filedialog.askopenfilename(parent=self.win, title="导入影片资料", filetypes=[("JSON 资料", "*.json")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8-sig") as stream:
                data = json.load(stream)
            if not isinstance(data, dict):
                raise ValueError("资料文件须包含一个 JSON 对象")
            profile = profiles.clean_profile(data, self._current_id)
            profile["status"] = "draft"
            self._set_fields(profile)
            self._dirty = True
            self._save(False, advance=False)
            self.status.set("资料已导入为草稿，请核对并确认。")
        except Exception as exc:
            messagebox.showerror("导入资料", str(exc), parent=self.win)

    def _export_json(self):
        path = filedialog.asksaveasfilename(parent=self.win, title="导出影片资料", defaultextension=".json",
                                           initialfile="影片资料.json", filetypes=[("JSON 资料", "*.json")])
        if path:
            try:
                profile = profiles.clean_profile(self._fields(), self._current_id)
                with open(path, "w", encoding="utf-8") as stream:
                    json.dump(profile, stream, ensure_ascii=False, indent=2)
                self.status.set("资料已导出，可在其他工程导入后重新核对。")
            except Exception as exc:
                messagebox.showerror("导出资料", str(exc), parent=self.win)

    def show(self):
        if not self._closed:
            self.win.deiconify()
            self.win.lift()

    def _destroyed(self, event):
        if event.widget == self.win:
            self._finish_close()

    def _finish_close(self):
        if self._closed:
            return
        self._closed = True
        self._search_token += 1
        if self._timer:
            try:
                self.win.after_cancel(self._timer)
            except tk.TclError:
                pass
            self._timer = None
        if self.on_close:
            self.on_close()

    def close(self):
        if self._closed:
            return True
        if self._dirty and not self._save(False, advance=False):
            if not messagebox.askyesno("放弃未保存资料", "当前内容未能保存，是否关闭并放弃这些修改？", parent=self.win):
                return False
        self._finish_close()
        self.win.destroy()
        return True
