"""Text-to-storyboard workspace with explicit correction learning and AECPP export."""
import copy
import json
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import uuid

import theme

ACTIONS = {"idle": "站立", "talk": "说话", "walk": "走路", "run": "跑步", "jump": "跳跃", "fall": "摔倒", "shake": "抖动"}
EMOTIONS = {"neutral": "平静", "happy": "开心", "angry": "生气", "sad": "伤心", "shocked": "震惊"}
CAMERAS = {"wide": "全景", "medium": "中景", "close": "近景"}
FORMATS = {"自动识别": "auto", "小说 / 普通文本": "novel", "角色：台词": "script", "Fountain 剧本": "fountain"}
EXAMPLE = "场景：客厅\n小明：我决定了，今天开始早睡！\n小王：现在几点？\n小明看了看手机，突然愣住。\n小明：凌晨三点。\n小王：你这是提前开始明天的熬夜啊！\n小明震惊得跳了起来。"


class StoryWindow:
    def __init__(self, parent, root, *, container=None, on_close=None, can_write=lambda: True):
        self.root = Path(root).resolve()
        self.on_close, self.can_write = on_close, can_write
        self.embedded = container is not None
        self.busy = False
        self._closed = False
        self._timer = None
        self._events = queue.Queue()
        self._worker = None
        self._active_kind = None
        self._generation_stop = None
        self._last_generate_backup = None
        self._last_animation_output = None
        self._automation_warnings = []
        self._selected = None
        self._loading = False
        self.board = None
        self.resource_catalog = None
        self.resource_config = {}
        self._restored_resource_root = ""
        self._resource_rows = {}
        self._resource_options = {}
        self._buttons = []
        self.win = ttk.Frame(container) if self.embedded else tk.Toplevel(parent)
        if not self.embedded:
            self.win.title("小说成片 · AECPP")
            self.win.geometry("1100x740")
            self.win.minsize(900, 620)
            self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<Destroy>", self._destroyed, add="+")
        self.title = tk.StringVar(self.win, value="我的沙雕动画")
        self.source_format = tk.StringVar(self.win, value="自动识别")
        self.sound_enabled = tk.BooleanVar(self.win,value=os.name=='nt')
        self.status = tk.StringVar(self.win, value="粘贴小说或剧本 → 一键生成动画；自动匹配 AECPP 现有角色、动作、表情与场景。")
        self.summary = tk.StringVar(self.win, value="尚未生成分镜")
        self.scene = tk.StringVar(self.win)
        self.speaker = tk.StringVar(self.win, value="旁白")
        self.actor = tk.StringVar(self.win, value="自动 / 无")
        self.action = tk.StringVar(self.win, value=ACTIONS["idle"])
        self.emotion = tk.StringVar(self.win, value=EMOTIONS["neutral"])
        self.camera = tk.StringVar(self.win, value=CAMERAS["medium"])
        self.duration = tk.StringVar(self.win, value="3")
        self.resource_root = tk.StringVar(self.win)
        self.resource_choice = tk.StringVar(self.win)
        self.resource_summary = tk.StringVar(self.win,value="选择 AECPP 现有素材目录，再扫描角色和背景。")
        self._build()
        self._restore()
        self._timer = self.win.after(100, self._poll)

    @property
    def draft_path(self):
        return self.root / "creations" / "story_draft.json"

    @property
    def board_path(self):
        return self.root / "creations" / "current_story.json"

    def _button(self, parent, text, command, **kwargs):
        button = ttk.Button(parent, text=text, command=command, **kwargs)
        self._buttons.append(button)
        return button

    def _build(self):
        self.body = ttk.Frame(self.win, padding=(16, 12))
        self.body.pack(fill="both", expand=True)
        self.body.columnconfigure(0, weight=1)
        self.body.rowconfigure(2, weight=1)
        head = ttk.Frame(self.body)
        head.grid(row=0, column=0, sticky="ew")
        ttk.Label(head, text="小说成片", style="Title.TLabel").pack(side="left")
        ttk.Label(head, text="可编辑分镜 · 组合 AECPP 现有素材", style="Muted.TLabel").pack(side="left", padx=12)
        title_row = ttk.Frame(self.body)
        title_row.grid(row=1, column=0, sticky="ew", pady=10)
        title_row.columnconfigure(1, weight=1)
        ttk.Label(title_row, text="作品名").grid(row=0, column=0, padx=(0, 8))
        self.title_entry = ttk.Entry(title_row, textvariable=self.title)
        self.title_entry.grid(row=0, column=1, sticky="ew")
        self._button(title_row, "保存草稿", self._save).grid(row=0, column=2, padx=(10, 0))
        self.open_output_button = self._button(title_row, "打开生成目录", self._open_animation_directory, state="disabled")
        self.open_output_button.grid(row=0, column=3, padx=(8, 0))
        self.tabs = ttk.Notebook(self.body)
        self.tabs.grid(row=2, column=0, sticky="nsew")
        source_page = ttk.Frame(self.tabs, padding=12)
        board_page = ttk.Frame(self.tabs, padding=12)
        self.resources_page = ttk.Frame(self.tabs,padding=12)
        self.tabs.add(source_page, text="1  小说 / 剧本")
        self.tabs.add(board_page, text="2  分镜与动作")
        self.tabs.add(self.resources_page,text="3  素材与组装")
        source_page.columnconfigure(0, weight=1)
        source_page.rowconfigure(2, weight=1)
        bar = ttk.Frame(source_page)
        bar.grid(row=0, column=0, sticky="ew")
        self._button(bar, "导入文本…", self._import_text).pack(side="left")
        self._button(bar, "填入示例", self._example).pack(side="left", padx=8)
        self.format_box = ttk.Combobox(bar, textvariable=self.source_format, values=list(FORMATS), state="readonly", width=18)
        self.format_box.pack(side="left")
        self.oneclick_button = self._button(bar, "一键生成动画", self._generate_animation, style="Primary.TButton")
        self.oneclick_button.pack(side="right")
        self.cancel_generation_button = ttk.Button(bar, text="取消生成", command=self._cancel_generation, state="disabled")
        self.cancel_generation_button.pack(side="right", padx=6)
        self.generate_button = self._button(bar, "先生成分镜精调", self._generate)
        self.generate_button.pack(side="right", padx=(6, 0))
        ttk.Label(source_page, text="可直接粘贴正文；剧本每行写「角色：台词」。未识别说话人的文字保留为旁白，可在分镜中修改。",
                  style="Muted.TLabel", wraplength=810).grid(row=1, column=0, sticky="w", pady=8)
        self.source = self._text(source_page, height=12)
        self.source.master.grid(row=2, column=0, sticky="nsew")
        ttk.Label(source_page, text="自动复用素材设置并保存可编辑工程；缺少动作时会说明替代方案。可在“素材与组装”选择素材目录，生成后仍可调整分镜与映射。",
                  style="Caption.TLabel", wraplength=810).grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.sound_check=ttk.Checkbutton(source_page,text="自动配音、口型和音效（本机中文语音）",variable=self.sound_enabled)
        self.sound_check.grid(row=4,column=0,sticky='w',pady=(4,0))
        board_page.rowconfigure(1, weight=1)
        board_page.columnconfigure(0, weight=1)
        ttk.Label(board_page, textvariable=self.summary, style="Muted.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
        content = ttk.Panedwindow(board_page, orient="horizontal")
        content.grid(row=1, column=0, sticky="nsew")
        listing = ttk.Frame(content)
        editor = ttk.Frame(content, padding=(12, 0, 0, 0))
        content.add(listing, weight=3)
        content.add(editor, weight=2)
        listing.rowconfigure(0, weight=1)
        listing.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(listing, columns=("speaker", "text", "duration"), show="headings", selectmode="browse", height=8)
        for name, label, width in (("speaker", "角色", 80), ("text", "台词 / 旁白", 230), ("duration", "秒", 45)):
            self.tree.heading(name, text=label)
            self.tree.column(name, width=width, minwidth=40, stretch=name=="text")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(listing, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<<TreeviewSelect>>", lambda _: self._selection())
        controls = ttk.Frame(listing)
        controls.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        for text, command in (("上移", lambda: self._move(-1)), ("下移", lambda: self._move(1)),
                              ("新增镜头", self._add), ("删除镜头", self._delete)):
            self._button(controls, text, command, style="Compact.TButton").pack(side="left", padx=(0, 6))
        editor.columnconfigure(1, weight=1)
        editor.columnconfigure(3, weight=1)
        editor.rowconfigure(2, weight=1)
        ttk.Label(editor, text="场景").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.scene_entry = ttk.Entry(editor, textvariable=self.scene)
        self.scene_entry.grid(row=0, column=1, columnspan=3, sticky="ew", pady=3)
        ttk.Label(editor, text="说话人").grid(row=1, column=0, sticky="w")
        self.speaker_box = ttk.Combobox(editor, textvariable=self.speaker, values=["旁白"], width=8)
        self.speaker_box.grid(row=1, column=1, sticky="ew", pady=3)
        ttk.Label(editor, text="动作角色").grid(row=1, column=2, sticky="w", padx=6)
        self.actor_box = ttk.Combobox(editor, textvariable=self.actor, values=["自动 / 无"], width=8)
        self.actor_box.grid(row=1, column=3, sticky="ew", pady=3)
        self.dialogue = self._text(editor, height=4, width=22)
        self.dialogue.master.grid(row=2, column=0, columnspan=4, sticky="nsew", pady=6)
        self.edit_boxes = []
        for row, column, label, variable, values in ((3, 0, "动作", self.action, ACTIONS), (3, 2, "表情", self.emotion, EMOTIONS), (4, 0, "景别", self.camera, CAMERAS)):
            ttk.Label(editor, text=label).grid(row=row, column=column, sticky="w", padx=(6 if column else 0, 0))
            box = ttk.Combobox(editor, textvariable=variable, values=list(values.values()), state="readonly", width=8)
            box.grid(row=row, column=column+1, sticky="ew", pady=3)
            self.edit_boxes.append(box)
        ttk.Label(editor, text="秒数").grid(row=4, column=2, sticky="w", padx=6)
        self.duration_entry = ttk.Entry(editor, textvariable=self.duration, width=8)
        self.duration_entry.grid(row=4, column=3, sticky="ew", pady=3)
        self._button(editor, "应用当前镜头", self._apply, style="Accent.TButton").grid(row=5, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        footer = ttk.Frame(self.body)
        footer.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self.learn_button = self._button(footer, "确认修正并学习", self._train, style="Accent.TButton")
        self.learn_button.pack(side="left")
        self.script_export_button = self._button(footer, "导出角色剧本…", self._export_script)
        self.script_export_button.pack(side="left", padx=8)
        self.draft_export_button = self._button(footer,"导出示意草稿…",self._export)
        self.draft_export_button.pack(side="left")
        self.export_button = self._button(footer, "用现有素材组装 AECPP…", self._export_resources, style="Primary.TButton")
        self.export_button.pack(side="right")
        self._build_resources()
        self.status_text = self._text(self.body, height=2)
        self.status_text.master.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        self.status_text.configure(state="disabled", foreground=theme.MUTED)
        self._status_trace = self.status.trace_add("write", self._status_changed)
        self._status_changed()

    def _build_resources(self):
        body = self.resources_page
        body.columnconfigure(0,weight=1)
        body.rowconfigure(2,weight=1)
        toolbar = ttk.Frame(body)
        toolbar.grid(row=0,column=0,sticky="ew")
        toolbar.columnconfigure(1,weight=1)
        ttk.Label(toolbar,text="AECPP 素材目录").grid(row=0,column=0,padx=(0,8))
        self.resource_root_entry = ttk.Entry(toolbar,textvariable=self.resource_root)
        self.resource_root_entry.grid(row=0,column=1,sticky="ew")
        self.choose_resources_button = self._button(toolbar,"选择目录…",self._choose_resources,style="Compact.TButton")
        self.choose_resources_button.grid(row=0,column=2,padx=(8,0))
        self.scan_resources_button = self._button(toolbar,"扫描素材",self._scan_resources,style="Compact.TButton")
        self.scan_resources_button.grid(row=0,column=3,padx=(8,0))
        ttk.Label(body,textvariable=self.resource_summary,style="Caption.TLabel",wraplength=850,justify="left").grid(row=1,column=0,sticky="ew",pady=8)
        panels = ttk.Frame(body)
        panels.grid(row=2,column=0,sticky="nsew")
        panels.columnconfigure(0,weight=2,minsize=440)
        panels.columnconfigure(1,weight=1,minsize=280)
        panels.rowconfigure(0,weight=1)
        mapping = ttk.Frame(panels)
        mapping.grid(row=0,column=0,sticky="nsew",padx=(0,10))
        mapping.columnconfigure(0,weight=1)
        mapping.rowconfigure(0,weight=1)
        self.resource_tree = ttk.Treeview(mapping,columns=("kind","story","asset"),show="headings",selectmode="browse",height=6)
        for key,label,width in (("kind","用途",60),("story","故事中的要求",160),("asset","选用的现有素材",200)):
            self.resource_tree.heading(key,text=label)
            self.resource_tree.column(key,width=width,minwidth=50,stretch=key!="kind")
        self.resource_tree.grid(row=0,column=0,sticky="nsew")
        scroll = ttk.Scrollbar(mapping,command=self.resource_tree.yview)
        scroll.grid(row=0,column=1,sticky="ns")
        self.resource_tree.configure(yscrollcommand=scroll.set)
        self.resource_tree.bind("<<TreeviewSelect>>",lambda _:self._select_resource_mapping())
        choices = ttk.Frame(mapping)
        choices.grid(row=1,column=0,columnspan=2,sticky="ew",pady=(8,0))
        choices.columnconfigure(0,weight=1)
        self.resource_box = ttk.Combobox(choices,textvariable=self.resource_choice,state="readonly")
        self.resource_box.grid(row=0,column=0,sticky="ew")
        self.bind_resource_button = self._button(choices,"应用映射",self._apply_resource_mapping,style="Compact.TButton")
        self.bind_resource_button.grid(row=0,column=1,padx=(8,0))
        inventory = ttk.Frame(panels)
        inventory.grid(row=0,column=1,sticky="nsew")
        inventory.columnconfigure(0,weight=1)
        inventory.rowconfigure(0,weight=1)
        inventory.rowconfigure(1,weight=1)
        self.inventory_tree = ttk.Treeview(inventory,show="tree",height=4)
        self.inventory_tree.column("#0",width=270,minwidth=220)
        self.inventory_tree.grid(row=0,column=0,sticky="nsew")
        scroll = ttk.Scrollbar(inventory,command=self.inventory_tree.yview)
        scroll.grid(row=0,column=1,sticky="ns")
        self.inventory_tree.configure(yscrollcommand=scroll.set)
        self.resource_detail = self._text(inventory,height=5,width=28)
        self.resource_detail.master.grid(row=1,column=0,columnspan=2,sticky="nsew",pady=(8,0))
        self.resource_detail.configure(state="disabled")
        controls = ttk.Frame(body)
        controls.grid(row=3,column=0,sticky="ew",pady=(8,0))
        ttk.Label(controls,text="选择表中的角色、动作、表情或场景，再绑定现有素材。",style="Caption.TLabel").pack(side="left")
        self.save_resources_button = self._button(controls,"保存素材设置",self._save_resources,style="Compact.TButton")
        self.save_resources_button.pack(side="right")
        self.check_resources_button = self._button(controls,"检查缺失",self._check_resources,style="Compact.TButton")
        self.check_resources_button.pack(side="right",padx=8)

    def _status_changed(self, *_):
        if self._closed:
            return
        self.status_text.configure(state="normal")
        self.status_text.delete("1.0", "end")
        self.status_text.insert("1.0", self.status.get())
        self.status_text.configure(state="disabled")

    @staticmethod
    def _text(parent, **kwargs):
        frame = ttk.Frame(parent)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        text = tk.Text(frame, wrap="word", undo=True, background=theme.FIELD, foreground=theme.TEXT,
                       insertbackground=theme.TEXT, selectbackground=theme.SELECTED, selectforeground=theme.TEXT, **kwargs)
        text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=scroll.set)
        return text

    def _restore(self):
        try:
            if self.draft_path.is_file():
                draft = json.loads(self.draft_path.read_text(encoding="utf-8"))
                self.title.set(draft.get("title", "我的沙雕动画"))
                self.source.insert("1.0", draft.get("source", ""))
                self.source_format.set(draft.get("format", "自动识别"))
                if type(draft.get('sound_enabled')) is bool:self.sound_enabled.set(draft['sound_enabled'])
                output = draft.get("last_animation_output")
                if output and Path(output).is_file():
                    self._last_animation_output = Path(output)
                    self.open_output_button.configure(state="normal")
            if self.board_path.is_file():
                from story_pipeline import load_storyboard
                self.board = load_storyboard(self.board_path)
                self.title.set(self.board["title"])
                self._render()
                provenance = self.board.get("provenance") or {}
                note = "自动参考的动作、表情和景别仍需核对。" if provenance.get("review_status") == "unreviewed" else ""
                self.status.set("已恢复上次分镜，可继续编辑或导出。" + note)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.status.set("草稿未能读取：" + str(exc))
        try:
            from aecpp_resources import load_bindings
            self.resource_config = load_bindings(self.root)
            self.resource_root.set(self.resource_config.get("resource_root",""))
            self._restored_resource_root = self.resource_root.get().strip()
            if self.board and "character_names" not in self.resource_config:
                self.resource_config["character_names"] = {row["id"]: row["name"] for row in self.board["characters"]}
            if self.resource_root.get():
                self.resource_summary.set("已恢复素材目录和映射；点击“扫描素材”核对本机文件后再组装。")
            self._render_resource_mappings()
        except (OSError,ValueError) as exc:
            self.resource_summary.set("素材设置未能读取："+str(exc))

    def _allowed(self):
        if self.busy:
            self.status.set("请等待当前生成、学习或导出任务结束。")
            return False
        if not self.can_write():
            self.status.set("工程正在迁移或整理资源，请等待完成后再操作。")
            return False
        return not self._closed

    def _fail(self, exc):
        self.status.set(str(exc))
        messagebox.showerror("小说成片", str(exc), parent=self.win)
        return False

    def _save_draft(self):
        self.draft_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.draft_path.with_suffix(".tmp")
        temp.write_text(json.dumps({"version": 1, "title": self.title.get(), "source": self.source.get("1.0", "end-1c"),
                                    "format": self.source_format.get(),
                                    "sound_enabled": bool(self.sound_enabled.get()),
                                    "last_animation_output": str(self._last_animation_output) if self._last_animation_output else None},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.draft_path)

    def save_current(self):
        if not self._allowed():
            return False
        try:
            if not self._commit_selected():
                return False
            self._save_draft()
            if self.board:
                from story_pipeline import save_storyboard
                self.board["title"] = self.title.get().strip() or "我的沙雕动画"
                save_storyboard(self.board, self.board_path)
            return True
        except Exception as exc:
            return self._fail(exc)

    def _save(self):
        if self.save_current():
            self.status.set("正文和分镜草稿已保存，下次打开工程可继续。")

    def open_board(self, path):
        """Open a selected analysis draft, preserving the user's current work first."""
        if not self._allowed():
            return False
        try:
            from story_pipeline import load_storyboard
            path = Path(path)
            if path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("分镜文件超过 8 MiB，请按章节打开。")
            incoming = load_storyboard(path)
            had_content = self.board is not None or bool(self.source.get("1.0", "end-1c").strip())
            if not self.save_current():
                return False
            backup = None
            if had_content:
                backup = self.root / "creations" / "draft_history" / uuid.uuid4().hex
                backup.mkdir(parents=True)
                for old, name in ((self.draft_path, "story_draft.json"), (self.board_path, "current_story.json")):
                    if old.is_file():
                        (backup / name).write_bytes(old.read_bytes())
            self._last_open_backup = backup
            self.board = incoming
            self._selected = None
            self.title.set(incoming["title"])
            names = {row["id"]: row["name"] for row in incoming["characters"]}
            source = incoming.get("source_text") or "\n".join(
                (names[shot["speaker"]] + "：" if shot["speaker"] else "") + shot["text"]
                for shot in incoming["shots"])
            self.source.delete("1.0", "end")
            self.source.insert("1.0", source)
            selected_format = next((label for label, value in FORMATS.items() if value == incoming.get("source_format")), "自动识别")
            self.source_format.set(selected_format)
            self._render()
            self.tabs.select(1)
            if not self.save_current():
                return False
            provenance = incoming.get("provenance") or {}
            note = ("这是自动参考草稿：动作、表情和景别是规则建议；核对后点击“确认修正并学习”。"
                    if provenance.get("kind") == "automatic-video-reference" else "可继续编辑并导出 AECPP。")
            self.status.set("已打开分镜。" + note + (f" 原草稿已备份至：{backup}" if backup else ""))
            self.show()
            return True
        except Exception as exc:
            return self._fail(exc)

    def _example(self):
        if not self._allowed():
            return
        if self.source.get("1.0", "end-1c").strip() and not messagebox.askyesno("替换正文", "用示例替换当前正文？已有分镜保留至重新生成。", parent=self.win):
            return
        self.source.delete("1.0", "end")
        self.source.insert("1.0", EXAMPLE)
        self.title.set("早睡大作战")
        self.source_format.set("自动识别")

    def _import_text(self):
        if not self._allowed():
            return
        filename = filedialog.askopenfilename(parent=self.win, title="导入小说或剧本", filetypes=[("文本与剧本", "*.txt *.md *.fountain")])
        if not filename:
            return
        try:
            from subtitles import decode_text
            path = Path(filename)
            if path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError("文本超过 4 MB，请按章节导入。")
            content = decode_text(path.read_bytes())
            if self.source.get("1.0", "end-1c").strip() and not messagebox.askyesno("替换正文", "用导入文本替换当前正文？已有分镜保留至重新生成。", parent=self.win):
                return
            self.source.delete("1.0", "end")
            self.source.insert("1.0", content)
            self.title.set(path.stem)
            self.source_format.set("Fountain 剧本" if path.suffix.lower()==".fountain" else "自动识别")
            self._save_draft()
        except Exception as exc:
            self._fail(exc)

    def _run(self, kind, action):
        if not self._allowed():
            return False
        self.busy = True
        self._active_kind = kind
        self._set_busy(True)
        self.status.set({"generate": "正在生成分镜…", "train": "正在学习已确认的分镜修正…", "export": "正在生成图形示意草稿…", "export-script": "正在导出 AECPP 角色剧本…",
                         "scan-resources":"正在扫描 AECPP 现有角色、动作、表情和背景…","export-resources":"正在用现有素材组装 AECPP 工程…",
                         "oneclick":"正在准备分镜、自动匹配现有素材并生成可编辑动画…"}[kind])
        events = self._events
        def work():
            try:
                events.put((kind, action(), None))
            except Exception as exc:
                events.put((kind, None, str(exc)))
        self._worker = threading.Thread(target=work, daemon=True, name="story-"+kind)
        self._worker.start()
        return True

    def _set_busy(self, value):
        self.sound_check.configure(state='disabled' if value else 'normal')
        for button in self._buttons:
            button.configure(state="disabled" if value else "normal")
        for entry in (self.title_entry, self.scene_entry, self.duration_entry, self.speaker_box, self.actor_box,self.resource_root_entry):
            entry.configure(state="disabled" if value else "normal")
        for box in [self.format_box,self.resource_box, *self.edit_boxes]:
            box.configure(state="disabled" if value else "readonly")
        for widget in (self.source, self.dialogue):
            widget.configure(state="disabled" if value else "normal")
        self.open_output_button.configure(state="disabled" if value or not self._last_animation_output else "normal")
        self.cancel_generation_button.configure(state="normal" if value and self._active_kind=="oneclick" and self._generation_stop and not self._generation_stop.is_set() else "disabled")

    def _generate_animation(self):
        """Snapshot every Tk value before a single background generation pipeline."""
        if not self._allowed():
            return False
        source = self.source.get("1.0", "end-1c")
        if not source.strip():
            self.status.set("请先粘贴或导入小说、剧本正文。")
            self.tabs.select(0)
            return False
        if not self.save_current():
            return False
        try:
            backup = self.root / "creations" / "draft_history" / uuid.uuid4().hex
            backup.mkdir(parents=True)
            for old in (self.draft_path, self.board_path):
                if old.is_file():
                    (backup / old.name).write_bytes(old.read_bytes())
            self._last_generate_backup = backup
            root, title = self.root, self.title.get().strip() or "我的沙雕动画"
            source_format = FORMATS.get(self.source_format.get(), "auto")
            resource_root = self.resource_root.get().strip() or None
            # A restored preference may point at an old machine. Let the
            # coordinator discover its replacement; a newly entered path is
            # explicit and must still fail visibly if it is invalid.
            if resource_root == self._restored_resource_root:
                resource_root = None
            bindings = copy.deepcopy(self.resource_config)
            catalog = copy.deepcopy(self.resource_catalog)
            sound={'enabled':bool(self.sound_enabled.get()),'effects':True}
            events, stop = self._events, threading.Event()
            self._generation_stop = stop
            def work():
                from story_automation import generate_animation
                return generate_animation(root, source, title=title, source_format=source_format,
                    resource_root=resource_root, bindings=bindings, catalog=catalog,
                    report=lambda text: events.put(("progress", text, None)), stop=stop,sound=sound)
            started = self._run("oneclick", work)
            if not started:
                self._generation_stop = None
            return started
        except Exception as exc:
            self._generation_stop = None
            return self._fail(exc)

    def _cancel_generation(self):
        if not self.busy or self._active_kind!="oneclick" or not self._generation_stop:
            return False
        self._generation_stop.set()
        self.cancel_generation_button.configure(state="disabled")
        self.status.set("正在取消本次生成，等待当前步骤停止；正文和原分镜已保存并备份。")
        return True

    def _open_animation_directory(self):
        if not self._allowed() or not self._last_animation_output:
            return False
        try:
            directory = self._last_animation_output.parent
            if not directory.is_dir():
                raise ValueError("生成目录已移动或删除，请重新生成动画。")
            os.startfile(str(directory))
            return True
        except Exception as exc:
            return self._fail(exc)

    def _generate(self):
        if not self._allowed() or not self.save_current():
            return
        source, title = self.source.get("1.0", "end-1c"), self.title.get().strip()
        format_value = FORMATS.get(self.source_format.get(), "auto")
        if self.board and not messagebox.askyesno("重新生成分镜", "重新生成会替换当前分镜修正，继续吗？", parent=self.win):
            return
        root = self.root
        def work():
            from story_pipeline import generate_project_storyboard
            return generate_project_storyboard(root, source, title=title or "我的沙雕动画", source_format=format_value)
        self._run("generate", work)

    def _render(self, selected=None):
        self._loading = True
        try:
            self.tree.delete(*self.tree.get_children())
            names = {character["id"]: character["name"] for character in self.board["characters"]} if self.board else {}
            self.speaker_box.configure(values=["旁白", *names.values()])
            self.actor_box.configure(values=["自动 / 无", *names.values()])
            for shot in self.board["shots"] if self.board else []:
                self.tree.insert("", "end", iid=shot["id"], values=(names.get(shot["speaker"], "旁白"), shot["text"].replace("\n", " "), f"{shot['duration']:g}"))
            shots = self.board["shots"] if self.board else []
            self.summary.set(f"{len(shots)} 个镜头 · {len(names)} 个角色 · 预计 {sum(s['duration'] for s in shots):.1f} 秒；输入新说话人名可添加角色")
            self._selected = None
            if shots:
                target = selected if selected and self.tree.exists(selected) else shots[0]["id"]
                self.tree.selection_set(target)
                self.tree.see(target)
                self._load_shot(target)
        finally:
            self._loading = False
        self._render_resource_mappings()

    def _load_shot(self, identifier):
        shot = next(s for s in self.board["shots"] if s["id"]==identifier)
        self._selected = identifier
        self.scene.set(shot["scene"])
        self.speaker.set(next((c["name"] for c in self.board["characters"] if c["id"]==shot["speaker"]), "旁白"))
        self.actor.set(next((c["name"] for c in self.board["characters"] if c["id"]==shot.get("actor", shot["speaker"])), "自动 / 无"))
        self.action.set(ACTIONS[shot["action"]])
        self.emotion.set(EMOTIONS[shot["emotion"]])
        self.camera.set(CAMERAS[shot["camera"]])
        self.duration.set(f"{shot['duration']:g}")
        self.dialogue.delete("1.0", "end")
        self.dialogue.insert("1.0", shot["text"])

    def _selection(self):
        if self._loading or not self.board:
            return
        if self.busy:
            if self._selected and self.tree.selection() != (self._selected,):
                self.tree.selection_set(self._selected)
            return
        selected = self.tree.selection()
        if not selected or selected[0]==self._selected:
            return
        if not self._commit_selected():
            if self._selected:
                self.tree.selection_set(self._selected)
            return
        self._load_shot(selected[0])

    def _commit_selected(self):
        if not self.board or not self._selected:
            return True
        try:
            from story_pipeline import validate_storyboard
            updated = copy.deepcopy(self.board)
            shot = next(s for s in updated["shots"] if s["id"]==self._selected)
            name = self.speaker.get().strip()
            identifier = ""
            if name and name != "旁白":
                character = next((c for c in updated["characters"] if c["name"]==name), None)
                if character is None:
                    character = {"id": "char_"+uuid.uuid4().hex[:12], "name": name}
                    updated["characters"].append(character)
                identifier = character["id"]
            actor_name = self.actor.get().strip()
            actor_id = ""
            if actor_name and actor_name != "自动 / 无":
                actor = next((c for c in updated["characters"] if c["name"]==actor_name), None)
                if actor is None:
                    actor = {"id": "char_"+uuid.uuid4().hex[:12], "name": actor_name}
                    updated["characters"].append(actor)
                actor_id = actor["id"]
            old_actor = shot.get("actor", shot["speaker"])
            if identifier != shot["speaker"] and actor_id == old_actor == shot["speaker"]:
                actor_id = identifier
            shot.update(scene=self.scene.get().strip(), speaker=identifier, actor=actor_id, text=self.dialogue.get("1.0", "end-1c").strip(),
                        action=next(k for k,v in ACTIONS.items() if v==self.action.get()),
                        emotion=next(k for k,v in EMOTIONS.items() if v==self.emotion.get()),
                        camera=next(k for k,v in CAMERAS.items() if v==self.camera.get()), duration=float(self.duration.get()))
            updated["title"] = self.title.get().strip() or "我的沙雕动画"
            self.board = validate_storyboard(updated)
            names = {character["id"]: character["name"] for character in self.board["characters"]}
            self.actor.set(names.get(actor_id, "自动 / 无"))
            self.speaker_box.configure(values=["旁白", *names.values()])
            self.actor_box.configure(values=["自动 / 无", *names.values()])
            if self.tree.exists(shot["id"]):
                self.tree.item(shot["id"], values=(names.get(identifier, "旁白"), shot["text"].replace("\n", " "), f"{shot['duration']:g}"))
            shots = self.board["shots"]
            self.summary.set(f"{len(shots)} 个镜头 · {len(names)} 个角色 · 预计 {sum(s['duration'] for s in shots):.1f} 秒；输入新说话人名可添加角色")
            return True
        except Exception as exc:
            return self._fail(ValueError("当前镜头未保存，请检查台词、场景和时长："+str(exc)))

    def _apply(self):
        if self._allowed() and self._commit_selected():
            self._render(self._selected)
            self._save()

    def _move(self, direction):
        if not self._allowed() or not self.board or not self._commit_selected():
            return
        rows = self.board["shots"]
        index = next(i for i,s in enumerate(rows) if s["id"]==self._selected)
        other = index+direction
        if 0<=other<len(rows):
            rows[index], rows[other] = rows[other], rows[index]
            self._render(self._selected)

    def _add(self):
        if not self._allowed() or not self.board or not self._commit_selected():
            return
        identifier = "shot_"+uuid.uuid4().hex[:12]
        row = {"id": identifier, "scene": self.scene.get() or "场景", "speaker": "", "text": "请填写台词或旁白。",
               "action": "idle", "emotion": "neutral", "camera": "medium", "duration": 3.}
        rows = self.board["shots"]
        index = next((i+1 for i,s in enumerate(rows) if s["id"]==self._selected), len(rows))
        rows.insert(index, row)
        self._render(identifier)

    def _delete(self):
        if not self._allowed() or not self.board or not self._selected:
            return
        if len(self.board["shots"])<=1:
            self.status.set("至少保留一个镜头。")
            return
        self.board["shots"] = [s for s in self.board["shots"] if s["id"]!=self._selected]
        self._render()

    def _train(self):
        if not self.board:
            self.status.set("先生成分镜，核对角色、台词、动作和表情后再学习。")
            return
        if not self.save_current():
            return
        if not messagebox.askyesno("学习已确认分镜", "已核对当前全部分镜？学习后，后续相似文字会参考这些动作、表情和景别。", parent=self.win):
            return
        board, root, events = copy.deepcopy(self.board), self.root, self._events
        def work():
            from story_pipeline import train_style
            return train_style(root, [board], report=lambda text: events.put(("progress", text, None)))
        self._run("train", work)

    def _choose_resources(self):
        if not self._allowed():
            return
        path = filedialog.askdirectory(parent=self.win,title="选择 AECPP 素材目录（含角色和背景）")
        if path:
            self.resource_root.set(path)
            self.resource_summary.set("目录已选择，点击“扫描素材”读取现有素材。")

    def _scan_resources(self):
        if not self._allowed():
            return False
        directory,events = self.resource_root.get().strip(),self._events
        if not directory:
            self.tabs.select(self.resources_page)
            self.status.set("请先选择 AECPP 现有素材目录。")
            return False
        def work():
            from aecpp_resources import scan_resources
            return scan_resources(directory,report=lambda text:events.put(("progress",text,None)))
        return self._run("scan-resources",work)

    def _resource_plan(self):
        if not self.board or not self.resource_catalog:
            return None
        from aecpp_resources import build_binding_plan
        return build_binding_plan(self.board,self.resource_catalog,self.resource_config)

    def _resource_detail_text(self,text):
        self.resource_detail.configure(state="normal")
        self.resource_detail.delete("1.0","end")
        self.resource_detail.insert("1.0",text)
        self.resource_detail.configure(state="disabled")

    def _load_resource_catalog(self, catalog):
        self.resource_catalog = catalog
        self.resource_root.set(catalog["root"])
        self.resource_config["resource_root"] = catalog["root"]
        self.inventory_tree.delete(*self.inventory_tree.get_children())
        for category,label in (("actors","角色与动作"),("backgrounds","背景"),("expressions","表情")):
            self.inventory_tree.insert("","end",iid=category,text=f"{label} · {len(catalog.get(category,[]))}",open=True)
            for index,item in enumerate(catalog.get(category,[])):
                key = category+"-"+str(index)
                self.inventory_tree.insert(category,"end",iid=key,text=(item.get("name") or item["id"])+("（资源缺失）" if item.get("valid") is False else ""))
                if category=="actors":
                    for action in item.get("actions",[]):
                        self.inventory_tree.insert(key,"end",text=action.get("name") or action["id"])

    def _render_resource_mappings(self):
        if self._closed:
            return
        selected = self.resource_tree.selection()
        previous = self._resource_rows.get(selected[0]) if selected else None
        self.resource_tree.delete(*self.resource_tree.get_children())
        self._resource_rows = {}
        plan = self._resource_plan()
        if plan:
            self.resource_config = plan["config"]
        config,catalog = self.resource_config,self.resource_catalog or {}
        actors = {row["id"]:row for row in catalog.get("actors",[])}
        backgrounds = {row["id"]:row for row in catalog.get("backgrounds",[])}
        expressions = {row["id"]:row for row in catalog.get("expressions",[])}
        def append(kind,key,field,title,selected_id,options):
            identifier = "mapping-"+str(len(self._resource_rows))
            row = {"kind":kind,"key":key,"field":field,"title":title,"selected":selected_id,"options":options}
            self._resource_rows[identifier] = row
            matched = next((asset for asset in options if asset["id"]==selected_id),None)
            text = matched.get("name",matched["id"]) if matched else ("缺失："+selected_id if selected_id else "待绑定")
            label = {"characters":"角色","backgrounds":"场景","actions":"动作","emotions":"表情"}[kind]
            self.resource_tree.insert("","end",iid=identifier,values=(label,title,text))
        if self.board:
            for character in self.board["characters"]:
                cid,name = character["id"],character["name"]
                actor_id = config.get("characters",{}).get(cid,"")
                append("characters",cid,"",name,actor_id,[row for row in actors.values() if row.get("valid",True)])
                actor = actors.get(actor_id,{})
                shots = [shot for shot in self.board["shots"] if (shot.get("actor") or shot.get("speaker"))==cid]
                for action in dict.fromkeys(["idle",*(shot["action"] for shot in shots)]):
                    append("actions",cid,action,name+" · "+ACTIONS.get(action,action),
                           config.get("actions",{}).get(cid,{}).get(action,""),actor.get("actions",[]))
                for emotion in dict.fromkeys(["neutral",*(shot["emotion"] for shot in shots)]):
                    append("emotions",cid,emotion,name+" · "+EMOTIONS.get(emotion,emotion),
                           config.get("emotions",{}).get(cid,{}).get(emotion,""),list(expressions.values()))
            for scene in dict.fromkeys(shot.get("scene","") for shot in self.board["shots"]):
                append("backgrounds",scene,"",scene or "未命名场景",config.get("backgrounds",{}).get(scene,""),list(backgrounds.values()))
        keys = list(self._resource_rows)
        target = next((key for key,row in self._resource_rows.items() if previous and all(row[field]==previous[field] for field in ("kind","key","field"))),keys[0] if keys else None)
        if target:
            self.resource_tree.selection_set(target)
            self.resource_tree.focus(target)
        self._select_resource_mapping()
        if self.resource_catalog:
            issues = list(catalog.get("issues",[]))
            if plan:
                issues.extend(plan.get("issues",[]))
            if self._automation_warnings:
                issues.extend(["上次一键生成的素材替代与编排说明：", *self._automation_warnings])
            counts = f"{len(actors)} 个角色 · {len(backgrounds)} 张背景 · {len(expressions)} 个表情素材"
            self.resource_summary.set(counts+(" · 素材已就绪，可组装。" if plan and plan.get("ready") else " · 请完成表中的素材映射。"))
            self._resource_detail_text("\n".join(issues) if issues else "已读取现有素材。角色动画、表情和背景将按左侧映射组装；请核对所选素材。")
        else:
            self._resource_detail_text("扫描后可选择真实 .actor 角色及其动作、现有表情和背景。缺失素材会列在这里，并阻止素材组装导出。")

    def _select_resource_mapping(self):
        selected = self.resource_tree.selection()
        row = self._resource_rows.get(selected[0]) if selected else None
        self._resource_options = {"（清除映射）":""}
        if row:
            for item in row["options"]:
                self._resource_options[f"{item.get('name') or item['id']} · {item['id']}"] = item["id"]
        self.resource_box.configure(values=tuple(self._resource_options))
        wanted = row["selected"] if row else ""
        self.resource_choice.set(next((label for label,value in self._resource_options.items() if value==wanted),"缺失："+wanted))

    def _apply_resource_mapping(self):
        if not self._allowed():
            return False
        selected = self.resource_tree.selection()
        row = self._resource_rows.get(selected[0]) if selected else None
        if not row or self.resource_choice.get() not in self._resource_options:
            self.status.set("请选择表中的用途以及一个现有素材。")
            return False
        value = self._resource_options[self.resource_choice.get()]
        mapping = self.resource_config.setdefault(row["kind"],{})
        if row["field"]:
            mapping = mapping.setdefault(row["key"],{})
            mapping[row["field"]] = value
        else:
            mapping[row["key"]] = value
            if row["kind"]=="characters":
                self.resource_config.setdefault("actions",{}).pop(row["key"],None)
        self._render_resource_mappings()
        self.status.set("素材映射已应用；保存设置后可在下次继续使用。")
        return True

    def _check_resources(self):
        if not self._allowed() or not self._commit_selected():
            return False
        self.tabs.select(self.resources_page)
        self._render_resource_mappings()
        plan = self._resource_plan()
        if not plan:
            self.status.set("请先生成分镜并扫描 AECPP 素材目录。")
            return False
        self.status.set("素材检查通过，可以组装动画。" if plan["ready"] else f"仍有 {len(plan.get('issues',[]))} 项素材问题，请查看右侧明细并调整映射。")
        return bool(plan["ready"])

    def _save_resources(self):
        if not self._allowed():
            return False
        try:
            from aecpp_resources import save_bindings
            config = copy.deepcopy(self.resource_config)
            config["resource_root"] = self.resource_root.get().strip()
            if self.board:
                config["character_names"] = {row["id"]: row["name"] for row in self.board["characters"]}
            self.resource_config = save_bindings(self.root,config)
            self.status.set("AECPP 素材目录与映射已保存。")
            return True
        except Exception as exc:
            return self._fail(exc)

    def _export_resources(self):
        if not self.board:
            self.status.set("先生成分镜，再绑定现有素材并组装动画。")
            return False
        if not self.save_current():
            return False
        if not self.resource_catalog or Path(self.resource_root.get()).resolve()!=Path(self.resource_catalog["root"]).resolve():
            self.tabs.select(self.resources_page)
            self.status.set("请在“素材与组装”中选择目录并扫描现有素材。")
            return False
        if not self._check_resources() or not self._save_resources():
            return False
        filename = filedialog.asksaveasfilename(parent=self.win,title="用现有素材组装 AECPP 工程（请选择新文件名）",
            defaultextension=".aecpp",initialfile="素材动画.aecpp",filetypes=[("AECPP 工程","*.aecpp")])
        if not filename:
            return False
        board,catalog,bindings,events = copy.deepcopy(self.board),copy.deepcopy(self.resource_catalog),copy.deepcopy(self.resource_config),self._events
        def work():
            from aecpp_resource_export import export_resource_project
            return export_resource_project(board,Path(filename),catalog=catalog,bindings=bindings,report=lambda text:events.put(("progress",text,None)))
        return self._run("export-resources",work)

    def _export_script(self):
        if not self.board:
            self.status.set("先生成分镜，再导出角色剧本。")
            return
        if not self.save_current():
            return
        filename = filedialog.asksaveasfilename(parent=self.win, title="导出 AECPP 角色剧本", defaultextension=".json",
                                               initialfile="故事角色剧本.json", filetypes=[("AECPP 结构化剧本", "*.json")])
        if not filename:
            return
        board, events = copy.deepcopy(self.board), self._events
        def work():
            from aecpp_export import export_script
            return export_script(board, Path(filename), report=lambda text: events.put(("progress", text, None)))
        self._run("export-script", work)

    def _export(self):
        if not self.board:
            self.status.set("先生成分镜，再导出 AECPP 工程。")
            return
        if not self.save_current():
            return
        filename = filedialog.asksaveasfilename(parent=self.win, title="导出图形示意草稿（不使用 AECPP 角色素材）", defaultextension=".aecpp",
                                               initialfile="分镜示意草稿.aecpp", filetypes=[("AECPP 示意草稿", "*.aecpp")])
        if not filename:
            return
        board, events = copy.deepcopy(self.board), self._events
        def work():
            from aecpp_export import export_project
            return export_project(board, Path(filename), report=lambda text: events.put(("progress", text, None)))
        self._run("export", work)

    def _poll(self):
        if self._closed:
            return
        self._timer = None
        try:
            while True:
                kind, result, error = self._events.get_nowait()
                if kind=="progress":
                    self.status.set(str(result))
                    continue
                if self._worker and self._worker.is_alive():
                    self._events.put((kind, result, error))
                    break
                self._worker = None
                self.busy = False
                cancelled = kind=="oneclick" and self._generation_stop and self._generation_stop.is_set()
                self._active_kind = None
                if kind=="oneclick":
                    self._generation_stop = None
                self._set_busy(False)
                if error:
                    if cancelled and "取消" in str(error):
                        self.status.set("本次生成已取消；正文、原分镜和素材映射保留，可修改后再次一键生成。")
                    else:
                        if kind=="oneclick" and any(word in str(error) for word in ("素材目录", "资源目录", "AECPP 安装", "素材与组装")):
                            self.tabs.select(self.resources_page)
                            if "素材与组装" not in str(error):
                                error = str(error)+"\n请在“素材与组装”选择 AECPP 素材目录并保存设置，再点击“一键生成动画”。"
                        self._fail(error)
                elif kind=="oneclick":
                    self.board = result["board"]
                    self._selected = None
                    self.resource_config = result["bindings"]
                    self._automation_warnings = [str(warning) for warning in result.get("warnings", [])]
                    self._load_resource_catalog(result["catalog"])
                    self._last_animation_output = Path(result["output"])
                    self.open_output_button.configure(state="normal")
                    self._render()
                    self.tabs.select(self.resources_page)
                    if self.save_current():
                        details = "；".join(self._automation_warnings[:3])
                        if len(self._automation_warnings)>3:
                            details += f"；其余 {len(self._automation_warnings)-3} 项见右侧说明。"
                        self.status.set(f"动画工程已生成：{self._last_animation_output}\n"+
                            ("素材替代说明："+details if details else "角色、动作、表情与场景已自动匹配；可继续修改分镜和素材映射。"))
                        sound=result.get('sound') or {}
                        if sound:
                            self.status.set(self.status.get()+f"\n声音：{sound.get('voiced_shots',0)} 句配音、{sound.get('mouth_clips',0)} 个角色口型轨、{sound.get('effect_events',0)} 处音效。")
                elif kind=="generate":
                    self.board = result
                    self._selected = None
                    self._render()
                    self.tabs.select(1)
                    if self.save_current():
                        from story_pipeline import reference_description
                        self.status.set("分镜已生成并保存。核对说话人和动作后，在“素材与组装”绑定现有素材。" + reference_description(self.board))
                elif kind=="train":
                    if self.board and self.board.get("provenance"):
                        self.board["provenance"]["review_status"] = "reviewed"
                        self.save_current()
                    validation = result.get("validation") or {}
                    checked = "尚无独立故事验证" if validation.get("kind")=="none" else "已按完整故事留出验证"
                    self.status.set(f"已学习 {result.get('story_count', 0)} 个故事、{result.get('example_count', 0)} 条分镜；{checked}。下一次生成自动使用。")
                elif kind=="export":
                    self.status.set(f"图形示意草稿已导出：{result}；含示意角色和字幕。使用现有角色素材成片请点击“用现有素材组装 AECPP”。")
                elif kind=="export-script":
                    self.status.set(f"角色剧本已导出：{result}；在 AECPP 剧本工作台导入，可继续选择角色素材、动作和配音。")
                elif kind=="scan-resources":
                    self._load_resource_catalog(result)
                    self._render_resource_mappings()
                    self.status.set("现有素材扫描完成，请核对角色、动作、表情及背景映射。")
                elif kind=="export-resources":
                    self.status.set(f"现有素材动画已组装：{result}；请保留同名 .assets 素材文件夹，在 AECPP 中继续编辑。")
        except queue.Empty:
            pass
        except Exception as exc:
            self.busy = False
            self._active_kind = None
            self._generation_stop = None
            self._set_busy(False)
            self._fail(exc)
        finally:
            if not self._closed:
                self._timer = self.win.after(100, self._poll)

    def show(self):
        if self._closed:
            return
        if not self.embedded:
            self.win.deiconify()
            self.win.lift()

    def _destroyed(self, event):
        if event.widget==self.win:
            self._finish_close()

    def _finish_close(self):
        if self._closed:
            return
        self._closed = True
        if self._generation_stop:
            self._generation_stop.set()
        if getattr(self, "_status_trace", None):
            self.status.trace_remove("write", self._status_trace)
            self._status_trace = None
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
        if self.busy:
            if self._active_kind=="oneclick":
                self._cancel_generation()
            else:
                self.status.set("请等待生成、学习或导出结束后再关闭。")
            return False
        if not self.save_current():
            return False
        self._finish_close()
        self.win.destroy()
        return True
