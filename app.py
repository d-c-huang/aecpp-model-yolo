"""Movie annotation and training desktop client. Run with Python 3.11+."""
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import dataset
from editor import BoxCanvas
from dialogue import edit_note, manage_voices
from media import MOVIE_TYPES

BASE = Path(__file__).resolve().parent
STATES = {"new":"未标注","draft":"人工草稿","predicted":"待复核","reviewed":"已确认"}
MODES = {"按片内时段":"time","整部训练":"train","整部验证":"val"}


def options(parent, title, fields):
    dlg = tk.Toplevel(parent)
    dlg.title(title)
    dlg.transient(parent)
    area = ttk.Frame(dlg,padding=16)
    area.pack(fill="both",expand=True)
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
    ttk.Button(area,text="开始",command=accept).grid(row=len(fields),column=1,sticky="e",pady=(12,0))
    dlg.bind("<Return>",lambda e: accept())
    dlg.bind("<Escape>",lambda e: dlg.destroy())
    dlg.grab_set()
    parent.wait_window(dlg)
    return result


class Studio:
    def __init__(self, win):
        self.win = win
        win.title("映格 · 电影学习素材标注")
        win.geometry("1220x760")
        win.minsize(920,560)
        self.root, self.row = None,None
        self.rows, self.names, self.history = [],[],[]
        self.voices = []
        self.busy = False
        self.after_success = None
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.status = tk.StringVar(value="选择一个文件夹新建或打开标注工程")
        self.title = tk.StringVar(value="电影标注工作台")
        self.filter = tk.StringVar(value="全部状态")
        self.category = tk.StringVar()
        self.selection_text = tk.StringVar(value="拖动鼠标绘制矩形框")
        self.detail = tk.StringVar(value="")
        bar = ttk.Frame(win,padding=(12,10))
        bar.pack(fill="x")
        ttk.Label(bar,textvariable=self.title,font=("Microsoft YaHei UI",13,"bold")).pack(side="left")
        for label,fn in [("打开工程",self.open_project),("导入电影",self.import_movie),
                         ("分析片段",lambda:self.import_movie(clip=True)),("角色确认",self.roles),
                         ("辅助识别",self.predict),("导出学习素材",self.export)]:
            ttk.Button(bar,text=label,command=fn).pack(side="left",padx=(8,0))
        sub_button = ttk.Menubutton(bar,text="字幕")
        sub_menu = tk.Menu(sub_button,tearoff=False)
        sub_menu.add_command(label="提取 / 更新电影字幕",command=self.read_subtitles)
        sub_menu.add_command(label="导出字幕为 SRT",command=self.write_subtitles)
        sub_button.configure(menu=sub_menu)
        sub_button.pack(side="left",padx=(8,0))
        self.cancel = ttk.Button(bar,text="停止任务",command=self.stop.set,state="disabled")
        self.cancel.pack(side="right")
        panes = ttk.Panedwindow(win,orient="horizontal")
        panes.pack(fill="both",expand=True,padx=10)
        left,main = ttk.Frame(panes),ttk.Frame(panes)
        panes.add(left,weight=0)
        panes.add(main,weight=1)
        ttk.Label(left,text="图片素材",font=("Microsoft YaHei UI",11,"bold")).pack(anchor="w",pady=5)
        filt = ttk.Combobox(left,textvariable=self.filter,values=["全部状态",*STATES.values()],state="readonly")
        filt.pack(fill="x",pady=(0,5))
        filt.bind("<<ComboboxSelected>>",lambda e:self.refresh())
        listing = ttk.Frame(left)
        listing.pack(fill="both",expand=True)
        self.tree = ttk.Treeview(listing,columns=("time","state"),show="headings",selectmode="browse",height=16)
        self.tree.heading("time",text="时间 / 分组")
        self.tree.heading("state",text="标注状态")
        self.tree.column("time",width=115,stretch=False)
        self.tree.column("state",width=75,stretch=False)
        scroll = ttk.Scrollbar(listing,command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right",fill="y")
        self.tree.pack(side="left",fill="both",expand=True)
        self.tree.bind("<<TreeviewSelect>>",lambda e:self.show_selected())
        ttk.Label(left,textvariable=self.detail,wraplength=198).pack(fill="x",pady=8)
        toolbar = ttk.Frame(main,padding=(6,3))
        toolbar.pack(fill="x")
        self.cat = ttk.Combobox(toolbar,textvariable=self.category,state="readonly",width=13)
        self.cat.pack(side="left")
        ttk.Button(toolbar,text="新增类别",command=self.new_class).pack(side="left",padx=3)
        ttk.Button(toolbar,text="人物名与台词",command=self.annotate).pack(side="left",padx=3)
        self.voice_button = ttk.Button(toolbar,text="画外台词 / 旁白",command=self.frame_dialogue)
        self.voice_button.pack(side="left",padx=3)
        ttk.Button(toolbar,text="更改框类别",command=self.reclass).pack(side="left")
        ttk.Button(toolbar,text="删除框",command=self.remove).pack(side="left",padx=3)
        ttk.Button(toolbar,text="撤销",command=self.undo).pack(side="left")
        self.canvas = BoxCanvas(main,self.cat_index,self.edited,self.select_box)
        self.canvas.bind("<Double-Button-1>",lambda e:self.annotate())
        self.canvas.pack(fill="both",expand=True,padx=(5,0))
        foot = ttk.Frame(main,padding=6)
        foot.pack(fill="x")
        ttk.Label(foot,textvariable=self.selection_text).pack(side="left")
        ttk.Button(foot,text="确认图片并继续  ↵",command=self.confirm).pack(side="right")
        ttk.Label(win,textvariable=self.status,anchor="w",padding=(12,7),wraplength=1050).pack(fill="x")
        win.bind("<Delete>",lambda e:self.remove())
        win.bind("<Control-z>",lambda e:self.undo())
        win.bind("<Return>",lambda e:self.confirm() if win.focus_get() in (self.canvas,self.tree) else None)
        win.protocol("WM_DELETE_WINDOW",self.close)
        win.after(100,self.poll)

    def ready(self):
        if self.busy:
            return False
        if not self.root:
            self.open_project()
        return self.root is not None

    def open_project(self):
        if self.busy:
            return
        path = filedialog.askdirectory(title="选择或新建工程文件夹")
        if path:
            self.use(Path(path))

    def use(self,path):
        dataset.initialize(path)
        self.root = path.resolve()
        self.title.set(self.root.name)
        self.filter.set("全部状态")
        self.names = dataset.classes(self.root)
        self.cat["values"] = self.names
        self.cat.current(0)
        self.row = None
        self.refresh()
        self.status.set(str(self.root))

    def refresh(self, prefer=None):
        if not self.root:
            return
        if prefer is None and self.row:
            prefer = self.row["id"]
        self.rows = dataset.frames(self.root)
        self.tree.delete(*self.tree.get_children())
        for r in self.rows:
            if self.filter.get() not in ("全部状态",STATES[r["state"]]):
                continue
            sec = r["ms"]/1000
            split = "训" if r["split"]=="train" else "验"
            self.tree.insert("", "end",iid=str(r["id"]),values=(f"{int(sec//60):02}:{sec%60:04.1f} · {split}",STATES[r["state"]]))
        ids = self.tree.get_children()
        self.row = None
        if ids:
            key = str(prefer) if str(prefer) in ids else ids[0]
            self.tree.selection_set(key)
            self.tree.see(key)
            self.show_selected()
        else:
            self.canvas.load(None,[],self.names)
            self.voices = []
            self.voice_button.configure(text="画外台词 / 旁白")
            self.detail.set(f"工程共 {len(self.rows)} 张图片")

    def show_selected(self):
        sel = self.tree.selection()
        if not sel or (self.row and str(self.row["id"]) == sel[0]):
            return
        self.row = next(r for r in self.rows if str(r["id"]) == sel[0])
        self.history = []
        r = self.row
        self.voices = json.loads(r["voices"])
        self.voice_button.configure(text=f"画外台词 / 旁白 ({len(self.voices)})")
        self.canvas.load(self.root/r["path"],json.loads(r["boxes"]),self.names)
        self.detail.set(f"{r['film']}\n{r['ms']/1000:.3f} 秒 · {'训练' if r['split']=='train' else '验证'}集\n工程共 {len(self.rows)} 张图片")
        self.select_box()

    def cat_index(self):
        return max(0,self.cat.current())

    def select_box(self):
        idx = self.canvas.selected
        if idx is None:
            self.selection_text.set("空白处拖动画框，框内拖动移动，拖角缩放；Shift 拖动可画重叠框")
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
        self.select_box()

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

    def confirm(self):
        if self.busy or not self.row:
            return
        r = self.row
        if not self.canvas.boxes and not self.voices and not messagebox.askyesno("确认无目标","将此图作为无目标的负样本加入数据集？",parent=self.win):
            return
        dataset.save_boxes(self.root,r["id"],self.canvas.boxes,"reviewed",voices=self.voices)
        ids = self.tree.get_children()
        idx = ids.index(str(r["id"]))
        self.refresh(int(ids[min(idx+1,len(ids)-1)]))
        self.status.set("图片已确认，可导出电影学习素材")

    def start(self,task,on_success=None):
        self.busy = True
        self.after_success = on_success
        self.stop.clear()
        self.canvas.enabled = False
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
        while not self.events.empty():
            kind,msg = self.events.get_nowait()
            self.status.set(msg)
            if kind in ("done","error"):
                self.busy = False
                self.canvas.enabled = True
                self.cancel.configure(state="disabled")
                self.refresh()
                if kind=="error":
                    messagebox.showinfo("任务消息",msg,parent=self.win)
                elif self.after_success:
                    self.after_success()
                self.after_success = None
        self.win.after(120,self.poll)

    def import_movie(self,clip=False):
        if self.busy:
            return
        path = filedialog.askopenfilename(parent=self.win,title="选择要跟踪的电影" if clip else "选择电影并随机抽帧",
                                          filetypes=MOVIE_TYPES)
        if not path:
            return
        destination = self.root or Path.home()/"映格工程"/"默认工程"
        if clip:
            fields = [("start","起点（秒）","0",None),("length","片段时长（秒）","12",None),
                      ("weights","人物检测权重",self.default_weight(destination),"file")]
        else:
            fields = [("n","期望新增图片数","40",None),("layer","采样层级","标准 · 1 秒",list(dataset.LAYERS)),
                      ("from","起点（秒）","0",None),("to","终点（留空到片尾）","",None),
                      ("mode","首次导入分组","按片内时段",list(MODES))]
        cfg = options(self.win,("分析连续片段" if clip else "随机抽帧")+" · "+Path(path).name,fields)
        if not cfg:
            return
        if not self.root:
            self.use(destination)
        if clip:
            args = [sys.executable,"-u",str(BASE/"model_tasks.py"),"track",str(self.root),cfg["weights"],
                    "--source",path,"--start",str(float(cfg["start"])),"--length",str(float(cfg["length"]))]
            self.start(lambda:self.run_worker("track",args))
            return
        n = int(cfg["n"])
        self.filter.set("全部状态")
        def extract():
            reports = []
            count = dataset.extract(self.root,path,n,MODES[cfg["mode"]],self.progress,
                                    step_ms=dataset.LAYERS[cfg["layer"]],start=float(cfg["from"] or 0),
                                    end=float(cfg["to"]) if cfg["to"] else None,subtitle_report=reports.append)
            return f"新增 {count}/{n} 张 · {Path(path).name} · {'；'.join(reports)}"
        previous = max((r["id"] for r in self.rows),default=0)
        def select_imported():
            new = next((r for r in self.rows if r["id"]>previous),None)
            if new:
                key = str(new["id"])
                if not self.tree.exists(key):
                    self.filter.set("全部状态")
                    self.refresh(new["id"])
                    return
                self.tree.selection_set(key)
                self.tree.see(key)
                self.show_selected()
        self.start(extract,select_imported)

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

    def default_weight(self,root):
        latest = sorted(p.parent/"fit/weights/best.pt" for p in root.glob("runs/*/completed.json")
                        if (p.parent/"fit/weights/best.pt").is_file())
        base = BASE/"weights"/"yolo11n.pt"
        return str(latest[-1]) if latest else (str(base) if base.exists() else "yolo11n.pt")

    def predict(self):
        if not self.ready():
            return
        cfg = options(self.win,"辅助识别未标注图片",[
            ("weights","已有检测权重",self.default_weight(self.root),"file"),
            ("conf","置信度阈值","0.25",None)])
        if not cfg:
            return
        conf = float(cfg["conf"])
        if not 0<conf<1:
            raise ValueError("置信度需介于 0 和 1 之间")
        args = [sys.executable,"-u",str(BASE/"model_tasks.py"),"predict",
                str(self.root),cfg["weights"],"--confidence",str(conf)]
        self.start(lambda:self.run_worker("predict",args))

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

    def run_worker(self,task,args):
        logs = self.root/"logs"
        logs.mkdir(exist_ok=True)
        logfile = logs/(datetime.now().strftime("%Y%m%d-%H%M%S-%f")+"-"+task+".log")
        env = dict(os.environ,PYTHONIOENCODING="utf-8",PYTHONUTF8="1")
        proc = subprocess.Popen(args,cwd=self.root,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
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
                        self.events.put(("status",last))
                    elif "it/s" in line or "s/it" in line:
                        self.events.put(("status",line.strip()[:145]))
        reader = threading.Thread(target=read)
        reader.start()
        while proc.poll() is None:
            if self.stop.wait(.2):
                proc.terminate()
                break
        proc.wait()
        reader.join()
        if self.stop.is_set():
            raise RuntimeError(f"任务已停止。日志：{logfile}")
        if proc.returncode:
            raise RuntimeError(f"{last or '任务失败'}\n日志：{logfile}")
        return last or "任务完成"


    def close(self):
        if self.busy:
            self.stop.set()
            self.status.set("正在停止任务，结束后可关闭窗口")
        else:
            self.win.destroy()


if __name__=="__main__":
    win = tk.Tk()
    win.report_callback_exception = lambda cls,exc,tb:messagebox.showerror("操作未完成",str(exc),parent=win)
    studio = Studio(win)
    if len(sys.argv)>1:
        studio.use(Path(sys.argv[1]))
    else:
        default_project = Path.home()/"映格工程"/"默认工程"
        if (default_project/"annotations.sqlite").is_file():
            studio.use(default_project)
    win.mainloop()
