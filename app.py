"""Movie annotation and training desktop client. Run with Python 3.11+."""
from contextlib import nullcontext
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import dataset
import theme
from editor import BoxCanvas
from dialogue import edit_cast, edit_note, manage_voices
from media import MOVIE_TYPES
from thumbnails import ThumbnailTree
from training_window import TrainingWindow, read_json, save_job

BASE = Path(__file__).resolve().parent
STATES = {"new":"未标注","draft":"人工草稿","predicted":"待复核","reviewed":"已确认"}
MODES = {"按片内时段":"time","整部训练":"train","整部验证":"val"}
DEVICES = {"自动":"","CPU":"cpu","GPU 0":"0"}
DEFAULT_EPOCHS = 5
DEFAULT_MOVIE_EPOCHS = 30


def options(parent, title, fields, *, prompt=None, action="开始"):
    heading,_,context = title.partition(" · ")
    dlg,body = theme.sheet(parent,heading,context=context)
    if prompt:
        ttk.Label(body,text=prompt,style="Muted.TLabel",wraplength=470).pack(anchor="w",pady=(0,8))
    area = ttk.Frame(body)
    area.pack(fill="both",expand=True)
    area.columnconfigure(1,weight=1)
    variables, result = {}, {}
    for i,(key,label,value,choices) in enumerate(fields):
        ttk.Label(area,text=label).grid(row=i,column=0,sticky="w",padx=(0,12),pady=6)
        var = tk.StringVar(value=value)
        variables[key] = var
        if choices == "file":
            widget = ttk.Entry(area,textvariable=var,width=42)
            def browse(v=var):
                filename = filedialog.askopenfilename(parent=dlg,filetypes=[("YOLO 检测权重","*.pt")])
                if filename:
                    v.set(filename)
            ttk.Button(area,text="浏览",command=browse).grid(row=i,column=2,padx=3)
        elif choices:
            widget = ttk.Combobox(area,textvariable=var,values=choices,state="readonly",width=21)
        else:
            widget = ttk.Entry(area,textvariable=var,width=23)
        widget.grid(row=i,column=1,sticky="ew")
    def accept():
        result.update({k:v.get().strip() for k,v in variables.items()})
        dlg.destroy()
    ttk.Button(area,text=action,command=accept,style="Primary.TButton").grid(row=len(fields),column=1,sticky="e",pady=(14,0))
    ttk.Button(area,text="取消",command=dlg.destroy,style="Compact.TButton").grid(row=len(fields),column=0,sticky="w",pady=(14,0))
    dlg.bind("<Return>",lambda e: accept())
    dlg.bind("<Escape>",lambda e: dlg.destroy())
    dlg.tk.call("::tk::PlaceWindow",dlg,"widget",parent)
    dlg.grab_set()
    dlg.focus_set()
    parent.wait_window(dlg)
    return result


class Studio:
    def __init__(self, win):
        self.win = win
        theme.apply_theme(win)
        win.title("映格 · 电影学习工作台")
        win.geometry("1280x900")
        win.minsize(980,760)
        self.root, self.row = None,None
        self.rows, self.names, self.history = [],[],[]
        self.voices = []
        self.movies = {}
        self.batch_settings = {}
        self.batch_rows = {}
        self.batch_labels = {}
        self.busy = False
        self.project_transfer = False
        self.after_success = None
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.training_stop = threading.Event()
        self.training = None
        self.training_window = None
        self.training_settings = None
        self.training_kind = "movie-temporal"
        self.movie_training_settings = None
        self.feed_pending = False
        self.feed_snapshot = None
        self.pending_movie_learning = None
        self.training_cancel_version = 0
        self.online_manager = None
        self.online_window = None
        self.retired_online_managers = []
        self.auto_learning_manager = None
        self.auto_learning_window = None
        self.video_analysis_window = None
        self.video_analysis_api_key = ""
        self.retired_auto_learning_managers = []
        self.discovery_manager = None
        self.discovery_window = None
        self.discovery_usage = None
        self.retired_discovery_managers = []
        self.discovery_activity = {}
        self.continuous_learning_manager = None
        self.continuous_models_ready = False
        self.continuous_disk_paused = False
        self.retired_continuous_managers = []
        self.continuous_training_run = None
        self.online_learning_batches = {}
        self.online_refresh_pending = False
        self.online_poll_at = 0.
        self.cache_window = None
        self.library_page = None
        self.story_window = None
        self.active_view = None
        self.profile_window = None
        self.profile_waiting = {}
        self.profile_poll_at = 0.
        self.feed_movie_settings = None
        self.training_movie_options = {}
        self.closing = False
        self.status = tk.StringVar(value="选择一个文件夹新建或打开标注工程")
        self.training_status = tk.StringVar(value="训练尚未启动")
        self.feed_status = tk.StringVar()
        self.title = tk.StringVar(value="未打开工程")
        self.filter = tk.StringVar(value="全部状态")
        self.category = tk.StringVar()
        self.selection_text = tk.StringVar(value="拖动鼠标绘制矩形框")
        self.detail = tk.StringVar(value="")
        self.frame_count = tk.StringVar(value="0 张")
        self.frame_metadata = tk.StringVar()
        self.build_workspace()
        self.context_menu = tk.Menu(win,tearoff=False)
        win.bind("<Delete>",self.delete_key)
        win.bind("<Control-z>",self.undo_key)
        win.bind("<Return>",lambda e:self.confirm() if win.focus_get() in (self.canvas,self.tree) else None)
        win.protocol("WM_DELETE_WINDOW",self.close)
        win.after(100,self.poll)

    def build_workspace(self):
        header = ttk.Frame(self.win,padding=(20,12))
        header.pack(fill="x")
        mark = tk.Canvas(header,width=40,height=40,bg=theme.PANEL,highlightthickness=0)
        mark.pack(side="left")
        mark.create_polygon(8,1,32,1,39,8,39,32,32,39,8,39,1,32,1,8,fill=theme.ACCENT)
        for x,y,dx,dy in ((12,12,1,1),(28,12,-1,1),(12,28,1,-1),(28,28,-1,-1)):
            mark.create_line(x,y+dy*5,x,y,x+dx*5,y,fill=theme.INK,width=2)
        mark.create_oval(18,18,22,22,fill=theme.INK,outline="")
        ttk.Label(header,text="映格",style="Brand.TLabel").pack(side="left",padx=(10,18))
        ttk.Separator(header,orient="vertical").pack(side="left",fill="y",pady=3)
        project = ttk.Frame(header)
        project.pack(side="left",padx=16)
        ttk.Label(project,text="当前工程",style="Caption.TLabel").pack(anchor="w")
        ttk.Label(project,textvariable=self.title,style="Section.TLabel",wraplength=200).pack(anchor="w")
        ttk.Button(header,text="添加在线视频",command=self.add_online,style="Primary.TButton").pack(side="right")
        ttk.Button(header,text="导入本地",command=self.import_movie).pack(side="right",padx=8)
        self.project_button = ttk.Menubutton(header,text="工程")
        self.project_button.pack(side="right")
        project_menu = tk.Menu(self.project_button,tearoff=False)
        project_menu.add_command(label="打开 / 新建工程…",command=self.open_project)
        project_menu.add_separator()
        project_menu.add_command(label="导出迁移包…",command=self.export_project)
        project_menu.add_command(label="导入迁移包…",command=self.import_project)
        self.project_button.configure(menu=project_menu)
        ttk.Separator(self.win).pack(fill="x")
        navigation = ttk.Frame(self.win,padding=(20,10))
        navigation.pack(fill="x")
        self.nav_buttons = {}
        for key,label,command in (("learn","自动学习",self.import_online),
                                  ("story","小说成片",self.show_story),
                                  ("library","影片资源",self.show_library),
                                  ("training","训练中心",self.show_training),
                                  ("annotation","人工校正",self.show_annotation),
                                  ("cache","缓存管理",self.show_cache)):
            button = ttk.Button(navigation,text=label,command=command)
            button.pack(side="left",padx=(0,8))
            self.nav_buttons[key] = button
        self.online_button = self.nav_buttons["learn"]
        ttk.Button(navigation,text="学习设置",command=self.configure_online_learning,
                   style="Compact.TButton").pack(side="right")
        self.page_area = ttk.Frame(self.win)
        self.annotation_page = ttk.Frame(self.page_area)
        caption = ttk.Frame(self.annotation_page,padding=(16,4))
        caption.pack(fill="x")
        ttk.Label(caption,text="人工校正",style="Section.TLabel").pack(side="left")
        ttk.Label(caption,text="补充截图、角色与台词，纠正下一次学习使用的资料",
                  style="Muted.TLabel").pack(side="left",padx=12)
        self.train_button = ttk.Button(caption,text="手动训练…",command=self.start_training,style="Accent.TButton")
        self.train_button.pack(side="right")
        ttk.Button(caption,text="导出学习素材",command=self.export).pack(side="right",padx=8)
        panes = ttk.Panedwindow(self.annotation_page,orient="horizontal")
        side_card = ttk.Frame(panes,style="Outline.TFrame",padding=1)
        main_card = ttk.Frame(panes,style="Outline.TFrame",padding=1)
        panes.add(side_card,weight=0)
        panes.add(main_card,weight=1)
        left = ttk.Frame(side_card,padding=(12,11))
        left.pack(fill="both",expand=True)
        main = ttk.Frame(main_card,padding=(12,10))
        main.pack(fill="both",expand=True)
        side_head = ttk.Frame(left)
        side_head.pack(fill="x",pady=(0,8))
        ttk.Label(side_head,text="截图素材",style="Section.TLabel").pack(side="left")
        ttk.Label(side_head,textvariable=self.frame_count,style="Muted.TLabel").pack(side="right")
        ttk.Label(left,text="当前电影",style="Caption.TLabel").pack(anchor="w",pady=(0,4))
        self.movie_picker = ttk.Combobox(left,state="readonly",width=21)
        self.movie_picker.pack(fill="x",pady=(0,7))
        self.movie_picker.bind("<<ComboboxSelected>>",lambda e:self.refresh(reset_batches=True))
        filt = ttk.Combobox(left,textvariable=self.filter,values=["全部状态",*STATES.values()],state="readonly",width=21)
        filt.pack(fill="x",pady=(0,9))
        filt.bind("<<ComboboxSelected>>",lambda e:self.refresh())
        listing = ttk.Frame(left,style="Outline.TFrame",padding=1)
        scroll = ttk.Scrollbar(listing,orient="vertical")
        self.tree = ThumbnailTree(listing,scroll,columns=("time","state"),show=("tree","headings"),selectmode="extended",height=6)
        self.tree.heading("#0",text="批次 / 缩略图",anchor="w")
        self.tree.heading("time",text="时间",anchor="w")
        self.tree.heading("state",text="标注状态",anchor="w")
        self.tree.column("#0",width=111,minwidth=107)
        self.tree.column("time",width=65,stretch=False)
        self.tree.column("state",width=61,stretch=False)
        self.tree.tag_configure("batch",background=theme.RAISED,foreground=theme.TEXT)
        for state,(foreground,_) in theme.STATE_COLORS.items():
            self.tree.tag_configure(state,foreground=foreground)
        scroll.pack(side="right",fill="y")
        self.tree.pack(side="left",fill="both",expand=True)
        self.tree.bind("<<TreeviewSelect>>",lambda e:self.show_selected())
        self.tree.bind("<<TreeviewClose>>",lambda e:self.collapse_batch())
        self.tree.bind("<Button-3>",self.show_tree_menu)
        side_actions = ttk.Frame(left)
        side_actions.pack(side="bottom",fill="x")
        self.delete_button = ttk.Button(side_actions,text="删除截图",command=self.delete_selected,state="disabled",
                                        style="Danger.TButton")
        self.delete_button.pack(side="left")
        self.trash_button = ttk.Button(side_actions,text="已删除截图",command=self.show_deleted,state="disabled",
                                       style="Compact.TButton")
        self.trash_button.pack(side="right")
        ttk.Label(left,textvariable=self.detail,wraplength=215,style="Muted.TLabel").pack(side="bottom",fill="x",pady=(9,8))
        listing.pack(fill="both",expand=True)

        movie_bar = ttk.Frame(main)
        movie_bar.pack(fill="x",pady=(0,10))
        self.capture_button = ttk.Button(movie_bar,text="生成截图",command=self.capture,state="disabled",style="Accent.TButton")
        self.capture_button.pack(side="left",padx=(0,7))
        ttk.Button(movie_bar,text="辅助识别",command=self.predict).pack(side="left",padx=(0,7))
        ttk.Separator(movie_bar,orient="vertical").pack(side="left",fill="y",padx=(2,9),pady=4)
        for label,fn in (("分析片段",lambda:self.import_movie(clip=True)),("角色确认",self.roles)):
            ttk.Button(movie_bar,text=label,command=fn).pack(side="left",padx=(0,7))
        self.cast_button = ttk.Menubutton(movie_bar,text="人物 / 资料",state="disabled")
        self.cast_button.pack(side="left",padx=(0,7))
        resource_menu = tk.Menu(self.cast_button,tearoff=False)
        resource_menu.add_command(label="演员表 / 标注角色名单",command=self.cast)
        resource_menu.add_command(label="演员、角色与剧情资料",command=self.show_movie_profiles)
        resource_menu.add_command(label="视频缓存与存放位置",command=self.show_cache)
        self.cast_button.configure(menu=resource_menu)
        sub_button = ttk.Menubutton(movie_bar,text="字幕 / 剧本")
        sub_menu = tk.Menu(sub_button,tearoff=False)
        sub_menu.add_command(label="导入字幕文件",command=self.import_subtitles)
        sub_menu.add_command(label="导入剧本并匹配",command=self.import_script)
        sub_menu.add_separator()
        sub_menu.add_command(label="提取 / 更新电影字幕",command=self.read_subtitles)
        sub_menu.add_command(label="导出字幕为 SRT",command=self.write_subtitles)
        sub_button.configure(menu=sub_menu)
        sub_button.pack(side="left")
        self.subtitle_button, self.subtitle_menu = sub_button, sub_menu
        ttk.Separator(main).pack(fill="x")
        toolbar = ttk.Frame(main)
        toolbar.pack(fill="x",pady=(7,7))
        self.cat = ttk.Combobox(toolbar,textvariable=self.category,state="readonly",width=6)
        self.cat.pack(side="left")
        ttk.Button(toolbar,text="新增类别",command=self.new_class,style="Compact.TButton").pack(side="left",padx=(5,4))
        ttk.Button(toolbar,text="人物名与台词",command=self.annotate,style="Compact.TButton").pack(side="left",padx=(0,4))
        self.voice_button = ttk.Button(toolbar,text="画外台词 / 旁白",command=self.frame_dialogue,style="Compact.TButton")
        self.voice_button.pack(side="left",padx=(0,4))
        ttk.Button(toolbar,text="改类别",command=self.reclass,style="Compact.TButton").pack(side="left",padx=(0,4))
        ttk.Button(toolbar,text="删除框",command=self.remove,style="Danger.TButton").pack(side="left",padx=(0,4))
        ttk.Button(toolbar,text="撤销",command=self.undo,style="Compact.TButton").pack(side="left")
        monitor = ttk.Frame(main,style="Outline.TFrame",padding=1)
        monitor.pack(fill="both",expand=True)
        self.canvas = BoxCanvas(monitor,self.cat_index,self.edited,self.select_box)
        self.canvas.bind("<Double-Button-1>",lambda e:self.annotate())
        self.canvas.bind("<Button-3>",self.show_frame_menu)
        self.canvas.pack(fill="both",expand=True)
        frame_strip = ttk.Frame(main)
        frame_strip.pack(fill="x",pady=(6,7))
        self.frame_badge = ttk.Label(frame_strip,text="未选择",style="empty.Badge.TLabel")
        self.frame_badge.pack(side="right")
        ttk.Label(frame_strip,textvariable=self.frame_metadata,style="Caption.TLabel").pack(side="right",padx=8)
        ttk.Label(frame_strip,textvariable=self.selection_text,style="Caption.TLabel").pack(side="left")
        actions = ttk.Frame(main)
        actions.pack(fill="x")
        self.feed_capture_button = ttk.Button(actions,text="投喂并生成下一批",command=self.feed_and_capture,state="disabled",
                                              style="Primary.TButton")
        self.feed_capture_button.pack(side="right")
        self.capture_more_button = ttk.Button(actions,text="确认并生成截图",command=self.capture_more,state="disabled")
        self.capture_more_button.pack(side="right",padx=6)
        self.confirm_button = ttk.Button(actions,text="确认图片并继续  ↵",command=self.confirm,state="disabled",
                                         style="Primary.TButton")
        self.confirm_button.pack(side="right")

        train_bar = ttk.Frame(self.win,padding=(16,8))
        train_controls = ttk.Frame(train_bar)
        train_controls.pack(side="right")
        self.feed_button = ttk.Button(train_controls,text="投喂最新资源",command=self.feed,state="disabled")
        self.feed_button.pack(side="left",padx=6)
        self.training_cancel = ttk.Button(train_controls,text="停止训练",command=self.stop_training,state="disabled",
                                          style="Danger.TButton")
        self.training_cancel.pack(side="left")
        train_info = ttk.Frame(train_bar)
        train_info.pack(side="left",fill="x",expand=True,padx=(0,10))
        title = ttk.Frame(train_info)
        title.pack(fill="x")
        self.training_dot = ttk.Label(title,text="●",style="idle.Dot.TLabel")
        self.training_dot.pack(side="left",padx=(0,7))
        ttk.Label(title,text="后台训练",style="Section.TLabel").pack(side="left")
        ttk.Label(title,textvariable=self.feed_status,style="Accent.TLabel").pack(side="left",padx=10)
        training_text = ttk.Label(train_info,textvariable=self.training_status,style="Caption.TLabel",wraplength=480)
        training_text.pack(anchor="w")
        status = ttk.Frame(self.win,padding=(16,5))
        status.pack(side="bottom",fill="x")
        ttk.Separator(self.win).pack(side="bottom",fill="x")
        train_bar.pack(side="bottom",fill="x")
        self.page_area.pack(fill="both",expand=True)
        panes.pack(fill="both",expand=True,padx=16,pady=(12,10))
        self.cancel = ttk.Button(status,text="停止任务",command=self.stop.set,state="disabled",style="Danger.TButton")
        self.cancel.pack(side="right",padx=(8,0))
        status_text = ttk.Label(status,textvariable=self.status,style="Muted.TLabel",wraplength=860)
        status_text.pack(side="left",fill="x",expand=True)
        train_info.bind("<Configure>",lambda e:training_text.configure(wraplength=max(160,e.width-6)))
        status.bind("<Configure>",lambda e:status_text.configure(wraplength=max(180,e.width-130)))

    def activate_page(self,key,frame):
        for child in self.page_area.winfo_children():
            if child is not frame:
                child.pack_forget()
        frame.pack(fill="both",expand=True)
        self.active_view = key
        for name,button in self.nav_buttons.items():
            button.configure(style="Accent.TButton" if name==key else "TButton")

    def show_annotation(self):
        self.activate_page("annotation",self.annotation_page)

    def show_story(self):
        if not self.root:
            self.use(Path.home()/"映格工程"/"默认工程")
        if self.story_window is None:
            from story_window import StoryWindow
            self.story_window = StoryWindow(self.win,self.root,container=self.page_area,
                on_close=lambda:setattr(self,"story_window",None),
                can_write=lambda:not self.storage_busy())
        self.activate_page("story",self.story_window.win)

    def story_busy(self):
        return bool((getattr(self,"story_window",None) and self.story_window.busy) or
                    (getattr(self,"learning_assembly_task",None) and self.learning_assembly_task.busy))

    def get_auto_learning_manager(self):
        if not self.root:
            raise ValueError("请先打开工程")
        if getattr(self,"auto_learning_manager",None) is None:
            from auto_learning import AutoLearningManager
            from video_analysis import analyse_video
            self.auto_learning_manager = AutoLearningManager(self.root,
                analyser=lambda root,movie,report,check:analyse_video(root,movie,report,check,
                    api_key_provider=lambda:getattr(self,"video_analysis_api_key","")))
        return self.auto_learning_manager

    def show_auto_learning(self):
        if not self.root:
            self.use(Path.home()/"映格工程"/"默认工程")
        window = getattr(self,"auto_learning_window",None)
        if window and window.win.winfo_exists():
            window.show()
            return
        from auto_learning_window import AutoLearningWindow
        self.auto_learning_window = AutoLearningWindow(self.win,self.get_auto_learning_manager(),
            on_open_board=self.open_auto_learning_board,
            on_import_helper=self.import_helper_transcript,
            on_video_analysis=self.show_video_analysis,
            on_dashboard=self.show_learning_dashboard,
            continuous_status=self.continuous_learning_status,
            on_close=lambda:setattr(self,"auto_learning_window",None),
            can_write=lambda:not self.storage_busy() and not self.closing and not self.busy)

    def show_learning_dashboard(self):
        if not self.root:
            self.use(Path.home()/"映格工程"/"默认工程")
        window = getattr(self,"learning_dashboard",None)
        if window and window.win.winfo_exists():
            window.show()
            return
        from learning_dashboard import LearningDashboard
        controller = self.get_continuous_learning_manager()
        if getattr(self,'learning_assembly_task',None) is None:
            from story_assembly import AssemblyTask
            self.learning_assembly_task=AssemblyTask(self.root);self.learning_assembly_task.restore()
        self.learning_dashboard = LearningDashboard(self.win,self.root,runtime=controller.snapshot,
            assembly_task=self.learning_assembly_task,
            can_write=lambda:not self.storage_busy() and not self.closing and not self.busy and not self.project_transfer
                and not (getattr(self,'story_window',None) and self.story_window.busy),
            on_start=self.start_continuous_learning,on_stop=self.stop_continuous_learning,
            on_jobs=self.show_auto_learning,on_sources=self.show_discovery,
            on_close=lambda:setattr(self,"learning_dashboard",None))

    def show_video_analysis(self,movie_id=None):
        if not self.root:
            self.use(Path.home()/"映格工程"/"默认工程")
        window = getattr(self,"video_analysis_window",None)
        if window and window.win.winfo_exists():
            window.show()
            window.select_movie(movie_id)
            return
        from video_analysis_window import VideoAnalysisWindow
        self.video_analysis_window = VideoAnalysisWindow(self.win,self.root,movie_id=movie_id,
            on_close=lambda:setattr(self,"video_analysis_window",None),
            can_write=lambda:not self.storage_busy() and not self.closing and not self.busy and not self._continuous_enabled(),
            on_save=self.save_video_analysis_settings,on_test_models=self.test_video_analysis_models,
            on_download_asr=self.download_speech_model,on_download_vision=self.download_vision_model,
            on_open_board=self.open_analysis_board)
        self.video_analysis_window.api_key.set(getattr(self,"video_analysis_api_key",""))

    def save_video_analysis_settings(self,settings,key):
        if self.closing or self.busy or self.storage_busy() or self._continuous_enabled():
            return False
        from video_analysis import save_settings
        saved = save_settings(self.root,settings)
        self.video_analysis_api_key = key
        self.feed_status.set("视频理解设置已保存 · 后续任务使用新设置；历史影片可选择重新分析")
        return saved

    def _model_progress(self,event):
        message = str(event.get("message","正在处理模型任务"))
        percent = event.get("percent")
        if isinstance(percent,(int,float)):
            message += f" · {percent:.1f}%"
        if event.get("url"):
            message += " · "+str(event["url"])
        if self.stop.is_set():
            raise RuntimeError("模型任务已停止")
        self.events.put(("video_model_status",message))

    def test_video_analysis_models(self,settings,key):
        if self.closing or self.busy or self.storage_busy() or self._continuous_enabled():
            return False
        from video_analysis import clean_settings
        settings = clean_settings(settings)
        root = self.root
        def test():
            from video_semantics import list_models
            return list_models(base_url=settings["base_url"],provider=settings["provider"],
                api_key=key or os.environ.get(settings["api_key_env"],""),report=self._model_progress,stop=self.stop)
        def completed(names):
            window = getattr(self,"video_analysis_window",None)
            if not self.closing and self.root==root and window and window.win.winfo_exists():
                window.set_models(names)
            self.status.set(f"模型服务已连接 · {len(names)} 个可用模型")
        self.start(test,completed)
        return True

    def download_speech_model(self,size):
        if self.closing or self.busy or self.storage_busy() or self._continuous_enabled():
            return False
        root = self.root
        def download():
            from video_transcription import download_model
            return str(download_model(size,BASE/"weights"/"asr",report=self._model_progress,stop=self.stop))
        def completed(path):
            window = getattr(self,"video_analysis_window",None)
            if not self.closing and self.root==root and window and window.win.winfo_exists():
                window.set_status(f"{size} 语音模型已就绪 · {path}；保存设置后可重新分析影片")
            self.status.set(f"本地语音模型已就绪 · {path}")
        self.start(download,completed)
        return True

    def download_vision_model(self,settings,key=""):
        if self.closing or self.busy or self.storage_busy() or self._continuous_enabled():
            return False
        from video_analysis import clean_settings
        settings = clean_settings(settings)
        if settings["provider"]!="ollama":
            raise ValueError("一键准备用于本机 Ollama；兼容接口请填写服务中已部署的视觉模型")
        root = self.root
        def download():
            from video_model_setup import prepare_vision_model
            return prepare_vision_model(base_url=settings["base_url"],report=self._model_progress,stop=self.stop)
        def completed(name):
            window = getattr(self,"video_analysis_window",None)
            if not self.closing and self.root==root and window and window.win.winfo_exists():
                window.values["visual_model"].set(name)
                window.values["visual_enabled"].set(True)
                window.set_status(f"视觉模型已就绪 · {name}；保存设置后重新分析影片")
            self.status.set(f"本地视觉模型已就绪 · {name}")
        self.start(download,completed)
        return True

    def open_analysis_board(self,note_path,block_ids=None):
        if self.closing or self.busy or self.storage_busy():
            return False
        from video_analysis import write_json
        from video_analysis_notes import load_reference,to_storyboard
        note = load_reference(self.root,note_path)
        board = to_storyboard(note,block_ids=block_ids)
        path = self.root/"auto_learning"/"boards"/f"analysis-{note['movie_id']}-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.json"
        write_json(path,board)
        return self.open_auto_learning_board(path)

    def import_helper_transcript(self,movie_id):
        if self.closing or self.busy or self.storage_busy():
            return False
        manager = self.get_auto_learning_manager()
        job = next((row for row in manager.list_jobs() if row["movie_id"]==movie_id),None)
        if not job:
            raise ValueError("请先添加并选择对应影片的自动学习任务")
        if job["status"] in {"running","queued","paused"}:
            raise ValueError("请先完成或取消该影片的分析，再导入转写")
        path = filedialog.askopenfilename(parent=self.win,
            title=f"为《{job['title']}》选择 Video Helper 的 transcript.json",
            filetypes=[("Video Helper 转写","*.json")])
        if not path:
            return False
        root,version = self.root,self.training_cancel_version
        def import_file():
            from video_helper_import import import_transcript
            return import_transcript(root,movie_id,path,report=self.progress)
        def imported(result):
            self.status.set(f"Video Helper 转写已导入 · {result['cue_count']} 条")
            if not self.closing and self.root==root and self.training_cancel_version==version:
                self.get_auto_learning_manager().enqueue([movie_id],force=True)
                self.status.set(f"Video Helper 转写已导入 · {result['cue_count']} 条；已提交重新分析")
        self.start(import_file,imported)
        return True

    def open_auto_learning_board(self,path):
        if self.storage_busy() or self.closing:
            return False
        path = Path(path)
        if not path.is_absolute():
            path = self.root/path
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("自动学习草稿不在当前工程中，请重新分析")
        self.show_story()
        return self.story_window.open_board(path)

    def learn_auto_movies(self,movie_ids,*,request_version=None,show_panel=False):
        if self.closing or not self.root:
            return False
        if self.storage_busy():
            self.feed_status.set("缓存管理或工程迁移正在执行，完成后可继续自动学习")
            return False
        if request_version is not None and request_version!=self.training_cancel_version:
            self.feed_status.set("视频已导入 · 自动学习已取消")
            return False
        ids = list(dict.fromkeys(movie_ids))
        if not ids:
            return False
        jobs = self.get_auto_learning_manager().enqueue(ids)
        self.feed_status.set(f"自动编排学习 · 已提交 {len(jobs)} 个视频任务；在自动学习任务中查看进度")
        if show_panel:
            self.show_auto_learning()
        return True

    def poll_auto_learning(self):
        self.retired_auto_learning_managers = [manager for manager in
            getattr(self,"retired_auto_learning_managers",[]) if manager.busy]
        manager = getattr(self,"auto_learning_manager",None)
        if manager is None:
            return
        if time.monotonic()-getattr(self,"auto_learning_wake_at",0) >= .5:
            self.auto_learning_wake_at = time.monotonic()
            manager.wake()
        for event in manager.drain_events():
            if not self.closing and event.get("message"):
                self.feed_status.set("自动学习 · "+event["message"])
            window = getattr(self,"video_analysis_window",None)
            if window and event.get("status") in {"completed","partial"} and window.win.winfo_exists():
                window.refresh_notes()

    def get_discovery_manager(self):
        if not self.root:
            raise ValueError("请先打开工程")
        if getattr(self,"discovery_manager",None) is None:
            from discovery_learning import DiscoveryManager
            self.discovery_manager = DiscoveryManager(self.root)
        return self.discovery_manager

    def get_continuous_learning_manager(self):
        if not self.root:
            raise ValueError("请先打开工程")
        if getattr(self,"continuous_learning_manager",None) is None:
            from continuous_learning import ContinuousLearningManager
            self.continuous_learning_manager = ContinuousLearningManager(self.root,self.get_discovery_manager())
        return self.continuous_learning_manager

    def _continuous_enabled(self):
        manager = getattr(self,"continuous_learning_manager",None)
        return bool(manager and manager.active)

    def continuous_learning_status(self):
        if not self.root:
            return {"active":False,"state":"idle","message":"打开工程后即可一键启动自动学习",
                    "totals":{"ready":False,"message":"打开工程后统计学习成果"}}
        manager = self.get_continuous_learning_manager()
        snapshot = manager.snapshot()
        if manager.active and getattr(self,"continuous_disk_paused",False):
            snapshot.update(state="disk-low",phase="disk-low",message="缓存磁盘空间不足，下载与新增查找已暂停；腾出空间后自动继续")
        return {**snapshot,**getattr(self,"discovery_activity",{}),"totals":self._learning_totals()}

    def _learning_totals(self, *, force=False):
        """Small, read-only durable snapshots shared by all learning windows."""
        import sqlite3
        from learning_totals import summarize_learning
        root, now = Path(self.root).resolve(), time.monotonic()
        previous = getattr(self,"learning_totals_cache",None)
        same_project = getattr(self,"learning_totals_root",None)==root
        if same_project and previous is not None and not force and now-getattr(self,"learning_totals_at",0)<2:
            return previous
        def read(filename, table, *, raw=False):
            path = root/filename
            if not path.is_file():
                return []
            # Looking at cumulative results never constructs/resumes a worker.
            db = sqlite3.connect(path.as_uri()+"?mode=ro",uri=True,timeout=.1)
            try:
                db.row_factory = sqlite3.Row
                columns = "*" if raw else "data"
                rows = db.execute(f"SELECT {columns} FROM {table}").fetchall()
                return [dict(row) if raw else json.loads(row["data"]) for row in rows]
            finally:
                db.close()
        try:
            totals = summarize_learning(read("online_jobs.sqlite","online_jobs"),
                read("auto_learning.sqlite","jobs"),read("discovery.sqlite","candidates"),
                movies=read("annotations.sqlite","movies",raw=True))
        except (OSError,ValueError,sqlite3.Error) as exc:
            # A briefly locked database affects the display only, never the
            # automatic pipeline. Keep the last observed counters labelled stale.
            totals = {**(previous if same_project and previous else {}),"ready":False,
                      "message":"累计成果暂未刷新，稍后自动重试："+str(exc)[:250]}
        self.learning_totals_root, self.learning_totals_at, self.learning_totals_cache = root,now,totals
        return totals

    def start_continuous_learning(self):
        if self.closing or self.busy or self.storage_busy():
            self.status.set("请等待当前任务完成后启动自动学习")
            return False
        if not self.root:
            self.use(Path.home()/"映格工程"/"默认工程")
        manager = self.get_continuous_learning_manager()
        if manager.active:
            self.show_learning_dashboard()
            return True
        self.continuous_models_ready = False
        self.continuous_disk_paused = False
        manager.start()
        self.show_learning_dashboard()
        self.feed_status.set("正在自动配置本地模型；完成后持续查找、下载和学习三站视频")
        return True

    def stop_continuous_learning(self):
        errors = []
        def attempt(label,action):
            try:
                return action()
            except Exception as exc:
                errors.append(f"{label}：{exc}")
        manager = getattr(self,"continuous_learning_manager",None)
        if manager is not None:
            attempt("停止查找",manager.stop)
        self.continuous_models_ready = False
        self.continuous_disk_paused = False
        online = getattr(self,"online_manager",None)
        if online is not None:
            attempt("暂停下载",online.pause)
        automatic = getattr(self,"auto_learning_manager",None)
        if automatic is not None:
            attempt("暂停分析",automatic.pause)
        # Cancel this mode's pending training handoffs, leaving unrelated model
        # training alone. Its completed video files and learning outputs remain.
        rows = (attempt("读取下载记录",online.list_jobs) or []) if online else []
        for key in list(getattr(self,"online_learning_batches",{})):
            batch = key[0]
            if any(row["batch_id"]==batch and (row.get("discovery") or {}).get("continuous") for row in rows):
                self.online_learning_batches.pop(key,None)
        pending = getattr(self,"pending_movie_learning",None)
        if pending and pending[0].get("_continuous"):
            self.pending_movie_learning = None
        if (getattr(self,"feed_movie_settings",None) or {}).get("_continuous"):
            self.feed_pending = False
            self.feed_snapshot = None
            self.feed_movie_settings = None
        if self.training and self.training[3].name==getattr(self,"continuous_training_run",None):
            self.continuous_training_stop = True
            self.training_stop.set()
            self.training_status.set("正在停止自动电影训练；已有成果保留")
        message = ("停止请求已发送，部分状态需检查："+"；".join(errors) if errors else
                   "持续自动学习已停止；下载与分析已暂停，缓存和已学成果保留")
        self.feed_status.set(message)
        if errors:
            self.status.set(message)
        return not errors

    def _continuous_handoff_allowed(self,row):
        if not (row.get("discovery") or {}).get("continuous"):
            return True
        manager = getattr(self,"continuous_learning_manager",None)
        return bool(manager and manager.active and getattr(self,"continuous_models_ready",False))

    def poll_continuous_learning(self):
        retired = []
        for previous in getattr(self,"retired_continuous_managers",[]):
            previous.tick()
            if previous.busy:
                retired.append(previous)
        self.retired_continuous_managers = retired
        manager = getattr(self,"continuous_learning_manager",None)
        if manager is None or self.closing:
            return
        now = time.monotonic()
        if now-getattr(self,"continuous_poll_at",0)<.5:
            return
        self.continuous_poll_at = now
        if not manager.active:
            manager.tick()
            return
        online,automatic = getattr(self,"online_manager",None),getattr(self,"auto_learning_manager",None)
        downloads = online.list_jobs() if online else []
        analyses = automatic.list_jobs() if automatic else []
        from cache_storage import get_settings
        import shutil
        cache = Path(get_settings(self.root)["current"])
        while not cache.exists() and cache != cache.parent:
            cache = cache.parent
        workload = {"pending_downloads":sum(row["status"] in {"queued","running","paused"} for row in downloads),
                    "pending_learning":sum(row["status"] in {"queued","running","paused"} for row in analyses),
                    "training_busy":bool(self.training or self.pending_movie_learning),
                    "free_bytes":shutil.disk_usage(cache).free,
                    "paused":self.storage_busy() or bool(online and online.paused) or bool(automatic and automatic.paused)}
        if manager.active and getattr(self,"continuous_models_ready",False) and online:
            low_space = workload["free_bytes"] < manager.get_config()["min_free_gib"]*1024**3
            if low_space and not online.paused:
                online.pause()
                self.continuous_disk_paused = True
            elif not low_space and getattr(self,"continuous_disk_paused",False):
                online.resume(request_version=self.training_cancel_version)
                self.continuous_disk_paused = False
            workload["paused"] = self.storage_busy() or online.paused or bool(automatic and automatic.paused)
        manager.tick(workload=workload)
        for event in manager.drain_events():
            if event.get("type")=="models-ready" and manager.active:
                # Resume only after model readiness, including durable completed
                # downloads whose downstream handoff was paused before exit.
                if self.online_manager is None:
                    from online_jobs import OnlineJobManager
                    self.online_manager = OnlineJobManager(self.root)
                self.online_manager.migrate_restricted_jobs()
                self.online_manager.resume(request_version=self.training_cancel_version)
                automatic = self.get_auto_learning_manager()
                automatic.resume()
                completed = [row for row in self.online_manager.list_jobs()
                    if (row.get("discovery") or {}).get("continuous") and row["status"]=="completed"]
                refs = {row["movie_id"]:row for row in automatic.list_jobs()}
                retry_movies = []
                for row in completed:
                    movie_id = (row.get("result") or {}).get("movie_id")
                    reference = refs.get(movie_id,{})
                    if reference.get("status") in {"needs_input","partial","failed","cancelled"}:
                        retry_movies.append(movie_id)
                if retry_movies:
                    automatic.enqueue(retry_movies,force=True)
                self.online_manager.request_learning([row["id"] for row in completed],request_version=self.training_cancel_version)
                self.auto_learning_recovery_pending = True
                self.continuous_models_ready = True
                self.video_analysis_api_key = ""
            if event.get("message"):
                self.feed_status.set("持续学习 · "+str(event["message"]))

    def show_discovery(self):
        if not self.root:
            self.use(Path.home()/"映格工程"/"默认工程")
        window = getattr(self,"discovery_window",None)
        if window and window.win.winfo_exists():
            window.show()
            return
        from discovery_window import DiscoveryWindow
        self.discovery_window = DiscoveryWindow(self.win,self.get_discovery_manager(),
            on_dashboard=self.show_learning_dashboard,
            on_close=lambda:setattr(self,"discovery_window",None),
            on_open_learning=self.show_auto_learning,
            on_queue_selected=self.queue_discovered,
            can_write=lambda:not self.storage_busy() and not self.closing,
            activity=lambda:getattr(self,"discovery_activity",{}),
            on_start_continuous=self.start_continuous_learning,on_stop_continuous=self.stop_continuous_learning,
            continuous_status=self.continuous_learning_status)

    def queue_discovered(self,ids):
        if self.storage_busy() or self.closing:
            return False
        self.get_discovery_manager().request_download(ids)
        self.discovery_poll_at = 0.
        self.poll_discovery()
        return True

    def temporal_learning_states(self):
        """Combine saved outcomes with this process's actual active/pending jobs."""
        from movie_usage import MovieUsageIndex
        if getattr(self,"discovery_usage",None) is None:
            self.discovery_usage = MovieUsageIndex(self.root,
                active=lambda:self.training[3].name if self.training else None)
        states = {identifier:{"status":value["last_state"],"run":value["last_run"],
                              "message":{"completed":"电影时序训练完成", "failed":"训练失败，请查看训练日志",
                                         "stopped":"训练已停止", "unfinished":"上次训练未完成"}.get(value["last_state"],value["last_state"])}
                  for identifier,value in self.discovery_usage.snapshot().items()}
        if self.training and self.training_kind=="movie-temporal":
            run = self.training[3]
            job = read_json(self.root/"logs"/(run.name+"-train.json"))
            for identifier in job.get("movie_ids",[]):
                states[identifier] = {"status":"running","run":run.name,"message":self.training_status.get()}
        snapshots = []
        pending = getattr(self,"pending_movie_learning",None)
        if pending:
            snapshots.append(pending[1])
        if getattr(self,"feed_pending",False) and self.training_kind=="movie-temporal":
            snapshots.append(getattr(self,"feed_snapshot",None))
        for snapshot in snapshots:
            for movie in (snapshot or {}).get("movies",[]):
                states[movie["id"]] = {"status":"queued","message":"等待当前训练轮次结束后学习"}
        return states

    def learn_discovered_movies(self,ids,*,request_version):
        if not self.learn_auto_movies(ids,request_version=request_version):
            return False
        states = self.temporal_learning_states()
        usage = getattr(self,"discovery_usage",None)
        if usage is not None and not usage.ready:
            self.feed_status.set("正在读取已有电影学习记录…" + (" · "+usage.last_error if usage.last_error else ""))
            return False
        if usage is not None:
            # ready is published after values; reread in case the first scan
            # finished between the preceding snapshot and the readiness check.
            states = self.temporal_learning_states()
        remaining = [identifier for identifier in ids if states.get(identifier,{}).get("status")
                     not in {"running","queued","completed"}]
        if not remaining:
            return True
        cfg = dict(self.movie_training_settings or self.movie_defaults())
        cfg["movie_ids"] = remaining
        # Unconfirmed actors/story are omitted by snapshot_sources. Automatic
        # observation can train without pretending those optional facts are reviewed.
        return self.submit_movie_config(cfg,merge_pending=True,purpose="discovery-auto",review_profiles=False) is True

    def poll_discovery(self):
        self.retired_discovery_managers = [manager for manager in
            getattr(self,"retired_discovery_managers",[]) if manager.busy]
        manager = getattr(self,"discovery_manager",None)
        if manager is None or self.closing:
            return
        now = time.monotonic()
        if now-getattr(self,"discovery_poll_at",0)<.5:
            return
        self.discovery_poll_at = now
        if not self.storage_busy():
            from discovery_learning import video_site
            from online_jobs import OnlineJobManager
            for candidate in manager.pending_candidates():
                added = []
                try:
                    video_site(candidate["url"])
                    if self.online_manager is None:
                        self.online_manager = OnlineJobManager(self.root)
                        self.auto_learning_recovery_pending = True
                    online = self.online_manager
                    kind = "story-and-movie" if candidate["category"]=="movie" else "story-reference"
                    result = online.add(candidate["url"],mode="time",height=720,subtitles=True,
                                        learn=True,learning_kind=kind,request_version=self.training_cancel_version)
                    added = result["added"]
                    if result["errors"]:
                        raise ValueError(result["errors"][0]["message"])
                    jobs = online.list_jobs()
                    job = next((row for row in jobs if row["id"] in added),None)
                    if job is None:
                        job = next((row for row in jobs if row["url"]==candidate["url"] and
                            row.get("learning_kind",row.get("config",{}).get("learning_kind"))==kind and
                            row.get("config",{}).get("height")==720 and row.get("config",{}).get("mode")=="time" and
                            row.get("config",{}).get("subtitles") and row.get("config",{}).get("learn")),None)
                    if job is None:
                        raise ValueError("无法关联下载记录，请在下载队列中检查")
                    continuous = getattr(self,"continuous_learning_manager",None)
                    owned = continuous.snapshot().get("owned_round_ids",[]) if continuous else []
                    online.attach_discovery(job["id"],{**candidate,
                        "continuous":candidate["round_id"] in owned and not candidate.get("manual_requested")})
                    accepted = manager.ack_candidate(candidate["id"],job["id"],learning_kind=kind)
                    if not accepted:
                        if added:
                            online.cancel(added)
                        continue
                    if job["status"] in {"failed","cancelled","skipped"} and candidate.get("manual_requested"):
                        online.retry([job["id"]],request_version=self.training_cancel_version)
                    if job["status"]=="completed" and candidate.get("manual_requested"):
                        movie_id = (job.get("result") or {}).get("movie_id")
                        if movie_id is not None:
                            automatic = self.get_auto_learning_manager()
                            reference = next((row for row in automatic.list_jobs() if row["movie_id"]==movie_id),{})
                            if reference.get("status") in {"failed","needs_input","cancelled"}:
                                automatic.enqueue([movie_id],force=True)
                        online.request_learning([job["id"]],request_version=self.training_cancel_version)
                        self.auto_learning_recovery_pending = True
                    if job["status"]=="completed" and not job.get("learning_dispatched"):
                        self.auto_learning_recovery_pending = True
                    # An already paused queue stays paused until its own Continue action.
                    if not online.paused:
                        online.start()
                except Exception as exc:
                    if added and self.online_manager is not None:
                        self.online_manager.cancel(added)
                    manager.set_candidate_state(candidate["id"],"failed",str(exc))
        candidates = manager.list_candidates()
        linked = [row for row in candidates if row.get("online_job_id") and row["state"] not in {"discovered","dispatching"}]
        downloads, learning = [], []
        if linked:
            from discovery_status import candidate_status
            if self.online_manager is None:
                from online_jobs import OnlineJobManager
                self.online_manager = OnlineJobManager(self.root)
                self.auto_learning_recovery_pending = True
            jobs = {row["id"]:row for row in self.online_manager.list_jobs()}
            references = {row["movie_id"]:row for row in self.get_auto_learning_manager().list_jobs()}
            temporal = self.temporal_learning_states() if any(row.get("learning_kind")=="story-and-movie" for row in linked) else {}
            for candidate in linked:
                job = jobs.get(candidate["online_job_id"])
                movie_id = ((job or {}).get("result") or {}).get("movie_id")
                update = candidate_status(candidate,job,references.get(movie_id),temporal.get(movie_id))
                changed = manager.set_candidate_state(candidate["id"],**update)
                continuous = getattr(self,"continuous_learning_manager",None)
                if changed and job and (job.get("discovery") or {}).get("continuous") and continuous:
                    if update["state"]=="skipped":
                        continuous.notify_skipped(job["id"],site=candidate.get("site",""),
                            reason=update["detail"],title=job.get("title") or candidate["title"],url=candidate["url"])
                    elif job["status"]=="completed" and candidate.get("movie_id") is None:
                        continuous.notify_download_success(site=candidate.get("site",""))
                active = {**update,"title":(job or {}).get("title") or candidate["title"],
                          "source_url":candidate["url"],"message":update["detail"],"percent":update.get("progress")}
                if update["state"]=="downloading":
                    downloads.append(active)
                elif update["state"]=="learning":
                    learning.append(active)
        self.discovery_activity = {"current_downloads":downloads,"current_learning":learning}
        for event in manager.drain_events():
            if event.get("message"):
                self.feed_status.set("联网学习 · "+event["message"])

    def undo_key(self,event=None):
        if self.active_view=="annotation" and self.win.focus_get() in (self.canvas,self.tree):
            self.undo()
            return "break"

    def show_library(self):
        if not self.root:
            self.use(Path.home()/"映格工程"/"默认工程")
        if self.library_page is None:
            from movie_library import MovieLibrary
            self.library_page = MovieLibrary(self.page_area,self.root,
                on_import=self.import_movie,on_profiles=self.show_movie_profiles,
                on_learn=lambda ids:self.learn_imported_movies(ids,selected_only=True),
                on_annotate=self.open_online_movie,on_subtitles=self.open_movie_subtitles,
                on_training=self.show_training,on_online=self.add_online,
                active_training=lambda:self.training[3].name if self.training else None)
        self.library_page.show()
        self.activate_page("library",self.library_page.frame)

    def open_movie_subtitles(self,movie_id):
        if self.open_online_movie(movie_id):
            self.subtitle_menu.tk_popup(self.subtitle_button.winfo_rootx(),
                                        self.subtitle_button.winfo_rooty()+self.subtitle_button.winfo_height())

    def add_online(self):
        self.import_online()
        self.online_window._set_composer_expanded(True)
        self.online_window.links.focus_set()

    def ready(self):
        if self.busy or self.storage_busy():
            return False
        if not self.root:
            self.open_project()
        return self.root is not None

    def open_project(self):
        if self.busy or self.training or self.storage_busy():
            return
        path = filedialog.askdirectory(title="选择或新建工程文件夹")
        if path:
            self.use(Path(path))

    def transfer_ready(self):
        if self.busy or self.training or self.online_active() or self.storage_busy() or self.story_busy():
            self.status.set("请等当前任务完成，并停止后台学习和下载后再迁移工程")
            return False
        if getattr(self,"story_window",None) and not self.story_window.save_current():
            return False
        if self.profile_window and self.profile_window.win.winfo_exists():
            if self.profile_window.close() is False:
                return False
            self.profile_window = None
        return True

    def export_project(self):
        if not self.root or not self.transfer_ready():
            return
        cfg = options(self.win,"导出工程迁移包",[("videos","原片","包含原片（推荐）",
                      ["包含原片（推荐）","不包含原片"] )],
                      prompt="迁移包包含标注、影片资料和模型历史。带上原片，才能在另一台电脑继续电影学习。",action="选择保存位置")
        if not cfg:
            return
        path = filedialog.asksaveasfilename(parent=self.win,title="保存工程迁移包",defaultextension=".zip",
                initialfile=f"{self.root.name}-迁移包-{datetime.now():%Y%m%d-%H%M%S}.zip",filetypes=[("工程迁移包","*.zip")])
        if not path:
            return
        if not self.transfer_ready():
            return
        from portable_project import export_project
        source = self.root
        def work():
            result = export_project(source,Path(path),include_videos=cfg["videos"].startswith("包含"),report=self.progress)
            return f"工程迁移包已保存：{path}",result
        def complete(result):
            self.refresh_movies()
            self.refresh()
            missing = self.transfer_notes(result[1])
            message = f"{result[0]}\n在另一台电脑安装应用后，从「工程 → 导入迁移包」恢复。"
            messagebox.showinfo("迁移包已导出",message+(f"\n\n{missing}" if missing else ""),parent=self.win)
        self.start(work,complete)
        self.project_transfer = True
        self.cancel.configure(state="disabled")

    def import_project(self):
        if not self.transfer_ready():
            return
        archive = filedialog.askopenfilename(parent=self.win,title="选择工程迁移包",filetypes=[("工程迁移包","*.zip")])
        if not archive:
            return
        destination = filedialog.askdirectory(parent=self.win,title="选择一个空文件夹恢复工程（不会覆盖已有工程）")
        if not destination:
            return
        if not self.transfer_ready():
            return
        from portable_project import import_project
        def work():
            result = import_project(Path(archive),Path(destination),report=self.progress)
            return f"工程已恢复：{destination}",result
        def complete(result):
            if self.use(Path(destination)):
                self.status.set(result[0]+" · 在影片资源中选择影片继续学习；训练中心可查看原有模型")
            else:
                self.status.set(result[0]+" · 当前工程尚未切换，请稍后从工程菜单打开此文件夹")
            notes = self.transfer_notes(result[1])
            if notes:
                messagebox.showinfo("工程已恢复，部分资源需补齐",notes,parent=self.win)
        self.start(work,complete)
        self.project_transfer = True
        self.cancel.configure(state="disabled")

    @staticmethod
    def transfer_notes(result):
        notes = []
        if result.get("missing_videos"):
            notes.append(f"有 {len(result['missing_videos'])} 部影片未包含原片，需重新导入或下载后才能继续电影学习。")
        if result.get("missing_weights"):
            notes.append(f"有 {len(result['missing_weights'])} 份历史检测权重未找到。对应模型需要相同检测权重才能兼容续学，请在学习设置中补齐。")
        return "\n".join(notes)

    def use(self,path):
        if self.busy or self.training or self.story_busy():
            self.status.set("请等待当前任务完成后再切换工程")
            return False
        if self.storage_busy():
            self.status.set("缓存管理正在执行，请完成后再切换工程")
            return False
        for name in ("story_window","profile_window","cache_window"):
            window = getattr(self,name,None)
            if window and window.win.winfo_exists():
                if window.close() is False:
                    return False
            setattr(self,name,None)
        dataset.initialize(path)
        self.close_online_project()
        self.profile_waiting = {}
        if self.training_window and self.training_window.win.winfo_exists():
            self.training_window.close()
        self.training_window = None
        if self.library_page:
            self.library_page.close()
            self.library_page = None
        self.root = path.resolve()
        self.batch_settings.clear()
        self.training_settings = None
        self.training_kind = "movie-temporal"
        self.movie_training_settings = None
        self.pending_movie_learning = None
        self.feed_status.set("")
        self.title.set(self.root.name)
        self.filter.set("全部状态")
        self.names = dataset.classes(self.root)
        self.cat["values"] = self.names
        self.cat.current(0)
        self.row = None
        self.movie_picker.set("")
        self.refresh_movies()
        self.refresh(reset_batches=True)
        self.status.set(str(self.root))
        self.training_status.set("训练尚未启动")
        self.training_dot.configure(style="idle.Dot.TLabel")
        portable = read_json(self.root/"portable_settings.json")
        if isinstance(portable.get("latest_movie_settings"),dict):
            self.movie_training_settings = self.movie_defaults()
            self.movie_training_settings.update({k:v for k,v in portable["latest_movie_settings"].items()
                                                if k in {"epochs","sample_fps","patience","learning_rate"}})
        self.import_online()
        return True

    def refresh_movies(self, prefer=None):
        current = self.movies.get(self.movie_picker.get())
        if prefer is None and current:
            prefer = current["id"]
        self.movies = {f"{movie['name']} · #{movie['id']}":movie for movie in dataset.movies(self.root)}
        self.movie_picker["values"] = list(self.movies)
        self.movie_picker.set(next((key for key,row in self.movies.items() if row["id"]==prefer),next(iter(self.movies),"")))
        state = "normal" if self.movies and not self.busy else "disabled"
        self.capture_button.configure(state=state)
        self.cast_button.configure(state=state)
        self.trash_button.configure(state=state)

    def selected_item(self):
        selected = self.tree.selection()
        focus = self.tree.focus()
        return focus if focus in selected else next(iter(selected),"")

    def selected_batch(self):
        selected = self.selected_item()
        if not selected:
            return None
        item = self.tree.parent(selected) or selected
        return item.removeprefix("batch:")

    def collapse_batch(self):
        item = self.tree.focus()
        if item:
            hidden = [child for child in self.tree.selection() if self.tree.parent(child)==item]
            if hidden:
                self.tree.selection_remove(*hidden)
                self.tree.selection_add(item)

    def refresh(self, prefer=None, *, reset_batches=False):
        if not self.root:
            return
        if prefer is None and self.row and not reset_batches:
            prefer = self.row["id"]
        previous = None if reset_batches else self.selected_batch()
        roots = self.tree.get_children()
        opened = {item for item in roots if self.tree.item(item,"open")} if not reset_batches else set()
        movie = self.movies.get(self.movie_picker.get())
        self.rows = dataset.frames(self.root,movie["id"]) if movie else []
        order = dataset.frame_batches(self.root,movie["id"]) if movie else []
        self.frame_count.set(f"{len(self.rows)} 张")
        self.batch_rows = {}
        for r in self.rows:
            self.batch_rows.setdefault(r["batch_id"],[]).append(r)
        self.batch_labels = {batch:f"第 {i} 批" for i,batch in enumerate((b for b in order if b),1)}
        if "" in self.batch_rows:
            self.batch_labels[""] = "历史截图"
        preferred = next((row for row in self.rows if row["id"]==prefer),None)
        active = preferred["batch_id"] if preferred else previous
        if active not in self.batch_rows:
            active = next((batch for batch in reversed(order) if batch in self.batch_rows),None)
        self.tree.delete(*roots)
        selected_filter = self.filter.get()
        for batch in reversed(order):
            rows = self.batch_rows.get(batch)
            if not rows:
                continue
            group = f"batch:{batch}"
            self.tree.insert("","end",iid=group,text=self.batch_labels[batch],values=(f"{len(rows)} 张",),tags=("batch",),
                             open=group in opened or (batch==active and previous!=active))
            for r in rows:
                if selected_filter not in ("全部状态",STATES[r["state"]]):
                    continue
                sec = r["ms"]/1000
                split = "训" if r["split"]=="train" else "验"
                self.tree.insert(group,"end",iid=str(r["id"]),values=(f"{int(sec//60):02}:{sec%60:04.1f} {split}",STATES[r["state"]]),
                                 tags=(r["state"],))
        self.tree.set_paths({str(row["id"]):self.root/row["path"] for row in self.rows})
        self.row = None
        if active is not None:
            group = f"batch:{active}"
            if preferred and self.tree.exists(str(prefer)):
                key = str(prefer)
            elif previous!=active or preferred:
                key = next(iter(self.tree.get_children(group)),group)
            else:
                key = group
            self.tree.selection_set(key)
            self.tree.focus(key)
            self.tree.see(key)
        self.show_selected()

    def update_capture_more(self):
        complete = self.review_complete()
        self.capture_more_button.configure(text="生成下一批截图" if complete else "确认并生成截图",
                                           state="normal" if not self.busy and (complete or self.row) else "disabled")
        self.feed_capture_button.configure(state="normal" if self.can_feed_and_capture() else "disabled")
        self.confirm_button.configure(state="normal" if self.row and not self.busy else "disabled",
                                      style="TButton" if complete else "Primary.TButton")
        self.update_delete_button()

    def review_complete(self):
        rows = self.batch_rows.get(self.selected_batch(),())
        return bool(rows) and all(row["state"]=="reviewed" for row in rows)

    def can_feed_and_capture(self):
        return (not self.busy and not self.closing and self.review_complete()
                and not (self.training and self.training_stop.is_set()))

    def capture_more(self):
        if self.review_complete():
            self.capture(next_batch=True)
        else:
            self.confirm(capture=True)

    def feed_and_capture(self):
        if self.can_feed_and_capture():
            self.capture(next_batch=True,feed_reviewed=True)

    def show_selected(self):
        selection = self.selected_item()
        if selection and self.row and str(self.row["id"])==selection:
            self.update_delete_button()
            return
        batch = self.selected_batch()
        rows = self.batch_rows.get(batch,())
        self.row = next((r for r in rows if str(r["id"])==selection),None)
        self.history = []
        r = self.row
        if r:
            self.voices = json.loads(r["voices"])
            self.voice_button.configure(text=f"画外台词 / 旁白 ({len(self.voices)})")
            self.canvas.load(self.root/r["path"],json.loads(r["boxes"]),self.names)
            split = "训练" if r["split"]=="train" else "验证"
            self.detail.set(f"{self.batch_labels[batch]} · {len(rows)} 张截图")
            self.frame_metadata.set(f"{r['ms']/1000:.3f} 秒  /  {split}集")
            self.frame_badge.configure(text=STATES[r["state"]],style=f"{r['state']}.Badge.TLabel")
        else:
            self.voices = []
            self.voice_button.configure(text="画外台词 / 旁白")
            self.canvas.load(None,[],self.names,prompt=("选择截图","展开左侧批次，或生成新的截图。") if self.rows else
                             ("开始画面标注","从当前电影生成截图。" if self.movies else "导入一部电影，然后生成截图。"))
            self.frame_metadata.set("")
            self.frame_badge.configure(text="未选择",style="empty.Badge.TLabel")
            if batch is not None:
                self.detail.set(f"{self.batch_labels[batch]} · {len(rows)} 张截图")
            else:
                self.detail.set("本片还没有截图" if self.movies else "请先导入电影")
        self.select_box()
        self.update_capture_more()

    def selected_frames(self):
        return [int(item) for item in self.tree.selection() if item.isdecimal()]

    def update_delete_button(self):
        count = len(self.selected_frames())
        self.delete_button.configure(text=f"删除所选 ({count})" if count>1 else "删除截图",
                                     state="normal" if count and not self.busy else "disabled")

    def delete_key(self, event):
        focus = self.win.focus_get()
        if focus is self.tree:
            self.delete_selected()
            return "break"
        if focus is self.canvas:
            self.remove()
            return "break"

    def delete_selected(self):
        self.delete_screenshots(self.selected_frames())

    def delete_screenshots(self, frame_ids):
        movie = self.movies.get(self.movie_picker.get())
        if self.busy or not movie or not frame_ids:
            return
        count = len(frame_ids)
        if not messagebox.askyesno("删除截图",f"删除所选 {count} 张截图？可以在「已删除截图」中恢复。",
                                    parent=self.win,default="no"):
            return
        count = dataset.set_frames_deleted(self.root,movie["id"],frame_ids,deleted=True)
        self.refresh()
        self.status.set(f"已删除 {count} 张截图")

    def show_deleted(self):
        movie = self.movies.get(self.movie_picker.get())
        if self.busy or not movie:
            return
        rows = dataset.frames(self.root,movie["id"],deleted=True)
        if not rows:
            self.status.set("本片暂无已删除截图")
            return
        dlg,body = theme.sheet(self.win,"已删除截图",context=movie["name"])
        ttk.Label(body,text="选择要恢复的截图。按住 Ctrl 或 Shift 可多选。",style="Muted.TLabel").pack(anchor="w",pady=(0,8))
        list_panel = ttk.Frame(body)
        list_panel.pack(fill="both",expand=True)
        listing = ttk.Treeview(list_panel,columns=("time","state"),show="headings",selectmode="extended",height=8)
        listing.heading("time",text="截图时间")
        listing.heading("state",text="标注状态")
        listing.column("time",width=160)
        listing.column("state",width=105)
        scroll = ttk.Scrollbar(list_panel,command=listing.yview)
        listing.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right",fill="y")
        listing.pack(side="left",fill="both",expand=True)
        for row in reversed(rows):
            listing.insert("","end",iid=str(row["id"]),values=(f"{row['ms']/1000:.3f} 秒",STATES[row["state"]]))

        def restore():
            selected = [int(item) for item in listing.selection()]
            if not selected:
                return
            count = dataset.set_frames_deleted(self.root,movie["id"],selected,deleted=False)
            dlg.destroy()
            self.filter.set("全部状态")
            self.refresh(selected[0])
            self.status.set(f"已恢复 {count} 张截图")

        buttons = ttk.Frame(body)
        buttons.pack(fill="x",pady=(12,0))
        recover = ttk.Button(buttons,text="恢复所选",command=restore,state="disabled",style="Primary.TButton")
        recover.pack(side="right")
        ttk.Button(buttons,text="关闭",command=dlg.destroy).pack(side="right",padx=8)
        listing.bind("<<TreeviewSelect>>",lambda _:recover.configure(state="normal" if listing.selection() else "disabled"))
        dlg.bind("<Escape>",lambda _:dlg.destroy())
        dlg.grab_set()
        listing.focus_set()

    def popup_menu(self, event):
        try:
            self.context_menu.tk_popup(event.x_root,event.y_root)
        finally:
            self.context_menu.grab_release()
        return "break"

    def frame_menu(self, menu):
        frame = self.row
        if not frame:
            return
        id_ = frame["id"]
        menu.add_command(label="辅助识别此图",command=lambda:self.win.after_idle(lambda:self.predict(frame_id=id_)))
        idx = self.canvas.selected
        person = idx is not None and self.names[self.canvas.boxes[idx][0]] in dataset.PERSON
        menu.add_command(label="人物名与台词",command=self.annotate,state="normal" if person else "disabled")
        menu.add_command(label="画外台词 / 旁白",command=self.frame_dialogue)
        menu.add_command(label="确认图片",command=self.confirm)

    def show_frame_menu(self, event):
        if self.busy or not self.row:
            return
        self.canvas.select_at(event)
        menu = self.context_menu
        menu.delete(0,"end")
        self.frame_menu(menu)
        menu.add_separator()
        frame = self.row["id"]
        menu.add_command(label="删除此截图",command=lambda:self.delete_screenshots([frame]))
        return self.popup_menu(event)

    def show_tree_menu(self, event):
        if self.busy:
            return
        item = self.tree.identify_row(event.y)
        menu = self.context_menu
        menu.delete(0,"end")
        if item:
            self.tree.focus_set()
            self.tree.focus(item)
            if item not in self.tree.selection():
                self.tree.selection_set(item)
            self.show_selected()
            if item.isdecimal():
                self.frame_menu(menu)
                menu.add_separator()
            else:
                children = self.tree.get_children(item)
                if children:
                    def select_batch_images():
                        self.tree.item(item,open=True)
                        self.tree.focus(children[0])
                        self.tree.selection_set(*children)
                    menu.add_command(label="选择本批可见截图",command=select_batch_images)
                menu.add_command(label="辅助识别本批",command=lambda:self.win.after_idle(self.predict))
            count = len(self.selected_frames())
            if count:
                menu.add_command(label=f"删除所选截图 ({count})",command=self.delete_selected)
        menu.add_command(label="已删除截图",command=self.show_deleted)
        return self.popup_menu(event)

    def cat_index(self):
        return max(0,self.cat.current())

    def select_box(self):
        idx = self.canvas.selected
        if idx is None:
            self.selection_text.set("拖动画框或移动，拖角缩放。Shift 拖动可画重叠框")
        else:
            box = self.canvas.boxes[idx]
            note = box[5] if len(box)>5 else {}
            text = " / ".join(note.get("lines",[]))
            self.selection_text.set(f"框 {idx+1} · {note.get('role') or self.names[box[0]]}  {text[:32]}")

    def annotate(self):
        idx = self.canvas.selected
        if self.busy or not self.row or idx is None:
            self.status.set("先单击要绑定台词的人物框")
            return
        box = self.canvas.boxes[idx]
        if self.names[box[0]] not in dataset.PERSON:
            self.status.set("请选择人物框，或先将所选框的类别改为「人物」")
            return
        note = box[5] if len(box)>5 else {}
        result = edit_note(self.win,self.root,self.row,note)
        if result is not None:
            box[5:] = [result] if result["role"] or result["lines"] else []
            self.edited()
            self.canvas.overlay()

    def frame_dialogue(self):
        if self.busy or not self.row:
            return
        manage_voices(self.win,self.root,self.row,self.voices,self.edited)

    def new_class(self):
        if not self.ready():
            return
        name = simpledialog.askstring("新增类别","对象名称，例如「手机」「杯子」",parent=self.win)
        if name:
            dataset.add_class(self.root,name)
            self.names = dataset.classes(self.root)
            self.cat["values"] = self.names
            self.category.set(name.strip())
            self.canvas.names = self.names

    def edited(self, remember=True):
        r = self.row
        if not r:
            return
        dataset.save_boxes(self.root,r["id"],self.canvas.boxes,"draft",voices=self.voices)
        if remember:
            self.history.append((r["boxes"],r["voices"]))
        r["voices"] = json.dumps(self.voices,ensure_ascii=False)
        self.voice_button.configure(text=f"画外台词 / 旁白 ({len(self.voices)})")
        r["boxes"],r["state"] = json.dumps(self.canvas.boxes),"draft"
        self.tree.set(str(r["id"]),"state",STATES["draft"])
        self.tree.item(str(r["id"]),tags=("draft",))
        self.frame_badge.configure(text=STATES["draft"],style="draft.Badge.TLabel")
        self.select_box()
        self.update_capture_more()

    def undo(self):
        if not self.busy and self.row and self.history:
            boxes,voices = self.history.pop()
            self.canvas.boxes = json.loads(boxes)
            self.voices = json.loads(voices)
            self.canvas.selected = None
            self.edited(False)
            self.canvas.overlay()

    def remove(self):
        if self.busy or self.canvas.selected is None:
            return
        self.canvas.boxes.pop(self.canvas.selected)
        self.canvas.selected = None
        self.edited()
        self.canvas.overlay()

    def reclass(self):
        if not self.busy and self.canvas.selected is not None:
            box = self.canvas.boxes[self.canvas.selected]
            if len(box)>5 and self.names[self.cat_index()] not in dataset.PERSON:
                if not messagebox.askyesno("更改类别","改为道具将清除这个框的人物名和台词，继续？",parent=self.win):
                    return
                del box[5:]
            box[0] = self.cat_index()
            self.edited()
            self.canvas.overlay()

    def confirm(self, capture=False):
        if self.busy or not self.row:
            return
        r = self.row
        if not self.canvas.boxes and not self.voices and not messagebox.askyesno("确认无目标","将此图作为无目标的负样本加入数据集？",parent=self.win):
            return
        dataset.save_boxes(self.root,r["id"],self.canvas.boxes,"reviewed",voices=self.voices)
        r["state"] = "reviewed"
        pending = [row["id"] for row in self.batch_rows[r["batch_id"]] if row["state"]!="reviewed"]
        if pending:
            visible = [key for key in pending if self.tree.exists(str(key))]
            if not visible:
                self.filter.set("全部状态")
            candidates = visible or pending
            self.refresh(next((key for key in candidates if key>r["id"]),candidates[0]))
            self.status.set(f"截图已确认 · 本批还有 {len(pending)} 张待确认")
        else:
            self.refresh(r["id"])
            self.status.set("本批截图均已确认。点击「投喂并生成下一批」可边训练边续采。")
        if capture:
            self.capture(next_batch=not pending)

    def start(self,task,on_success=None):
        if self.storage_busy():
            self.status.set("正在管理视频缓存，请完成后再开始任务")
            return False
        self.busy = True
        self.after_success = on_success
        self.stop.clear()
        self.canvas.enabled = False
        self.movie_picker.configure(state="disabled")
        self.capture_button.configure(state="disabled")
        self.cast_button.configure(state="disabled")
        self.trash_button.configure(state="disabled")
        self.update_capture_more()
        self.cancel.configure(state="normal")
        self.status.set("正在准备任务…")
        def work():
            try:
                self.events.put(("done",task()))
            except Exception as exc:
                self.events.put(("error",str(exc)))
        threading.Thread(target=work,daemon=True).start()

    def progress(self,message):
        if self.stop.is_set():
            raise RuntimeError("任务已停止，已保存的数据可继续使用")
        self.events.put(("status",message))

    def poll(self):
        try:
            self.poll_continuous_learning()
        except Exception as exc:
            self.stop_continuous_learning()
            self.status.set(f"持续自动学习已暂停 · {exc}")
        try:
            self.poll_discovery()
        except Exception as exc:
            self.status.set(f"联网学习任务消息 · {exc}")
        try:
            self.poll_online()
        except Exception as exc:
            self.status.set(f"在线视频任务消息 · {exc}")
        try:
            self.poll_auto_learning()
        except Exception as exc:
            self.status.set(f"自动学习任务消息 · {exc}")
        if getattr(self,"profile_waiting",None) and time.monotonic()-getattr(self,"profile_poll_at",0)>.7:
            self.profile_poll_at = time.monotonic()
            self.resume_profile_learning()
        while not self.events.empty():
            kind,msg = self.events.get_nowait()
            if kind=="video_model_status":
                self.status.set(msg)
                window = getattr(self,"video_analysis_window",None)
                if window and window.win.winfo_exists():
                    window.set_status(msg)
                continue
            if kind.startswith("train_"):
                try:
                    self.poll_training(kind,msg)
                except Exception as exc:
                    self.training_status.set(f"后续训练未启动 · {exc}")
                    if self.training is None:
                        self.project_button.configure(state="normal")
                        self.train_button.configure(state="normal")
                        self.feed_button.configure(state="disabled")
                        self.training_cancel.configure(state="disabled")
                    if not self.closing:
                        messagebox.showerror("训练消息",str(exc),parent=self.win)
                continue
            self.status.set(msg[0] if isinstance(msg,tuple) else msg)
            if kind in ("done","error"):
                self.busy = False
                self.project_transfer = False
                self.canvas.enabled = True
                self.movie_picker.configure(state="readonly")
                self.cancel.configure(state="disabled")
                callback,self.after_success = self.after_success,None
                if kind=="done" and callback:
                    try:
                        callback(msg)
                    except Exception as exc:
                        self.status.set(f"资源已处理，后续操作未完成 · {exc}")
                        if not self.closing:
                            messagebox.showerror("后续操作未完成",str(exc),parent=self.win)
                else:
                    self.refresh_movies()
                    self.refresh()
                if kind=="error" and not self.closing:
                    messagebox.showinfo("任务消息",msg,parent=self.win)
        if self.closing and not self.busy and not self.training and not self.online_active():
            self.win.destroy()
            return
        self.win.after(120,self.poll)

    def import_movie(self,clip=False):
        if self.busy:
            return
        path = filedialog.askopenfilename(parent=self.win,title="选择要跟踪的电影" if clip else "选择要导入的电影",
                                          filetypes=MOVIE_TYPES)
        if not path:
            return
        destination = self.root or Path.home()/"映格工程"/"默认工程"
        if clip:
            fields = [("start","起点（秒）","0",None),("length","片段时长（秒）","12",None),
                      ("weights","人物检测权重",self.default_weight(destination),"file")]
        else:
            fields = [("mode","首次导入分组","按片内时段",list(MODES))]
        cfg = options(self.win,("分析连续片段" if clip else "导入电影")+" · "+Path(path).name,fields)
        if not cfg:
            return
        if not self.root:
            self.use(destination)
        if clip:
            args = [sys.executable,"-u",str(BASE/"model_tasks.py"),"track",str(self.root),cfg["weights"],
                    "--source",path,"--start",str(float(cfg["start"])),"--length",str(float(cfg["length"]))]
            self.start(lambda:self.run_worker("track",args))
            return
        def load():
            movie_id,report = dataset.import_movie(self.root,path,MODES[cfg["mode"]],self.progress)
            return f"已导入 {Path(path).name} · {report} · 在影片资源中准备资料并开始学习",movie_id
        def select_imported(result):
            self.refresh_movies(result[1])
            self.filter.set("全部状态")
            self.refresh(reset_batches=True)
            self.show_library()
        self.start(load,select_imported)

    def import_online(self):
        if not self.root:
            self.use(Path.home()/"映格工程"/"默认工程")
        if self.online_window and self.online_window.win.winfo_exists():
            self.activate_page("learn",self.online_window.win)
            return
        from online_jobs import OnlineJobManager
        from online_window import OnlineWindow
        if self.online_manager is None:
            self.online_manager = OnlineJobManager(self.root)
            self.auto_learning_recovery_pending = True
        self.online_window = OnlineWindow(
            self.win,self.online_manager,on_open_movie=self.open_online_movie,
            on_learn=lambda ids:self.learn_imported_movies(ids,selected_only=True),
            on_open_training=self.show_training,on_close=lambda:setattr(self,"online_window",None),
            request_version=lambda:self.training_cancel_version,
            on_cache=self.show_cache,on_profiles=self.show_movie_profiles,
            on_auto_learning=self.show_auto_learning,
            on_discovery=self.show_discovery,
            on_dashboard=self.show_learning_dashboard,
            on_start_continuous=self.start_continuous_learning,on_stop_continuous=self.stop_continuous_learning,
            continuous_status=self.continuous_learning_status,
            on_auto_learn=lambda ids:self.learn_auto_movies(ids,show_panel=True),
            on_learning_settings=self.configure_online_learning,can_download=lambda:not self.storage_busy(),
            active_training=lambda:self.training[3].name if self.training else None,container=self.page_area)
        self.activate_page("learn",self.online_window.win)

    def storage_busy(self):
        window = getattr(self,"cache_window",None)
        return bool(getattr(self,"project_transfer",False) or (window and window.busy))

    def show_cache(self):
        if self.project_transfer:
            self.status.set("工程迁移正在执行，请完成后再管理缓存")
            return
        if not self.root:
            self.use(Path.home()/"映格工程"/"默认工程")
        if self.cache_window and self.cache_window.win.winfo_exists():
            self.activate_page("cache",self.cache_window.win)
            return
        from cache_window import CacheWindow
        self.cache_window = CacheWindow(self.win,self.root,
            is_busy=lambda:bool(self.busy or self.training or self.online_active()),on_changed=self.cache_changed,
            container=self.page_area)
        self.activate_page("cache",self.cache_window.win)

    def cache_changed(self):
        if not self.closing:
            self.refresh_movies()
            self.resume_profile_learning()

    def movie_defaults(self):
        weight = self.default_weight(self.root)
        portable = read_json(self.root/"portable_settings.json")
        relative = portable.get("latest_movie_detector")
        if relative == "":
            weight = ""
        elif isinstance(relative,str):
            candidate = (self.root/relative).resolve()
            if candidate.is_relative_to(self.root.resolve()) and candidate.is_file():
                weight = str(candidate)
        return {"weights":weight if weight and Path(weight).is_file() else "","epochs":DEFAULT_MOVIE_EPOCHS,
                "device":"","sample_fps":1.,"movie_ids":None,"patience":5,"learning_rate":.001,"restart":False}

    @staticmethod
    def movie_options(cfg):
        return {"patience":cfg.get("patience",5),"learning_rate":cfg.get("learning_rate",.001),
                "restart":cfg.get("restart",False),
                **({"continuous":bool(cfg["_continuous"])} if "_continuous" in cfg else {})}

    def configure_online_learning(self):
        if not self.root or self.storage_busy():
            return False
        window = getattr(self,"online_window",None)
        choice = getattr(window,"learning_kind",None)
        if choice is not None and choice.get()=="自动编排学习（推荐）":
            self.show_auto_learning()
            return True
        from training_setup import training_setup
        current = self.movies.get(self.movie_picker.get())
        cfg = training_setup(self.win,dataset.movies(self.root),current["id"] if current else None,
                             self.movie_defaults()["weights"],initial=self.movie_training_settings,movie_only=True)
        if cfg:
            self.movie_training_settings = cfg
            self.feed_status.set(f"电影学习设置已保存 · 最多 {cfg['epochs']} 轮 · 无改善时提前停止")
        return bool(cfg)

    def show_movie_profiles(self,movie_ids=None):
        if self.storage_busy():
            self.status.set("工程资源正在迁移或管理，请完成后再编辑影片资料")
            return False
        if not self.root:
            self.status.set("请先导入电影，再准备演员、角色和剧情资料")
            return False
        if movie_ids is None:
            movie = self.movies.get(self.movie_picker.get())
            movie_ids = [movie["id"]] if movie else [row["id"] for row in dataset.movies(self.root)]
        movie_ids = list(dict.fromkeys(movie_ids))
        if not movie_ids:
            self.status.set("请先导入电影")
            return False
        if self.profile_window and self.profile_window.win.winfo_exists():
            self.profile_window.add_movies(movie_ids)
            self.profile_window.win.lift()
            return True
        from movie_profile_window import MovieProfileWindow
        self.profile_window = MovieProfileWindow(self.win,self.root,movie_ids,
            on_ready=self.resume_profile_learning,on_close=lambda:setattr(self,"profile_window",None))
        return True

    def ensure_movie_profiles(self,movie_ids,resume,*,purpose="learning"):
        from movie_profiles import required_missing
        ids = list(dict.fromkeys(movie_ids)) if movie_ids is not None else [row["id"] for row in dataset.movies(self.root)]
        if not required_missing(self.root,ids):
            return True
        key = (str(self.root),self.training_cancel_version,tuple(sorted(ids)),purpose)
        self.profile_waiting[key] = (ids,resume)
        self.feed_status.set("学习准备中 · 请核对演员、角色和剧情资料；缺失资料可手动填写")
        self.training_cancel.configure(state="normal")
        self.show_movie_profiles(ids)
        return False

    def resume_profile_learning(self):
        if self.closing or self.storage_busy() or (self.training and self.training_stop.is_set()):
            return
        from movie_profiles import required_missing
        for key,(ids,resume) in list(getattr(self,"profile_waiting",{}).items()):
            if key[:2]!=(str(self.root),self.training_cancel_version):
                self.profile_waiting.pop(key,None)
                continue
            try:
                if required_missing(self.root,ids):
                    continue
                self.profile_waiting.pop(key,None)
                resume()
            except Exception as exc:
                self.profile_waiting.pop(key,None)
                self.feed_status.set(f"资料准备后学习未启动 · {exc}")

    def open_online_movie(self,movie_id):
        if self.busy:
            self.status.set("当前任务结束后即可打开在线视频标注，下载可继续进行")
            return False
        self.refresh_movies(movie_id)
        self.filter.set("全部状态")
        self.refresh(reset_batches=True)
        self.show_annotation()
        self.win.lift()
        return True

    def online_active(self):
        managers = [getattr(self,"online_manager",None),*getattr(self,"retired_online_managers",[])]
        managers.extend([getattr(self,"auto_learning_manager",None),
                         *getattr(self,"retired_auto_learning_managers",[])])
        managers.extend([getattr(self,"discovery_manager",None),
                         *getattr(self,"retired_discovery_managers",[])])
        continuous = [getattr(self,"continuous_learning_manager",None),
                      *getattr(self,"retired_continuous_managers",[])]
        return (any(manager is not None and manager.busy for manager in managers) or
                any(manager is not None and (manager.active or manager.busy) for manager in continuous))

    def close_online_project(self):
        dashboard = getattr(self,"learning_dashboard",None)
        if dashboard and dashboard.win.winfo_exists():
            dashboard.close()
        self.learning_dashboard = None
        task=getattr(self,'learning_assembly_task',None)
        if task and task.busy:task.cancel()
        self.learning_assembly_task=None
        continuous = getattr(self,"continuous_learning_manager",None)
        if continuous is not None:
            continuous.close()
            if not hasattr(self,"retired_continuous_managers"):
                self.retired_continuous_managers = []
            self.retired_continuous_managers.append(continuous)
        self.continuous_learning_manager = None
        self.continuous_models_ready = False
        self.continuous_disk_paused = False
        self.continuous_training_run = None
        self.continuous_poll_at = 0.
        self.learning_totals_cache = None
        self.learning_totals_root = None
        self.learning_totals_at = 0.
        model_window = getattr(self,"video_analysis_window",None)
        if model_window and model_window.win.winfo_exists():
            model_window.close()
        self.video_analysis_window = None
        self.video_analysis_api_key = ""
        discovery_window = getattr(self,"discovery_window",None)
        if discovery_window and discovery_window.win.winfo_exists():
            discovery_window.close()
        self.discovery_window = None
        discovery_manager = getattr(self,"discovery_manager",None)
        if discovery_manager is not None:
            discovery_manager.close()
            if not hasattr(self,"retired_discovery_managers"):
                self.retired_discovery_managers = []
            self.retired_discovery_managers.append(discovery_manager)
        self.discovery_manager = None
        usage = getattr(self,"discovery_usage",None)
        if usage:
            usage.close()
        self.discovery_usage = None
        self.discovery_activity = {}
        self.discovery_poll_at = 0.
        auto_window = getattr(self,"auto_learning_window",None)
        if auto_window and auto_window.win.winfo_exists():
            auto_window.close()
        self.auto_learning_window = None
        auto_manager = getattr(self,"auto_learning_manager",None)
        if auto_manager is not None:
            auto_manager.close()
            if not hasattr(self,"retired_auto_learning_managers"):
                self.retired_auto_learning_managers = []
            self.retired_auto_learning_managers.append(auto_manager)
        self.auto_learning_manager = None
        window = getattr(self,"online_window",None)
        if window and window.win.winfo_exists():
            window.close()
        self.online_window = None
        manager = getattr(self,"online_manager",None)
        if manager is not None:
            manager.close()
            self.retired_online_managers.append(manager)
        self.online_manager = None
        self.online_learning_batches = {}
        self.auto_learning_recovery_pending = False
        self.auto_handoff_retries = {}
        self.online_refresh_pending = False
        self.online_poll_at = 0.
        if hasattr(self,"online_button"):
            self.online_button.configure(text="自动学习")

    def poll_online(self):
        self.retired_online_managers = [manager for manager in getattr(self,"retired_online_managers",[]) if manager.busy]
        manager = getattr(self,"online_manager",None)
        if manager is None:
            return
        now = time.monotonic()
        if not self.closing and now-getattr(self,"online_poll_at",0)<.5:
            return
        self.online_poll_at = now
        rows = manager.list_jobs()
        by_id = {row["id"]:row for row in rows}
        # Recover only the new workflow's unacknowledged durable handoffs. Old
        # movie jobs retain their original event-driven behavior.
        if getattr(self,"auto_learning_recovery_pending",False) and not self.closing:
            self.auto_learning_recovery_pending = False
            for row in rows:
                kind = row.get("learning_kind",row.get("config",{}).get("learning_kind","movie-temporal"))
                if (kind in {"story-reference","story-and-movie"} and row["status"]=="completed" and
                        self._continuous_handoff_allowed(row) and
                        row.get("learn",row.get("config",{}).get("learn")) and
                        not row.get("learning_dispatched") and not row.get("learning_cancelled") and
                        row.get("result",{}).get("movie_id") is not None):
                    key = (row["batch_id"],self.training_cancel_version,kind)
                    self.online_learning_batches.setdefault(key,set()).add(row["result"]["movie_id"])
        pending = sum(row["status"] in {"queued","running","paused"} for row in rows)
        self.online_button.configure(text=f"自动学习 ({pending})" if pending else "自动学习")
        for event in manager.drain_events():
            if event.get("type")!="completed":
                continue
            self.online_refresh_pending = True
            row = by_id.get(event.get("job_id"))
            if (self.closing or row is None or not event.get("learn") or row.get("learning_cancelled")
                    or not self._continuous_handoff_allowed(row)):
                continue
            version = event.get("request_version",row.get("request_version",0))
            if version!=self.training_cancel_version:
                self.feed_status.set("在线视频已完成 · 自动学习已取消")
                continue
            movie_id = event.get("result",{}).get("movie_id")
            if movie_id is not None:
                kind = row.get("learning_kind",row.get("config",{}).get("learning_kind","movie-temporal"))
                key = (row["batch_id"],version,kind)
                self.online_learning_batches.setdefault(key,set()).add(movie_id)
        if self.online_refresh_pending and not self.busy and not self.closing:
            had_movies = bool(self.movies)
            self.refresh_movies()
            if not had_movies:
                self.refresh(reset_batches=True)
            self.online_refresh_pending = False
        for key,ids in list(self.online_learning_batches.items()):
            batch,version,kind = key
            if any(row["batch_id"]==batch and not self._continuous_handoff_allowed(row) for row in rows):
                continue
            retry = getattr(self,"auto_handoff_retries",{}).get(key,{})
            if time.monotonic() < retry.get("after",0):
                continue
            if version!=self.training_cancel_version or self.closing:
                del self.online_learning_batches[key]
                continue
            unfinished = any(row["batch_id"]==batch and row["status"] in {"queued","running","paused"} for row in rows)
            if not unfinished:
                if self.storage_busy() or (self.training and self.training_stop.is_set()):
                    # New requests made after Stop wait for the old worker to exit.
                    continue
                del self.online_learning_batches[key]
                # Include successful members downloaded before a pause or restart.
                ids.update(row["result"]["movie_id"] for row in rows
                           if row["batch_id"]==batch and row["status"]=="completed"
                           and row.get("learn",row.get("config",{}).get("learn"))
                           and not row.get("learning_cancelled")
                           and row.get("learning_kind",row.get("config",{}).get("learning_kind","movie-temporal"))==kind
                           and row.get("request_version",0)==version
                           and row.get("result",{}).get("movie_id") is not None)
                try:
                    if kind in {"story-reference","story-and-movie"}:
                        learn = self.learn_discovered_movies if kind=="story-and-movie" else self.learn_auto_movies
                        accepted = learn(sorted(ids),request_version=version)
                        if accepted:
                            manager.acknowledge_learning([row["id"] for row in rows
                                if row["batch_id"]==batch and row["status"]=="completed" and
                                row.get("result",{}).get("movie_id") in ids and
                                row.get("learning_kind",row.get("config",{}).get("learning_kind","movie-temporal"))==kind])
                            getattr(self,"auto_handoff_retries",{}).pop(key,None)
                        else:
                            self.online_learning_batches[key] = ids
                    else:
                        self.learn_imported_movies(sorted(ids),request_version=version)
                except Exception as exc:
                    self.feed_status.set(f"在线视频已导入，自动学习未启动 · {exc}")
                    if kind in {"story-reference","story-and-movie"}:
                        self.online_learning_batches[key] = ids
                        if not hasattr(self,"auto_handoff_retries"):
                            self.auto_handoff_retries = {}
                        attempts = retry.get("attempts",0)+1
                        self.auto_handoff_retries[key] = {"attempts":attempts,
                            "after":time.monotonic()+min(30,2**min(attempts,5))}

    def learn_imported_movie(self, movie_id, *, request_version=None):
        return self.learn_imported_movies([movie_id],request_version=request_version)

    def learn_imported_movies(self, movie_ids, *, request_version=None,selected_only=False):
        """Queue online resources without changing the active worker's task type."""
        from movie_sequence_data import snapshot_sources
        movie_ids = list(dict.fromkeys(movie_ids))
        if not movie_ids:
            return False
        if self.storage_busy():
            self.feed_status.set("缓存管理正在执行，完成后可继续学习")
            return False
        if request_version is not None and request_version!=self.training_cancel_version:
            self.feed_status.set("视频已导入 · 自动学习已取消")
            return False
        if self.training and self.training_stop.is_set():
            self.feed_status.set("视频已导入，停止完成后可重新开始电影学习")
            return False
        cfg = dict(self.movie_training_settings or self.movie_defaults())
        if selected_only:
            cfg["movie_ids"] = movie_ids
        elif cfg["movie_ids"] is not None:
            cfg["movie_ids"] = list(dict.fromkeys([*cfg["movie_ids"],*movie_ids]))
        return self.submit_movie_config(cfg,merge_pending=True,
                                        purpose="online-selected" if selected_only else "online-auto")

    def submit_movie_config(self,cfg,*,merge_pending=False,purpose="manual-train",review_profiles=True):
        from movie_sequence_data import snapshot_sources
        cfg = dict(cfg)
        continuous = getattr(self,"continuous_learning_manager",None)
        cfg["_continuous"] = bool(purpose=="discovery-auto" and continuous and continuous.active)
        if self.storage_busy():
            self.feed_status.set("缓存管理正在执行，完成后可继续学习")
            return False
        if review_profiles and not self.ensure_movie_profiles(cfg["movie_ids"],
                lambda:self.submit_movie_config(cfg,merge_pending=merge_pending,purpose=purpose),purpose=purpose):
            return "waiting-profiles"
        reviewed = snapshot_sources(self.root,cfg["movie_ids"])
        pending = (self.pending_movie_learning[1] if self.pending_movie_learning else
                   getattr(self,"feed_snapshot",None) if self.training_kind=="movie-temporal"
                   and getattr(self,"feed_pending",False) else None)
        if merge_pending and self.training and pending and "movies" in pending:
            previous_cfg = (self.pending_movie_learning[0] if self.pending_movie_learning else
                            getattr(self,"feed_movie_settings",None) or {})
            # A mixed manual/automatic batch belongs to the manual workflow;
            # stopping continuous mode must not discard a user's reviewed work.
            cfg["_continuous"] = cfg["_continuous"] and bool(previous_cfg.get("_continuous"))
            from movie_sequence_data import fingerprint
            movies = {movie["id"]:movie for movie in pending["movies"]}
            movies.update({movie["id"]:movie for movie in reviewed["movies"]})
            reviewed = {**reviewed,"movies":[movies[key] for key in sorted(movies)]}
            reviewed.pop("fingerprint",None)
            reviewed["fingerprint"] = fingerprint(reviewed)
            if cfg["movie_ids"] is not None:
                cfg["movie_ids"] = sorted(movies)
        self.movie_training_settings = cfg
        if self.training and self.training_kind!="movie-temporal":
            self.pending_movie_learning = (cfg,reviewed)
            self.feed_status.set("在线视频已排队 · 截图训练结束后开始电影学习")
        elif self.training:
            self.feed(reviewed=reviewed)
        else:
            self.launch_training(cfg["weights"],cfg["epochs"],cfg["device"],
                                 self.latest_completed(self.root,kind="movie-temporal"),reviewed=reviewed,
                                 kind="movie-temporal",sample_fps=cfg["sample_fps"],**self.movie_options(cfg))
        return True

    def capture(self, *, next_batch=False, feed_reviewed=False):
        if self.busy:
            return
        movie = self.movies.get(self.movie_picker.get())
        if not movie:
            self.status.set("请先导入电影")
            return
        previous = self.batch_settings.get(movie["id"])
        defaults = previous or {"n":"1","layer":"标准 · 1 秒","from":"0","to":""}
        if next_batch and previous:
            cfg = previous
        else:
            fields = [("n","期望新增截图数",defaults["n"],None),
                      ("layer","采样层级",defaults["layer"],list(dataset.LAYERS)),
                      ("from","起点（秒）",defaults["from"],None),("to","终点（留空到片尾）",defaults["to"],None)]
            if not next_batch:
                fields.insert(0,("at","指定时间截图（秒，留空则抽样）","",None))
            caption = "投喂并生成下一批" if feed_reviewed else ("生成下一批截图" if next_batch else "生成截图")
            cfg = options(self.win,f"{caption} · {movie['name']}",fields)
        if not cfg:
            return
        at = float(cfg["at"]) if cfg.get("at") else None
        n,start,end = 1,0,None
        if at is None:
            n = int(cfg["n"])
            start,end = float(cfg["from"] or 0),float(cfg["to"]) if cfg["to"] else None
        def extract():
            count,first = dataset.generate_screenshots(self.root,movie,n,self.progress,
                                    step_ms=dataset.LAYERS[cfg["layer"]],start=start,end=end,at=at,next_batch=next_batch)
            if next_batch and count<n:
                message = f"新增 {count}/{n} 张截图 · 所选范围已无可补采画面"
            elif at is not None and not count:
                message = "此画面已有截图，已定位" if first else "此画面已删除，可在「已删除截图」中恢复"
            else:
                message = f"新增 {count}/{n} 张截图"
            return f"{message} · {movie['name']}",first,count
        def select_new(result):
            if at is None:
                self.batch_settings[movie["id"]] = {key:cfg[key] for key in ("n","layer","from","to")}
            self.refresh_movies(movie["id"])
            if result[1]:
                self.filter.set("全部状态")
                self.refresh(result[1],reset_batches=result[2]>0)
            else:
                self.refresh()
        if feed_reviewed:
            if not self.can_feed_and_capture():
                return
            self.submit_reviewed()
        self.start(extract,select_new)

    def import_subtitles(self):
        if not self.busy:
            from subtitles import import_subtitles
            self.import_movie_text("字幕",[("文字字幕",("*.srt","*.vtt","*.ass","*.ssa"))],
                                   lambda movie,path:import_subtitles(self.root,movie,path,self.progress))

    def cast(self):
        movie = self.movies.get(self.movie_picker.get())
        if movie and not self.busy:
            count = edit_cast(self.win,self.root,movie)
            if count is not None:
                self.status.set(f"演员表已保存 · {movie['name']} · {count} 人")

    def import_script(self):
        if not self.busy:
            from scripts import import_script
            self.import_movie_text("剧本",[("剧本",("*.txt","*.fountain"))],
                                   lambda movie,path:import_script(self.root,movie,path))

    def import_movie_text(self, label, types, load):
        movie = self.movies.get(self.movie_picker.get())
        if not movie:
            self.status.set("请先导入电影，再为当前电影导入字幕或剧本")
            return
        path = filedialog.askopenfilename(parent=self.win,title=f"为 {movie['name']} 导入{label}",filetypes=types)
        if path:
            def read():
                self.progress(f"正在读入{label}…")
                count = load(movie["id"],path)
                return f"已导入 {count} 条{label} · {movie['name']} · 可在截图绑定台词时查看匹配"
            self.start(read)

    def read_subtitles(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(parent=self.win,title="选择电影以提取或更新字幕",filetypes=MOVIE_TYPES)
        if path:
            if not self.root:
                self.use(Path.home()/"映格工程"/"默认工程")
            self.start(lambda:dataset.extract_subtitles(self.root,path,self.progress))

    def write_subtitles(self):
        if not self.ready():
            return
        from subtitles import export_srt, list_tracks
        tracks = {f"{r['name']} · {r['label']} (#{r['id']})":r["id"] for r in list_tracks(self.root)}
        if not tracks:
            messagebox.showinfo("导出字幕","工程中尚无文字字幕轨。可先选择「提取 / 更新电影字幕」。",parent=self.win)
            return
        cfg = options(self.win,"选择字幕轨",[("track","电影及字幕轨",next(iter(tracks)),list(tracks))])
        if cfg:
            path = filedialog.asksaveasfilename(parent=self.win,title="导出字幕",defaultextension=".srt",
                                                filetypes=[("SubRip 字幕","*.srt")])
            if path:
                export_srt(self.root,tracks[cfg["track"]],path)
                self.status.set(f"字幕已导出 · {path}")

    def roles(self):
        if self.ready():
            from role_review import RoleReview
            RoleReview(self.win,self.root)

    def latest_completed(self,root,*,detector=False,kind=None):
        completed = (path for path in root.glob("runs/*/completed.json")
                     if (not detector or (path.parent/"fit/weights/best.pt").is_file())
                     and (kind is None or read_json(path).get("kind","annotations")==kind))
        return max(completed,key=lambda path:path.stat().st_mtime_ns,default=None)

    def latest_detector(self,root):
        completed = self.latest_completed(root,detector=True)
        return str(completed.parent/"fit/weights/best.pt") if completed else None

    def default_weight(self,root,*,fallback=None):
        base = BASE/"weights"/"yolo11n.pt"
        portable = read_json(Path(root)/"portable_settings.json")
        relative = portable.get("latest_movie_detector")
        if relative == "":
            return self.latest_detector(root) or fallback or ""
        if isinstance(relative,str):
            weight = (Path(root)/relative).resolve()
            if weight.is_relative_to(Path(root).resolve()) and weight.is_file():
                return self.latest_detector(root) or fallback or str(weight)
        return self.latest_detector(root) or fallback or (str(base) if base.exists() else "yolo11n.pt")

    def predict(self, *, frame_id=None):
        if not self.ready():
            return
        movie = self.movies.get(self.movie_picker.get())
        row = next((row for row in self.rows if row["id"]==frame_id),None) if frame_id is not None else None
        if frame_id is not None and row is None:
            self.status.set("此截图已不在当前列表中，请重新选择")
            return
        batch = row["batch_id"] if row else self.selected_batch()
        if not movie or batch is None:
            self.status.set("请先选择一个截图批次")
            return
        label = self.batch_labels[batch]
        allow_validation = bool(row and row["split"]=="val" and row["state"]=="new")
        if not dataset.prediction_frames(self.root,movie["id"],batch,frame_id=frame_id,allow_validation=allow_validation):
            if row is None:
                self.status.set(f"{label}没有待辅助识别的训练图片")
                return
            if row["state"]!="new":
                message = f"这张截图的状态是「{STATES[row['state']]}」。辅助识别适用于未标注的截图。"
            else:
                message = "此截图的状态已变化，请重新选择要识别的截图。"
            self.status.set(message)
            messagebox.showinfo("辅助识别此图",message,parent=self.win)
            return
        scope = f"{row['ms']/1000:.3f} 秒" if row else label
        prompt = "这张截图属于验证集。继续识别将为这张截图生成待复核草稿，请人工核对后再确认。" if allow_validation else None
        cfg = options(self.win,f"辅助识别 · {scope}",[
            ("weights","已有检测权重",self.default_weight(self.root),"file"),
            ("conf","置信度阈值","0.25",None)],prompt=prompt,action="继续识别" if allow_validation else "开始")
        if not cfg:
            return
        conf = float(cfg["conf"])
        if not 0<conf<1:
            raise ValueError("置信度需介于 0 和 1 之间")
        args = [sys.executable,"-u",str(BASE/"model_tasks.py"),"predict",
                str(self.root),cfg["weights"],"--confidence",str(conf),"--movie-id",str(movie["id"]),f"--batch-id={batch}"]
        if frame_id is not None:
            args.extend(("--frame-id",str(frame_id)))
        if allow_validation:
            args.append("--allow-validation")
        self.start(lambda:self.run_worker("predict",args))
        self.status.set(f"{scope} · 正在加载检测权重…")

    def start_training(self):
        if self.training or not self.ready():
            return
        from training_setup import training_setup
        current = self.movies.get(self.movie_picker.get())
        cfg = training_setup(self.win,dataset.movies(self.root),current["id"] if current else None,
                             self.default_weight(self.root),initial=self.movie_training_settings)
        if not cfg:
            return
        self.training_kind = cfg["kind"]
        if self.training_kind=="movie-temporal":
            self.movie_training_settings = cfg
            self.submit_reviewed()
        else:
            self.training_settings = (cfg["weights"],cfg["epochs"],cfg["device"])
            self.launch_training(*self.training_settings,kind="annotations")

    def show_training(self):
        if not self.root:
            self.status.set("打开工程后可查看训练记录")
            return
        if self.training_window and self.training_window.win.winfo_exists():
            self.activate_page("training",self.training_window.win)
            return
        self.training_window = TrainingWindow(self.win,self.root,lambda:self.training[3].name if self.training else None,
                                              self.training_status,self.feed_status,container=self.page_area)
        self.activate_page("training",self.training_window.win)

    def submit_reviewed(self):
        from film_learning import snapshot

        if self.training_kind=="movie-temporal":
            cfg = self.movie_training_settings or {"weights":"", "epochs":DEFAULT_MOVIE_EPOCHS,
                                                   "device":"", "sample_fps":1., "movie_ids":None}
            return self.submit_movie_config(cfg)
        reviewed = snapshot(self.root)
        if self.training:
            self.feed(reviewed=reviewed)
            return
        weights,epochs,device = self.training_settings or (None,DEFAULT_EPOCHS,"")
        weights = self.default_weight(self.root,fallback=weights)
        self.launch_training(weights,epochs,device,self.latest_completed(self.root,kind="annotations"),reviewed=reviewed,
                             kind="annotations")

    def launch_training(self, weights, epochs, device, previous=None, *, reviewed=None,kind="annotations",sample_fps=1.,
                        patience=5,learning_rate=.001,restart=False,continuous=False):
        from film_learning import new_run_path

        if self.storage_busy():
            raise ValueError("缓存管理正在执行，请完成后再启动训练")
        if kind=="movie-temporal" and restart:
            previous = None
        root = self.root
        run = new_run_path(root)
        job = {"status":"running","started":datetime.now().astimezone().isoformat(),"kind":kind,
               "epochs":epochs,"device":next((label for label,value in DEVICES.items() if value==device),device),
               "weights":Path(weights).name if weights else "未启用人物检测",
               "weights_path":str(Path(weights).resolve()) if weights and Path(weights).is_file() else ""}
        if kind=="movie-temporal":
            job.update(sample_fps=sample_fps,movie_count=len(reviewed["movies"]),movie_ids=[m["id"] for m in reviewed["movies"]],patience=patience,
                       learning_rate=learning_rate,restart=restart)
            job["continuous_learning"] = bool(continuous)
        save_job(root,run.name,job)
        logfile = root/"logs"/(run.name+"-train.log")
        self.training = (weights,epochs,device,run,previous)
        self.training_kind = kind
        self.continuous_training_run = run.name if continuous and kind=="movie-temporal" else None
        self.continuous_training_stop = False
        self.training_sample_fps = sample_fps
        self.training_movie_options = {"patience":patience,"learning_rate":learning_rate,"restart":False,"continuous":bool(continuous)}
        if kind=="movie-temporal" and self.movie_training_settings:
            self.movie_training_settings["restart"] = False
        self.training_stop.clear()
        self.training_status.set("正在准备电影学习训练…")
        self.training_dot.configure(style="running.Dot.TLabel")
        self.feed_status.set("在线视频已排队 · 截图训练结束后开始电影学习"
                             if self.pending_movie_learning else "")
        self.project_button.configure(state="disabled")
        self.train_button.configure(state="disabled")
        self.feed_button.configure(state="normal")
        self.training_cancel.configure(state="normal")
        task = "train-movie" if kind=="movie-temporal" else "train"
        args = [sys.executable,"-u",str(BASE/"model_tasks.py"),task,str(root),weights,
                "--epochs",str(epochs),"--run",str(run)]
        if kind=="movie-temporal":
            args.extend(("--sample-fps",str(sample_fps),"--patience",str(patience),"--learning-rate",str(learning_rate)))
            if restart:
                args.append("--restart")
        if device:
            args.extend(("--device",device))
        if previous:
            args.extend(("--previous",str(previous)))
        def work():
            try:
                with (TemporaryDirectory(prefix="yingge-training-") if reviewed is not None else nullcontext()) as directory:
                    if directory:
                        path = Path(directory)/"reviewed.json"
                        path.write_text(json.dumps(reviewed,ensure_ascii=False),encoding="utf-8")
                        args.extend(("--snapshot",str(path)))
                    result = self.run_worker("train",args,stop=self.training_stop,event="train_status",root=root,logfile=logfile)
            except Exception as exc:
                kind,result = "train_error",str(exc)
                state = "stopped" if self.training_stop.is_set() else "failed"
            else:
                kind = "train_done"
                state = "completed" if (run/"completed.json").is_file() else "skipped"
            job.update(status=state,finished=datetime.now().astimezone().isoformat(),message=result)
            try:
                save_job(root,run.name,job)
            except OSError as exc:
                result += f"\n训练记录保存失败 · {exc}"
            self.events.put((kind,result))
        threading.Thread(target=work,daemon=True).start()

    def feed(self, *, reviewed=None):
        if self.training and not self.training_stop.is_set():
            if self.training_kind=="movie-temporal" and reviewed is None:
                cfg = {**self.movie_defaults(),**(self.movie_training_settings or {})}
                return self.submit_movie_config(cfg,purpose="feed-latest")
            if self.training_kind=="movie-temporal":
                self.feed_movie_settings = dict(self.movie_training_settings or {})
            self.feed_pending = True
            self.feed_snapshot = reviewed
            self.feed_button.configure(state="disabled")
            self.feed_status.set("已排队 · 当前轮结束后学习本次资源" if self.training_kind=="movie-temporal" else
                                 "已排队 · 当前轮结束后训练本次标注" if reviewed is not None else
                                 "已排队 · 当前轮结束后读取已确认标注")

    def stop_training(self):
        continuous = getattr(self,"continuous_learning_manager",None)
        if continuous and continuous.active:
            self.stop_continuous_learning()
        self.continuous_training_stop = False
        self.training_cancel_version += 1
        # Persist cancellation of outstanding automatic handoffs so reopening
        # the project cannot resurrect a request the user stopped.
        manager = getattr(self,"online_manager",None)
        if manager is not None:
            ids = [row["id"] for row in manager.list_jobs()
                   if row.get("learning_kind",row.get("config",{}).get("learning_kind")) in {"story-reference","story-and-movie"}
                   and not row.get("learning_dispatched")]
            if ids:
                manager.cancel_learning(ids)
        automatic = getattr(self,"auto_learning_manager",None)
        if automatic is not None:
            automatic.pause()
        self.online_learning_batches.clear()
        self.profile_waiting = {}
        self.feed_movie_settings = None
        self.feed_pending = False
        self.feed_snapshot = None
        self.pending_movie_learning = None
        self.feed_status.set("")
        self.feed_button.configure(state="disabled")
        self.training_cancel.configure(state="disabled")
        self.training_stop.set()
        self.update_capture_more()
        self.training_status.set("正在停止训练…" if self.training else "学习请求已取消")

    def poll_training(self, kind, message):
        if kind=="train_status":
            if not self.training_stop.is_set():
                self.training_status.set(message)
            return
        weights,epochs,device,run,previous = self.training
        stopped = self.training_stop.is_set()
        continuous_stop = bool(getattr(self,"continuous_training_stop",False) and
                               run.name==getattr(self,"continuous_training_run",None))
        self.continuous_training_stop = False
        self.training = None
        self.training_status.set(message)
        self.training_dot.configure(style=f"{'idle' if stopped else 'ready' if kind=='train_done' else 'error'}.Dot.TLabel")
        pending,self.feed_pending = self.feed_pending,False
        reviewed,self.feed_snapshot = self.feed_snapshot,None
        next_cfg,self.feed_movie_settings = getattr(self,"feed_movie_settings",None),None
        self.feed_status.set("")
        resume_manual = continuous_stop and not (next_cfg or {}).get("_continuous")
        if pending and (not stopped or resume_manual) and not self.closing and (kind=="train_done" or reviewed is not None):
            checkpoint = run/"fit/weights/best.pt"
            completed = run/"completed.json"
            if completed.is_file():
                previous = completed
                if checkpoint.is_file():
                    weights = str(checkpoint)
            kwargs = {"sample_fps":self.training_sample_fps}
            if self.training_kind=="movie-temporal":
                kwargs.update(getattr(self,"training_movie_options",{}) or self.movie_options({}))
                if next_cfg:
                    weights,epochs,device = next_cfg.get("weights",weights),next_cfg.get("epochs",epochs),next_cfg.get("device",device)
                    kwargs.update(sample_fps=next_cfg.get("sample_fps",self.training_sample_fps),**self.movie_options(next_cfg))
            self.launch_training(weights,epochs,device,previous,reviewed=reviewed,kind=self.training_kind,**kwargs)
            return
        queued,self.pending_movie_learning = self.pending_movie_learning,None
        if queued and (not stopped or continuous_stop and not queued[0].get("_continuous")) and not self.closing:
            cfg,reviewed = queued
            self.launch_training(cfg["weights"],cfg["epochs"],cfg["device"],
                                 self.latest_completed(self.root,kind="movie-temporal"),reviewed=reviewed,
                                 kind="movie-temporal",sample_fps=cfg["sample_fps"],**self.movie_options(cfg))
            return
        self.project_button.configure(state="normal")
        self.train_button.configure(state="normal")
        self.feed_button.configure(state="disabled")
        self.training_cancel.configure(state="normal" if getattr(self,"profile_waiting",{}) else "disabled")
        self.update_capture_more()
        if kind=="train_error" and pending and not stopped:
            self.feed_status.set("已确认标注保留，可重新开始训练")

    def export(self):
        if not self.ready():
            return
        path = filedialog.asksaveasfilename(parent=self.win,title="导出电影学习素材包",
                defaultextension=".zip",filetypes=[("电影学习素材包","*.zip")])
        if path:
            def write():
                count = dataset.export_samples(self.root,path,self.progress)
                return f"已导出 {count} 张已确认画面及标注 · {path}"
            self.start(write)

    def run_worker(self,task,args,*,stop=None,event="status",root=None,logfile=None):
        root = root or self.root
        stop = stop or self.stop
        logs = root/"logs"
        logs.mkdir(exist_ok=True)
        logfile = logfile or logs/(datetime.now().strftime("%Y%m%d-%H%M%S-%f")+"-"+task+".log")
        env = dict(os.environ,PYTHONIOENCODING="utf-8",PYTHONUTF8="1")
        proc = subprocess.Popen(args,cwd=root,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                                encoding="utf-8",errors="replace",creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        last = ""
        def read():
            nonlocal last
            with proc.stdout, logfile.open("w",encoding="utf-8") as output:
                for line in proc.stdout:
                    output.write(line)
                    output.flush()
                    if line.startswith("AECPP "):
                        last = line[6:].strip()
                        self.events.put((event,last))
                    elif task!="train" and ("it/s" in line or "s/it" in line):
                        self.events.put((event,line.strip()[:145]))
        reader = threading.Thread(target=read)
        reader.start()
        while proc.poll() is None:
            if stop.wait(.2):
                proc.terminate()
                break
        proc.wait()
        reader.join()
        if stop.is_set():
            raise RuntimeError(f"{'训练' if task=='train' else '任务'}已停止 · 日志 {logfile}")
        if proc.returncode:
            raise RuntimeError(f"{last or '任务失败'}\n日志：{logfile}")
        return last or "任务完成"


    def close(self):
        if self.closing:
            return
        if self.story_busy():
            self.status.set("小说成片正在生成、学习或导出，请等待完成后再退出")
            if getattr(self,'story_window',None) and self.story_window.busy:self.activate_page("story",self.story_window.win)
            else:self.show_learning_dashboard()
            return
        if self.project_transfer:
            self.status.set("工程迁移正在执行，请等待完成后再退出")
            return
        if self.storage_busy():
            self.status.set("缓存管理正在执行，请等待完成后再退出")
            self.activate_page("cache",self.cache_window.win)
            return
        if self.training and not messagebox.askyesno("退出映格","训练仍在运行。要停止训练并退出吗？",parent=self.win):
            return
        for name in ("story_window","profile_window","cache_window","training_window"):
            window = getattr(self,name,None)
            if window and window.win.winfo_exists():
                if window.close() is False:
                    return
        self.closing = True
        if self.library_page:
            self.library_page.close()
            self.library_page = None
        self.profile_waiting = {}
        self.close_online_project()
        if self.training:
            self.stop_training()
        if self.busy:
            self.stop.set()
            self.status.set("正在停止任务，结束后将退出")
        if not self.busy and not self.training and not self.online_active():
            self.win.destroy()


if __name__=="__main__":
    win = tk.Tk()
    win.report_callback_exception = lambda cls,exc,tb:messagebox.showerror("操作未完成",str(exc),parent=win)
    studio = Studio(win)
    if len(sys.argv)>1:
        studio.use(Path(sys.argv[1]))
    else:
        default_project = Path.home()/"映格工程"/"默认工程"
        studio.use(default_project)
    win.mainloop()
