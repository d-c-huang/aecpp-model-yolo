"""Local video-analysis evidence viewer and model settings; callbacks own work."""
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk
import warnings

from PIL import Image, ImageTk

import theme
from video_analysis_notes import list_references


PROVIDERS = {"本地 Ollama": "ollama", "兼容模型接口": "openai-compatible"}
ACTION_LABELS = {"idle":"静止", "talk":"说话", "walk":"走路", "run":"跑步", "jump":"跳跃", "fall":"跌倒", "shake":"抖动", "unknown":"无法判断"}
EMOTION_LABELS = {"neutral":"平静", "happy":"开心", "angry":"生气", "sad":"难过", "shocked":"震惊", "unknown":"无法判断"}
CAMERA_LABELS = {"wide":"全景", "medium":"中景", "close":"近景", "unknown":"无法判断"}


def time_label(milliseconds):
    seconds = milliseconds//1000
    return f"{seconds//3600:02d}:{seconds//60%60:02d}:{seconds%60:02d}"


class VideoAnalysisWindow:
    def __init__(self, parent, root, *, on_close=None, can_write=lambda: True,
                 on_save=lambda settings,key: None, on_test_models=lambda settings,key: None,
                 on_download_asr=lambda size: None, on_open_board=lambda note_path,block_ids: None,
                 movie_id=None, on_download_vision=None):
        from video_analysis import DEFAULTS, load_settings
        self.root = Path(root).resolve()
        self.on_close, self.can_write = on_close, can_write
        self.on_save, self.on_test_models = on_save, on_test_models
        self.on_download_asr, self.on_open_board = on_download_asr, on_open_board
        self.on_download_vision = on_download_vision
        self._closed, self._photo = False, None
        self._visual_photo = None
        self._all_visual_notes, self._visual_notes, self._visual_note = [], {}, None
        self._visual_clips, self._visual_frames = {}, []
        self._all_notes, self._notes, self._note = [], {}, None
        self._blocks, self._map_nodes, self._frames = {}, {}, []
        self._movie_ids, self._wanted_movie = {}, movie_id
        self.win = tk.Toplevel(parent)
        self.win.title("视频理解 · 模型与学习结果")
        self.win.geometry("1080x790")
        self.win.minsize(980,720)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<Destroy>", self._destroyed, add="+")
        self.status = tk.StringVar(self.win, "打开此窗口只读取本地记录；模型操作在点击按钮后执行。")
        self.movie_filter = tk.StringVar(self.win, "全部影片")
        self.note_choice = tk.StringVar(self.win)
        self.frame_choice = tk.StringVar(self.win)
        self.visual_note_choice = tk.StringVar(self.win)
        self.visual_frame_choice = tk.StringVar(self.win)
        self.visual_summary = tk.StringVar(self.win, "尚无画面分析记录")
        self.summary = tk.StringVar(self.win, "尚无内容分析记录")
        self.api_key = tk.StringVar(self.win)
        warning = ""
        try:
            settings = load_settings(self.root)
        except (OSError,ValueError) as exc:
            settings = dict(DEFAULTS)
            warning = "无法读取设置，当前显示默认值："+str(exc)
        self.values = {}
        for key,value in settings.items():
            variable_type = tk.BooleanVar if type(value) is bool else tk.IntVar if type(value) is int else tk.StringVar
            variable = variable_type(self.win,value=value)
            self.values[key] = variable
        self.provider_choice = tk.StringVar(self.win,next((label for label,value in PROVIDERS.items() if value==settings["provider"]),"本地 Ollama"))
        self._build()
        self.refresh_notes()
        if warning:
            self.set_status(warning)

    def _build(self):
        body = ttk.Frame(self.win,padding=(14,10))
        body.pack(fill="both",expand=True)
        body.columnconfigure(0,weight=1)
        body.rowconfigure(1,weight=1)
        ttk.Label(body,text="视频理解",style="Title.TLabel").grid(row=0,column=0,sticky="w",pady=(0,8))
        self.tabs = ttk.Notebook(body)
        self.tabs.grid(row=1,column=0,sticky="nsew")
        self.results_tab, self.settings_tab = ttk.Frame(self.tabs,padding=10),ttk.Frame(self.tabs,padding=12)
        self.visual_tab = ttk.Frame(self.tabs,padding=10)
        self.tabs.add(self.results_tab,text="学习结果")
        self.tabs.add(self.visual_tab,text="画面动作")
        self.tabs.add(self.settings_tab,text="模型设置")
        self._build_results()
        self._build_visual_results()
        self._build_settings()
        ttk.Label(body,textvariable=self.status,style="Muted.TLabel",wraplength=940,justify="left").grid(row=2,column=0,sticky="ew",pady=(8,0))

    def _build_results(self):
        body = self.results_tab
        body.columnconfigure(0,weight=1)
        body.rowconfigure(2,weight=1)
        toolbar = ttk.Frame(body)
        toolbar.grid(row=0,column=0,sticky="ew")
        toolbar.columnconfigure(3,weight=1)
        ttk.Label(toolbar,text="影片").grid(row=0,column=0,padx=(0,5))
        self.movie_box = ttk.Combobox(toolbar,textvariable=self.movie_filter,state="readonly",width=19)
        self.movie_box.grid(row=0,column=1,sticky="ew",padx=(0,10))
        self.movie_box.bind("<<ComboboxSelected>>",lambda _:self._filter_notes())
        ttk.Label(toolbar,text="分析记录").grid(row=0,column=2,padx=(0,5))
        self.note_box = ttk.Combobox(toolbar,textvariable=self.note_choice,state="readonly")
        self.note_box.grid(row=0,column=3,sticky="ew",padx=(0,8))
        self.note_box.bind("<<ComboboxSelected>>",lambda _:self._select_note())
        self.refresh_button = ttk.Button(toolbar,text="刷新记录",command=self.refresh_notes,style="Compact.TButton")
        self.refresh_button.grid(row=0,column=4)
        ttk.Label(body,textvariable=self.summary,style="Caption.TLabel",wraplength=900).grid(row=1,column=0,sticky="ew",pady=(6,8))
        panels = ttk.Frame(body)
        panels.grid(row=2,column=0,sticky="nsew")
        panels.columnconfigure(0,weight=2,minsize=290)
        panels.columnconfigure(1,weight=3,minsize=430)
        panels.rowconfigure(0,weight=1)
        left = ttk.Notebook(panels)
        left.grid(row=0,column=0,sticky="nsew",padx=(0,10))
        segments, mapping = ttk.Frame(left),ttk.Frame(left)
        left.add(segments,text="内容段")
        left.add(mapping,text="思维导图")
        for frame in (segments,mapping):
            frame.columnconfigure(0,weight=1)
            frame.rowconfigure(0,weight=1)
        self.block_tree = ttk.Treeview(segments,columns=("time","title"),show="headings",selectmode="extended",height=8)
        self.block_tree.heading("time",text="原片时间")
        self.block_tree.heading("title",text="分析内容")
        self.block_tree.column("time",width=90,minwidth=75,stretch=False)
        self.block_tree.column("title",width=210,minwidth=120)
        self.block_tree.grid(row=0,column=0,sticky="nsew")
        scroll = ttk.Scrollbar(segments,command=self.block_tree.yview)
        scroll.grid(row=0,column=1,sticky="ns")
        self.block_tree.configure(yscrollcommand=scroll.set)
        self.block_tree.bind("<<TreeviewSelect>>",lambda _:self._select_block())
        self.map_tree = ttk.Treeview(mapping,show="tree",selectmode="browse",height=8)
        self.map_tree.column("#0",width=300,minwidth=180)
        self.map_tree.grid(row=0,column=0,sticky="nsew")
        scroll = ttk.Scrollbar(mapping,command=self.map_tree.yview)
        scroll.grid(row=0,column=1,sticky="ns")
        self.map_tree.configure(yscrollcommand=scroll.set)
        self.map_tree.bind("<<TreeviewSelect>>",lambda _:self._select_map_node())
        right = ttk.Frame(panels)
        right.grid(row=0,column=1,sticky="nsew")
        right.columnconfigure(0,weight=1)
        right.rowconfigure(2,weight=1)
        self.frame_box = ttk.Combobox(right,textvariable=self.frame_choice,state="disabled")
        self.frame_box.grid(row=0,column=0,sticky="ew",pady=(0,4))
        self.frame_box.bind("<<ComboboxSelected>>",lambda _:self._show_frame())
        preview = ttk.Frame(right,height=170)
        preview.grid(row=1,column=0,sticky="ew",pady=(0,7))
        preview.grid_propagate(False)
        preview.columnconfigure(0,weight=1)
        preview.rowconfigure(0,weight=1)
        self.preview = ttk.Label(preview,text="暂无本地关键帧",anchor="center",style="Muted.TLabel",wraplength=460)
        self.preview.grid(row=0,column=0,sticky="nsew")
        text_frame = ttk.Frame(right)
        text_frame.grid(row=2,column=0,sticky="nsew")
        text_frame.columnconfigure(0,weight=1)
        text_frame.rowconfigure(0,weight=1)
        self.detail = tk.Text(text_frame,wrap="word",state="disabled",height=8,width=30,
                              bg=theme.FIELD,fg=theme.TEXT,insertbackground=theme.TEXT,relief="flat",
                              font=(theme.FONT,10),padx=9,pady=7,selectbackground=theme.SELECTED)
        self.detail.grid(row=0,column=0,sticky="nsew")
        scroll = ttk.Scrollbar(text_frame,command=self.detail.yview)
        scroll.grid(row=0,column=1,sticky="ns")
        self.detail.configure(yscrollcommand=scroll.set)
        actions = ttk.Frame(body)
        actions.grid(row=3,column=0,sticky="ew",pady=(8,0))
        ttk.Label(actions,text="摘要尚待审核；点击导图可定位内容段。",style="Caption.TLabel").pack(side="left")
        self.open_all_button = ttk.Button(actions,text="生成全部参考分镜",command=lambda:self._open_board(False),style="Compact.TButton")
        self.open_all_button.pack(side="right")
        self.open_selected_button = ttk.Button(actions,text="生成所选参考分镜",command=lambda:self._open_board(True),style="Accent.TButton")
        self.open_selected_button.pack(side="right",padx=8)

    def _build_visual_results(self):
        body = self.visual_tab
        body.columnconfigure(0,weight=1)
        body.rowconfigure(2,weight=1)
        toolbar = ttk.Frame(body)
        toolbar.grid(row=0,column=0,sticky="ew")
        toolbar.columnconfigure(3,weight=1)
        ttk.Label(toolbar,text="影片").grid(row=0,column=0,padx=(0,5))
        self.visual_movie_box = ttk.Combobox(toolbar,textvariable=self.movie_filter,state="readonly",width=19)
        self.visual_movie_box.grid(row=0,column=1,padx=(0,10))
        self.visual_movie_box.bind("<<ComboboxSelected>>",lambda _:self._filter_notes())
        ttk.Label(toolbar,text="画面记录").grid(row=0,column=2,padx=(0,5))
        self.visual_note_box = ttk.Combobox(toolbar,textvariable=self.visual_note_choice,state="readonly")
        self.visual_note_box.grid(row=0,column=3,sticky="ew",padx=(0,8))
        self.visual_note_box.bind("<<ComboboxSelected>>",lambda _:self._select_visual_note())
        ttk.Button(toolbar,text="刷新记录",command=self.refresh_notes,style="Compact.TButton").grid(row=0,column=4)
        ttk.Label(body,textvariable=self.visual_summary,style="Caption.TLabel",wraplength=900,justify="left").grid(row=1,column=0,sticky="ew",pady=(8,8))
        panels = ttk.Frame(body)
        panels.grid(row=2,column=0,sticky="nsew")
        panels.columnconfigure(0,weight=1,minsize=420)
        panels.columnconfigure(1,weight=1,minsize=420)
        panels.rowconfigure(0,weight=1)
        left = ttk.Frame(panels)
        left.grid(row=0,column=0,sticky="nsew",padx=(0,10))
        left.columnconfigure(0,weight=1)
        left.rowconfigure(0,weight=1)
        self.visual_tree = ttk.Treeview(left,columns=("time","action","emotion","camera","confidence"),show="headings",selectmode="browse")
        for key,label,width in (("time","原片时间",90),("action","动作",70),("emotion","表情",70),("camera","景别",70),("confidence","置信度",75)):
            self.visual_tree.heading(key,text=label)
            self.visual_tree.column(key,width=width,minwidth=60,stretch=key=="time")
        self.visual_tree.grid(row=0,column=0,sticky="nsew")
        scroll = ttk.Scrollbar(left,command=self.visual_tree.yview)
        scroll.grid(row=0,column=1,sticky="ns")
        self.visual_tree.configure(yscrollcommand=scroll.set)
        self.visual_tree.bind("<<TreeviewSelect>>",lambda _:self._select_visual_clip())
        right = ttk.Frame(panels)
        right.grid(row=0,column=1,sticky="nsew")
        right.columnconfigure(0,weight=1)
        right.rowconfigure(2,weight=1)
        self.visual_frame_box = ttk.Combobox(right,textvariable=self.visual_frame_choice,state="disabled")
        self.visual_frame_box.grid(row=0,column=0,sticky="ew",pady=(0,6))
        self.visual_frame_box.bind("<<ComboboxSelected>>",lambda _:self._show_visual_frame())
        preview = ttk.Frame(right,height=205)
        preview.grid(row=1,column=0,sticky="ew",pady=(0,8))
        preview.grid_propagate(False)
        preview.columnconfigure(0,weight=1)
        preview.rowconfigure(0,weight=1)
        self.visual_preview = ttk.Label(preview,text="暂无本地连续帧",anchor="center",style="Muted.TLabel",wraplength=410)
        self.visual_preview.grid(row=0,column=0,sticky="nsew")
        detail = ttk.Frame(right)
        detail.grid(row=2,column=0,sticky="nsew")
        detail.columnconfigure(0,weight=1)
        detail.rowconfigure(0,weight=1)
        self.visual_detail = tk.Text(detail,wrap="word",state="disabled",height=8,width=30,
            bg=theme.FIELD,fg=theme.TEXT,insertbackground=theme.TEXT,relief="flat",
            font=(theme.FONT,10),padx=9,pady=7,selectbackground=theme.SELECTED)
        self.visual_detail.grid(row=0,column=0,sticky="nsew")
        scroll = ttk.Scrollbar(detail,command=self.visual_detail.yview)
        scroll.grid(row=0,column=1,sticky="ns")
        self.visual_detail.configure(yscrollcommand=scroll.set)
        ttk.Label(body,text="自动视觉参考用于新故事的动作、表情与景别建议；你确认的分镜优先。置信度为模型自评，仍需核对。",
                  style="Caption.TLabel",wraplength=900).grid(row=3,column=0,sticky="ew",pady=(8,0))

    def _build_settings(self):
        body = self.settings_tab
        body.columnconfigure(0,weight=1)
        switches = ttk.Frame(body)
        switches.grid(row=0,column=0,sticky="ew",pady=(0,10))
        for index,(key,label) in enumerate((("enabled","启用视频理解"),("asr_enabled","缺少字幕时本地转写"),
                                             ("semantic_enabled","分析剧情与镜头建议"),("keyframes","提取真实关键帧"))):
            ttk.Checkbutton(switches,text=label,variable=self.values[key]).grid(row=index//2,column=index%2,sticky="w",padx=(0,35),pady=3)
        columns = ttk.Frame(body)
        columns.grid(row=1,column=0,sticky="ew",pady=(0,8))
        columns.columnconfigure(0,weight=1,minsize=330)
        columns.columnconfigure(1,weight=2,minsize=520)
        asr = ttk.LabelFrame(columns,text="本地语音转写",padding=10)
        asr.grid(row=0,column=0,sticky="nsew",padx=(0,10))
        asr.columnconfigure(1,weight=1)
        ttk.Label(asr,text="语音模型").grid(row=0,column=0,sticky="w",padx=(0,10))
        self.asr_model_box = ttk.Combobox(asr,textvariable=self.values["asr_model"],values=("tiny","base","small"),state="readonly",width=14)
        self.asr_model_box.grid(row=0,column=1,sticky="w")
        self.download_button = ttk.Button(asr,text="下载语音模型",command=self._download_asr,style="Compact.TButton")
        self.download_button.grid(row=0,column=2,padx=(10,0))
        ttk.Label(asr,text="本地模型目录").grid(row=1,column=0,sticky="w",padx=(0,10),pady=6)
        ttk.Entry(asr,textvariable=self.values["asr_model_path"]).grid(row=1,column=1,sticky="ew",pady=6)
        ttk.Button(asr,text="选择目录…",command=self._choose_model_path,style="Compact.TButton").grid(row=1,column=2,padx=(10,0))
        ttk.Label(asr,text="转写语言").grid(row=2,column=0,sticky="w",padx=(0,10))
        ttk.Entry(asr,textvariable=self.values["language"],width=15).grid(row=2,column=1,sticky="w")
        ttk.Label(asr,text="zh = 中文；留空自动判断。\n模型目录留空使用下载的模型。",style="Caption.TLabel",wraplength=300).grid(row=3,column=0,columnspan=3,sticky="w",pady=(5,0))
        llm = ttk.LabelFrame(columns,text="模型服务（文字与画面共用）",padding=10)
        llm.grid(row=0,column=1,sticky="nsew")
        llm.columnconfigure(1,weight=1)
        labels = (("服务类型",self.provider_choice),("服务地址",self.values["base_url"]),
                  ("文字模型",self.values["model"]),("API Key",self.api_key),("Key 环境变量",self.values["api_key_env"]))
        for index,(label,variable) in enumerate(labels):
            ttk.Label(llm,text=label).grid(row=index,column=0,sticky="w",padx=(0,10),pady=3)
            if index==0:
                widget = ttk.Combobox(llm,textvariable=variable,values=tuple(PROVIDERS),state="readonly",width=22)
            elif index==2:
                widget = self.model_box = ttk.Combobox(llm,textvariable=variable)
            else:
                widget = ttk.Entry(llm,textvariable=variable,show="●" if index==3 else "")
            widget.grid(row=index,column=1,sticky="ew",pady=3)
        self.test_button = ttk.Button(llm,text="检测可用模型",command=self._test_models,style="Compact.TButton")
        self.test_button.grid(row=2,column=2,padx=(10,0))
        ttk.Label(llm,text="API Key 仅在本次运行使用，不写入工程。远程分析会发送字幕；启用画面理解还会发送抽样帧。",style="Caption.TLabel",wraplength=450).grid(row=5,column=0,columnspan=3,sticky="w",pady=(6,0))
        visual = ttk.LabelFrame(body,text="画面理解与动画编排",padding=10)
        visual.grid(row=2,column=0,sticky="ew")
        visual.columnconfigure(1,weight=1)
        ttk.Checkbutton(visual,text="启用画面理解",variable=self.values["visual_enabled"]).grid(row=0,column=0,sticky="w",padx=(0,10))
        self.visual_model_box = ttk.Combobox(visual,textvariable=self.values["visual_model"])
        self.visual_model_box.grid(row=0,column=1,sticky="ew",padx=(0,10))
        ttk.Label(visual,text="最多片段").grid(row=0,column=2,padx=(0,5))
        self.visual_limit_box = ttk.Spinbox(visual,from_=1,to=60,increment=1,textvariable=self.values["visual_max_clips"],width=5)
        self.visual_limit_box.grid(row=0,column=3)
        speed=ttk.Frame(visual);speed.grid(row=2,column=0,columnspan=5,sticky='ew',pady=(6,0))
        ttk.Checkbutton(speed,text='复用校验缓存、支持断点恢复',variable=self.values['analysis_cache']).pack(side='left')
        ttk.Label(speed,text='Ollama 保留秒数').pack(side='left',padx=(12,5))
        ttk.Spinbox(speed,from_=0,to=3600,increment=60,textvariable=self.values['ollama_keep_alive'],width=6).pack(side='left')
        ttk.Label(speed,text='0 表示每次释放；默认 300 秒',style='Caption.TLabel').pack(side='left',padx=8)
        self.vision_download_button = ttk.Button(visual,text="一键准备视觉模型",command=self._download_vision,style="Compact.TButton")
        self.vision_download_button.grid(row=0,column=4,padx=(10,0))
        if self.on_download_vision is None:
            self.vision_download_button.configure(state="disabled")
        ttk.Label(visual,text="请选择支持图片的视觉模型。每段最多 3 张按时间排序的真实帧，分析动作、表情和景别；切镜候选用于节奏参考。\n"
            "影片按预算抽样，未覆盖的画面不会自动补写。高置信度且匹配新故事的观察才用于编排，结果可在“画面动作”中核对。",
            style="Caption.TLabel",wraplength=870,justify="left").grid(row=1,column=0,columnspan=5,sticky="w",pady=(6,0))
        buttons = ttk.Frame(body)
        buttons.grid(row=3,column=0,sticky="ew",pady=(10,0))
        self.save_button = ttk.Button(buttons,text="保存视频理解设置",command=self._save,style="Accent.TButton")
        self.save_button.pack(side="right")
        ttk.Label(buttons,text="保存后，重新分析影片会使用新设置。",style="Muted.TLabel").pack(side="left")

    def collect_settings(self):
        from video_analysis import clean_settings
        settings = {key:variable.get() for key,variable in self.values.items()}
        settings["provider"] = PROVIDERS[self.provider_choice.get()]
        return clean_settings(settings)

    def _perform(self, callback, message="", *, write=True):
        if self._closed:
            return False
        if write and not self.can_write():
            self.set_status("当前工程仍在执行任务，请完成后再操作。")
            return False
        try:
            result = callback()
            if result is not False and message:
                self.set_status(message)
            return result
        except Exception as exc:
            self.set_status(str(exc))
            return False

    def _save(self):
        return self._perform(lambda:self.on_save(self.collect_settings(),self.api_key.get()),"视频理解设置已保存。")

    def _test_models(self):
        return self._perform(lambda:self.on_test_models(self.collect_settings(),self.api_key.get()))

    def _download_asr(self):
        return self._perform(lambda:self.on_download_asr(self.collect_settings()["asr_model"]))

    def _download_vision(self):
        if self.on_download_vision is not None:
            return self._perform(lambda:self.on_download_vision(self.collect_settings(),self.api_key.get()))

    def _choose_model_path(self):
        path = filedialog.askdirectory(parent=self.win,title="选择本地 faster-whisper 模型目录")
        if path:
            self.values["asr_model_path"].set(path)

    def set_models(self, names):
        if self._closed:
            return
        names = list(dict.fromkeys(str(name) for name in names))
        self.model_box.configure(values=names)
        self.visual_model_box.configure(values=names)
        if names and not self.values["model"].get():
            self.values["model"].set(names[0])
        self.set_status(f"检测到 {len(names)} 个模型；文字和画面可分别选择。画面理解需要支持图片的模型。")

    def set_status(self, message):
        if not self._closed:
            self.status.set(str(message))

    def refresh_notes(self):
        if self._closed:
            return
        old_path = self._note.get("path") if self._note else None
        try:
            self._all_notes = list_references(self.root)
            folder = (self.root/"video_analysis/references").resolve()
            if not folder.is_relative_to(self.root):
                raise ValueError("分析记录目录不在当前工程中")
            file_count = len(list(folder.glob("*.json"))) if folder.exists() else 0
            self._invalid_count = max(0,file_count-len(self._all_notes))
        except (OSError,ValueError) as exc:
            self._all_notes, self._invalid_count = [],0
            self.set_status("读取学习结果失败："+str(exc))
        try:
            from video_visual_learning import list_visual_references
            self._all_visual_notes = list_visual_references(self.root)
            folder = (self.root/"video_analysis/visual_references").resolve()
            if not folder.is_relative_to(self.root):
                raise ValueError("画面记录目录不在当前工程中")
            self._visual_invalid_count = max(0,len(list(folder.glob("*.json")))-len(self._all_visual_notes)) if folder.exists() else 0
        except (OSError,ValueError) as exc:
            self._all_visual_notes, self._visual_invalid_count = [],0
            self.set_status("读取画面结果失败："+str(exc))
        movies = {row["movie_id"]:row.get("movie_title",f"影片 {row['movie_id']}") for row in [*self._all_notes,*self._all_visual_notes]}
        self._movie_ids = {"全部影片":None,**{f"{name} · {identifier}":identifier for identifier,name in movies.items()}}
        self.movie_box.configure(values=tuple(self._movie_ids))
        self.visual_movie_box.configure(values=tuple(self._movie_ids))
        if self._wanted_movie is not None:
            self.movie_filter.set(next((label for label,identifier in self._movie_ids.items() if identifier==self._wanted_movie),"全部影片"))
            self._wanted_movie = None
        elif self.movie_filter.get() not in self._movie_ids:
            self.movie_filter.set("全部影片")
        self._filter_notes(old_path)

    def _filter_notes(self, old_path=None):
        movie = self._movie_ids.get(self.movie_filter.get())
        records = [row for row in self._all_notes if movie is None or row["movie_id"]==movie]
        self._notes = {}
        for index,note in enumerate(records,1):
            try:
                stamp = datetime.fromisoformat(note["created_at"]).astimezone().strftime("%m-%d %H:%M:%S")
            except ValueError:
                stamp = note["created_at"][:19]
            self._notes[f"{index}. {note['movie_title']} · {stamp}"] = note
        self.note_box.configure(values=tuple(self._notes))
        self.note_choice.set(next((label for label,note in self._notes.items() if note["path"]==old_path),next(iter(self._notes),"")))
        self._select_note()
        self._filter_visual_notes()

    def _filter_visual_notes(self):
        movie = self._movie_ids.get(self.movie_filter.get())
        old_path = self._visual_note.get("path") if self._visual_note else None
        records = [row for row in self._all_visual_notes if movie is None or row["movie_id"]==movie]
        self._visual_notes = {}
        for index,note in enumerate(records,1):
            stamp = str(note.get("created_at",""))[:19].replace("T"," ")
            label = f"{index}. {note.get('movie_title', '影片 '+str(note['movie_id']))} · {stamp}"
            self._visual_notes[label] = note
        self.visual_note_box.configure(values=tuple(self._visual_notes))
        self.visual_note_choice.set(next((label for label,note in self._visual_notes.items() if note["path"]==old_path),next(iter(self._visual_notes),"")))
        self._select_visual_note()

    def _select_visual_note(self):
        self._visual_note = self._visual_notes.get(self.visual_note_choice.get())
        self.visual_tree.delete(*self.visual_tree.get_children())
        self._visual_clips = {}
        if self._visual_note:
            note = self._visual_note
            self._visual_clips = {clip["id"]:clip for clip in note["clips"]}
            for clip in note["clips"]:
                self.visual_tree.insert("","end",iid=clip["id"],values=(time_label(clip["start_ms"]),
                    ACTION_LABELS.get(clip["action"],"无法判断"),EMOTION_LABELS.get(clip["emotion"],"无法判断"),
                    CAMERA_LABELS.get(clip["camera"],"无法判断"),f"{clip['confidence']:.0%}"))
            frame_count = sum(len(clip["frames"]) for clip in note["clips"])
            self.visual_summary.set(f"{note.get('movie_title','影片 '+str(note['movie_id']))} · {len(self._visual_clips)} 个抽样片段 · {frame_count} 张实际帧 · 自动观察，待审核；非逐帧覆盖")
            coverage = note.get("coverage",{})
            sampled, total = coverage.get("sampled_duration_ms"),coverage.get("total_duration_ms")
            if type(sampled) is int and type(total) is int and total>0:
                self.visual_summary.set(self.visual_summary.get()+f" · 抽样窗口 {time_label(sampled)} / 原片 {time_label(total)}")
            if self._visual_clips:
                first = next(iter(self._visual_clips))
                self.visual_tree.selection_set(first)
                self.visual_tree.focus(first)
        else:
            self.visual_summary.set("尚无画面分析记录。在模型设置中启用画面理解并选择视觉模型，然后重新分析影片。")
        if getattr(self,"_visual_invalid_count",0):
            self.visual_summary.set(self.visual_summary.get()+f" · {self._visual_invalid_count} 个记录或画面证据损坏，未载入")
        self._select_visual_clip()

    def _select_visual_clip(self):
        selected = self.visual_tree.selection()
        clip = self._visual_clips.get(selected[0]) if selected else None
        self._visual_frames = []
        if clip:
            note = self._visual_note
            self._visual_frames = clip["frames"]
            lines = [f"原片范围：{time_label(clip['start_ms'])} – {time_label(clip['end_ms'])}",
                f"动作：{ACTION_LABELS.get(clip['action'],'无法判断')}　表情：{EMOTION_LABELS.get(clip['emotion'],'无法判断')}　景别：{CAMERA_LABELS.get(clip['camera'],'无法判断')}",
                f"模型自评置信度：{clip['confidence']:.0%}（待审核）", "", "画面描述：",clip["text"],"", "判断依据：",clip["evidence"]]
            if clip.get("context_text"):
                lines.extend(["", "该片段字幕：",clip["context_text"]])
            cuts = clip.get("cut_times_ms",[])
            lines.extend(["", "切镜候选："+("、".join(time_label(value) for value in cuts) if cuts else "本片段未检测到候选"),
                "切镜候选来自亮度变化统计，可能包含光照变化；不等同于已确认的剪辑点。",
                "", f"模型：{note.get('model','')} · {note.get('provider','')}","参考记录："+note["path"]])
            text = "\n".join(lines)
        else:
            text = "这里展示连续帧、人物动作、表情、景别和切镜候选；自动观察不会标记为人工确认。"
        self.visual_detail.configure(state="normal")
        self.visual_detail.delete("1.0","end")
        self.visual_detail.insert("1.0",text)
        self.visual_detail.configure(state="disabled")
        labels = [f"{index+1}. {time_label(frame['time_ms'])} · 连续帧" for index,frame in enumerate(self._visual_frames)]
        self.visual_frame_box.configure(values=labels,state="readonly" if labels else "disabled")
        self.visual_frame_choice.set(labels[0] if labels else "没有连续帧")
        self._show_visual_frame()

    def _show_visual_frame(self):
        self._visual_photo = None
        self.visual_preview.configure(image="",text="暂无本地连续帧")
        index = self.visual_frame_box.current()
        if index<0 or index>=len(self._visual_frames):
            return
        try:
            path = (self.root/self._visual_frames[index]["path"]).resolve()
            if not path.is_relative_to(self.root) or not path.is_file() or path.stat().st_size>12*1024*1024:
                raise ValueError("画面帧缺失、路径越界或文件过大")
            with warnings.catch_warnings():
                warnings.simplefilter("error",Image.DecompressionBombWarning)
                with Image.open(path) as opened:
                    if opened.width*opened.height>12000000:
                        raise ValueError("画面帧尺寸过大")
                    opened.thumbnail((420,200))
                    picture = opened.convert("RGB")
            self._visual_photo = ImageTk.PhotoImage(picture,master=self.win)
            self.visual_preview.configure(image=self._visual_photo,text="")
        except (OSError,ValueError,Image.DecompressionBombError,Image.DecompressionBombWarning) as exc:
            self.visual_preview.configure(text="无法预览画面帧："+str(exc))

    def _select_note(self):
        self._note = self._notes.get(self.note_choice.get())
        self.block_tree.delete(*self.block_tree.get_children())
        self.map_tree.delete(*self.map_tree.get_children())
        self._blocks, self._map_nodes = {},{}
        if self._note:
            result = self._note["result"]
            self._blocks = {block["blockId"]:block for block in result["contentBlocks"]}
            for block in result["contentBlocks"]:
                self.block_tree.insert("","end",iid=block["blockId"],values=(time_label(block["startMs"]),block["title"] or "未命名内容段"))
            self._build_map(result["mindmap"])
            self.summary.set(f"{self._note['movie_title']} · {len(self._blocks)} 个内容段 · 机器分析摘要，尚未审核")
            if self._blocks:
                first = next(iter(self._blocks))
                self.block_tree.selection_set(first)
                self.block_tree.focus(first)
        else:
            self.summary.set("尚无内容分析记录。启用内容分析模型后，重新分析影片可生成记录。")
        if getattr(self,"_invalid_count",0):
            self.summary.set(self.summary.get()+f" · {self._invalid_count} 个记录损坏或超限，未载入")
        self._select_block()

    def _build_map(self,mindmap):
        self._map_nodes = {node["id"]:node for node in mindmap["nodes"]}
        children = {key:[] for key in self._map_nodes}
        incoming = set()
        for edge in mindmap["edges"]:
            children[edge["source"]].append(edge["target"])
            incoming.add(edge["target"])
        roots = [key for key in self._map_nodes if key not in incoming]
        inserted = set()
        # Iterative traversal handles graph cycles/shared children without hanging Tk.
        for start in [*roots,*self._map_nodes]:
            pending = [("",start)]
            while pending:
                parent,identifier = pending.pop()
                if identifier in inserted:
                    continue
                node = self._map_nodes[identifier]
                self.map_tree.insert(parent,"end",iid=identifier,text=node["label"] or identifier,open=True)
                inserted.add(identifier)
                pending.extend((identifier,child) for child in reversed(children[identifier]))

    def _select_map_node(self):
        selected = self.map_tree.selection()
        node = self._map_nodes.get(selected[0]) if selected else None
        if not node:
            return
        data = node.get("data",{})
        block_id = data.get("targetBlockId")
        if not block_id and data.get("targetHighlightId"):
            block_id = next((block["blockId"] for block in self._blocks.values() if any(h["highlightId"]==data["targetHighlightId"] for h in block["highlights"])),None)
        if block_id in self._blocks:
            self.block_tree.selection_set(block_id)
            self.block_tree.focus(block_id)
            self.block_tree.see(block_id)
            self._select_block()

    def _set_detail(self,text):
        self.detail.configure(state="normal")
        self.detail.delete("1.0","end")
        self.detail.insert("1.0",text)
        self.detail.configure(state="disabled")

    def _select_block(self):
        selected = [key for key in self.block_tree.selection() if key in self._blocks]
        self.open_selected_button.configure(state="normal" if selected and self.can_write() else "disabled")
        self.open_all_button.configure(state="normal" if self._note and self.can_write() else "disabled")
        self._frames = []
        if not selected:
            self._set_detail("内容分析会在这里显示摘要、原文证据和来源时间。摘要不会自动成为已确认训练标签。")
        else:
            block = self._blocks[selected[0]]
            lines = [block["title"],f"原片范围：{time_label(block['startMs'])} – {time_label(block['endMs'])}",
                     "机器分析摘要，尚未审核；以下摘要不是原始台词。",""]
            if self._note["result"].get("analysis",{}).get("transcript_only"):
                lines.insert(3,"内容理解基于字幕或转写；关键帧来自原片，镜头建议仍需人工核对。")
            for key in ("summary","analysis"):
                if block.get(key):
                    lines.extend([block[key],""])
            for highlight in block["highlights"]:
                lines.extend([f"{time_label(highlight['startMs'])} – {time_label(highlight['endMs'])}",highlight["text"],""])
                self._frames.extend(highlight["keyframes"])
            if block.get("evidenceSegments"):
                lines.append("引用的转写证据：")
                for segment in block["evidenceSegments"]:
                    lines.append(f"[{time_label(segment['startMs'])} – {time_label(segment['endMs'])}] {segment['text']}")
            lines.extend(["", "分析来源："+str(self._note.get("source","")),"参考记录："+self._note["path"]])
            if len(selected)>1:
                lines.insert(0,f"已选择 {len(selected)} 个内容段；当前预览第一个。")
            self._set_detail("\n".join(lines))
        labels = [f"{index+1}. {time_label(frame.get('timeMs',0))} · {frame.get('caption') or '关键帧'}" for index,frame in enumerate(self._frames)]
        self.frame_box.configure(values=labels,state="readonly" if labels else "disabled")
        self.frame_choice.set(labels[0] if labels else "没有关键帧")
        self._show_frame()

    def _show_frame(self):
        self._photo = None
        self.preview.configure(image="",text="暂无本地关键帧")
        index = self.frame_box.current()
        if index<0 or index>=len(self._frames):
            return
        frame = self._frames[index]
        if not frame.get("path"):
            self.preview.configure(text="此记录仅包含关键帧时间或外部资源地址，尚无本地图片。")
            return
        try:
            path = (self.root/frame["path"]).resolve()
            if not path.is_relative_to(self.root) or not path.is_file() or path.stat().st_size>12*1024*1024:
                raise ValueError("关键帧缺失、路径越界或文件过大")
            with warnings.catch_warnings():
                warnings.simplefilter("error",Image.DecompressionBombWarning)
                with Image.open(path) as opened:
                    if opened.width*opened.height>12000000:
                        raise ValueError("关键帧尺寸过大")
                    opened.thumbnail((490,165))
                    picture = opened.convert("RGB")
            self._photo = ImageTk.PhotoImage(picture,master=self.win)
            self.preview.configure(image=self._photo,text="")
        except (OSError,ValueError,Image.DecompressionBombError,Image.DecompressionBombWarning) as exc:
            self.preview.configure(text="无法预览关键帧："+str(exc))

    def _open_board(self, selected_only):
        if not self._note:
            return False
        ids = list(self.block_tree.selection()) if selected_only else None
        if selected_only and not ids:
            return False
        return self._perform(lambda:self.on_open_board(self._note["path"],ids))

    def show(self):
        if not self._closed:
            self.win.deiconify()
            self.win.lift()

    def select_movie(self,movie_id):
        if not self._closed:
            self._wanted_movie = movie_id
            if movie_id is None:
                self.movie_filter.set("全部影片")
            self.refresh_notes()

    def _destroyed(self,event):
        if event.widget==self.win:
            self._finish_close()

    def _finish_close(self):
        if self._closed:
            return
        self._closed = True
        self._photo = None
        self._visual_photo = None
        # No traces, timers or worker threads are created by this window.
        self.api_key.set("")
        if self.on_close:
            self.on_close()

    def close(self):
        if not self._closed:
            self._finish_close()
            self.win.destroy()
        return True
