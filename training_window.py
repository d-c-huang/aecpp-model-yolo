"""Inspect training runs and their on-disk results without interrupting the editor."""
import csv
from datetime import datetime
import json
import math
import os
from pathlib import Path
import queue
import re
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import theme


STATES = {"running":"训练中","completed":"已完成","skipped":"已跳过","failed":"失败","stopped":"已停止"}
METRICS = (("metrics/mAP50(B)","mAP50"),("metrics/mAP50-95(B)","mAP50–95"),
           ("metrics/precision(B)","精确率"),("metrics/recall(B)","召回率"))
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
VALIDATION_LABELS = {"independent-films":"独立电影验证", "within-film":"片内时段验证，尚未验证跨电影泛化",
                     "none":"无验证数据，仅训练集内部检查"}
FEATURE_LABELS = {
    "luma":"画面亮度", "contrast":"明暗对比", "saturation":"色彩饱和度",
    "red":"红色分量", "green":"绿色分量", "blue":"蓝色分量",
    "quadrant_luma_0":"左上区域亮度", "quadrant_luma_1":"右上区域亮度",
    "quadrant_luma_2":"左下区域亮度", "quadrant_luma_3":"右下区域亮度",
    "motion":"画面运动强度", "cut_score":"画面切换候选分数", "motion_x":"水平方向运动",
    "motion_y":"垂直方向运动", "edge_density":"画面边缘密度", "frame_interval":"采样时间间隔",
    "audio_rms":"声音平均能量", "audio_peak":"声音峰值", "audio_available":"声音可用状态",
    "subtitle_active":"字幕显示状态", "subtitle_chars":"字幕字符数量",
    "subtitle_available":"字幕可用状态", "subtitle_density":"同时显示的字幕数量",
    "confirmed_role_count":"已确认角色数量", "annotation_available":"人工标注可用状态",
    "people_count":"检测到的人物数量", "people_area":"人物画面占比",
    "people_x":"人物水平位置", "people_y":"人物垂直位置", "people_available":"人物检测可用状态",
    "script_role_count":"剧本匹配角色数量", "script_available":"剧本匹配可用状态",
}


def save_job(root, run_id, record):
    logs = root/"logs"
    logs.mkdir(exist_ok=True)
    path = logs/(run_id+"-train.json")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding="utf-8")
    tmp.replace(path)


def read_json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data,dict) else {}
    except (OSError,ValueError):
        return {}


def run_history(root, active, older_logs=None):
    records = {p.name.removesuffix("-train.json"):read_json(p) for p in (root/"logs").glob("*-train.json")}
    for marker in (root/"runs").glob("*/completed.json"):
        record = records.setdefault(marker.parent.name,{"started":datetime.fromtimestamp(marker.stat().st_mtime).isoformat()})
        for key,value in read_json(marker).items():
            record.setdefault(key,value)
        record["status"] = "completed"
    result = {}
    for key,record in sorted(records.items(),key=lambda item:item[1].get("started",""),reverse=True):
        log = (older_logs or {}).get(key,root/"logs"/(key+"-train.log"))
        result[key] = {**record,"run":root/"runs"/key,"log":log,
                       "state_label":("未结束" if record.get("status")=="running" and key!=active else
                                      STATES.get(record.get("status"),"状态未知"))}
    return result


def epoch_result(run):
    try:
        with (run/"fit/results.csv").open(encoding="utf-8",newline="") as stream:
            complete = {}
            for raw in csv.DictReader(stream):
                row = {key.strip():value.strip() for key,value in raw.items() if key and value is not None}
                if row.get("epoch","").isdigit() and all(row.get(key) for key,_ in METRICS):
                    complete = row
            return complete
    except (OSError,csv.Error):
        return {}


def metric_number(value):
    if finite_number(value):
        return f"{value:.5f}"
    return "—"


def finite_number(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


def improvement_text(model, baseline):
    """Compare like-for-like errors, including absent and zero baselines."""
    if not finite_number(model) or not finite_number(baseline) or baseline<0 or model<0:
        return "无可比基线","neutral"
    if baseline==0:
        return ("同为零","neutral") if model==0 else ("基线为零，误差增加","worse")
    change = (baseline-model)/baseline*100
    if abs(change)<.005:
        return "与基线持平","neutral"
    return (f"误差降低 {change:.2f}%","better") if change>0 else (f"误差增加 {-change:.2f}%","worse")


def measurement_change(row):
    if row.get("sample_count")==0:
        return "无有效样本","neutral"
    return improvement_text(row.get("model_loss"),row.get("baseline_loss"))


def loss_series(metrics):
    rows = metrics.get("epochs",[])
    if not isinstance(rows,list):
        return {"train":[],"val":[]}
    result = {"train":[],"val":[]}
    for row in rows:
        if not isinstance(row,dict) or not finite_number(row.get("epoch")):
            continue
        for key in result:
            value = row.get(key+"_loss")
            if finite_number(value) and value>=0:
                result[key].append((row["epoch"],value))
    return result


def movie_result(run, record, completed):
    """Build the temporal model view without interpreting loss as animation quality."""
    metrics = read_json(run/"movie_metrics.json")
    data = {**record,**completed,**metrics}
    epochs = metrics.get("epochs",[])
    latest = metrics.get("latest") or (epochs[-1] if isinstance(epochs,list) and epochs else {})
    if not isinstance(latest,dict):
        latest = {}
    count = completed.get("actual_epochs",latest.get("epoch",completed.get("epochs",0)))
    limit = metrics.get("total_epochs",record.get("epochs",count))
    config = f"电影时序 · {record['state_label']} · 最多 {limit} 轮 · {record.get('device') or '自动选设备'}"
    if data.get("sample_fps"):
        config += f" · {data['sample_fps']:g} 帧/秒"
    if data.get("patience"):
        config += f" · 连续 {data['patience']} 轮未改善即停止"
    samples = f"{data.get('movie_count',0)} 部电影"
    if "sample_count" in data:
        samples += f" · {data['sample_count']} 个时间点"
    if "train_windows" in data:
        samples += f" · {data['train_windows']} 个训练窗口 · {data.get('val_windows',0)} 个验证窗口"
    validation = VALIDATION_LABELS.get(data.get("validation_kind"),"正在准备训练数据")
    progress = f"{samples}\n{validation}"
    if data.get("profile_conditioned"):
        progress += f"\n已使用 {data.get('profile_count',0)} 部影片的确认资料 · 资料条件输入"
    if data.get("sampling_strategy")=="film-balanced":
        progress += "\n按影片均衡抽样，验证仍使用全部留出窗口"
    if data.get("early_stopped"):
        progress += f" · 已提前停止，共完成 {count}/{limit} 轮"
    if not completed:
        if record.get("status")=="running":
            progress += f" · 已完成 {count}/{limit} 轮" if count else " · 读取电影与准备样本"
        elif record.get("message"):
            progress += "\n"+record["message"].split("\n",1)[0]
    measured = []
    train_loss = completed.get("train_loss",latest.get("train_loss"))
    val_loss = completed.get("val_loss",latest.get("val_loss"))
    if completed.get("best_epoch")==0:
        origin = "上一版模型" if completed.get("best_origin")=="previous-model" else "训练前基线模型"
        measured.append(f"保留{origin}，本批训练未进一步改善")
    elif completed.get("best_epoch"):
        measured.append(f"导出模型取第 {completed['best_epoch']} 轮")
    if train_loss is not None:
        measured.append("训练损失 "+metric_number(train_loss))
    if val_loss is not None:
        measured.append("验证损失 "+metric_number(val_loss))
    baseline = data.get("baseline_loss")
    if baseline is not None and val_loss is not None:
        measured.append("保持上一时刻的验证基线 "+metric_number(baseline))
        measured.append(improvement_text(val_loss,baseline)[0])
    elif data.get("train_baseline_loss") is not None and data.get("validation_kind")=="none":
        measured.append("训练基线 "+metric_number(data["train_baseline_loss"]))
        measured.append("训练集"+improvement_text(train_loss,data["train_baseline_loss"])[0])
    if finite_number(latest.get("learning_rate")):
        measured.append(f"当前学习率 {latest['learning_rate']:.4g}")
    if measured:
        measured.append("损失越低越好，仅衡量特征预测误差")
    capability = "学习画面变化、声音强弱和字幕时序。当前模型不直接输出动画动作或完整分镜。"
    if data.get("profile_conditioned"):
        capability = "演员、角色和剧情通过词面特征参与条件预测，不代表已理解剧情。整片剧情作为已知先验，资料维度不计入预测误差。\n"+capability
    if data.get("restart"):
        capability = "本批按设置从头训练，历史模型保留。\n"+capability
    if data.get("warm_start_reason"):
        capability = str(data["warm_start_reason"])+"\n"+capability
    return {"summary":config,"progress":progress,"metrics":" · ".join(measured),
            "count":count,"limit":limit,
            "capability":capability}


class MovieEffectWindow:
    """Read reports and evaluate older models without touching Tk from workers."""
    def __init__(self,parent,run):
        self.run = Path(run)
        self.win,body = theme.sheet(parent,"学习效果",context=self.run.name)
        self.win.geometry("980x720")
        self.win.minsize(800,600)
        self.closed = threading.Event()
        self.events = queue.Queue()
        self.timer = None
        self.working = False
        self.chart_metrics = read_json(self.run/"movie_metrics.json")
        self.win.protocol("WM_DELETE_WINDOW",self.close)
        self.win.bind("<Destroy>",self._destroyed,add="+")
        self.headline = tk.StringVar(self.win,value="正在读取模型评估…")
        self.scope = tk.StringVar(self.win)
        self.status = tk.StringVar(self.win)
        top = ttk.Frame(body)
        top.pack(fill="x")
        ttk.Label(top,textvariable=self.headline,style="Section.TLabel",wraplength=700).pack(side="left")
        self.evaluate_button = ttk.Button(top,text="重新评估",command=self.evaluate)
        self.evaluate_button.pack(side="right")
        ttk.Label(body,textvariable=self.scope,style="Muted.TLabel",wraplength=900).pack(anchor="w",pady=(5,7))
        self.chart = tk.Canvas(body,height=145,background=theme.FIELD,highlightthickness=0)
        self.chart.pack(fill="x",pady=(0,10))
        self.chart.bind("<Configure>",lambda _:self.draw_chart())
        listing = ttk.Frame(body)
        listing.pack(fill="both",expand=True)
        self.table = ttk.Treeview(listing,columns=("model","baseline","samples","change"),show=("tree","headings"),height=11)
        self.table.heading("#0",text="观测项",anchor="w")
        self.table.column("#0",width=250,minwidth=190)
        for key,label,width in (("model","模型误差",105),("baseline","基线误差",105),
                                ("samples","有效观测",95),("change","相对基线",220)):
            self.table.heading(key,text=label,anchor="w")
            self.table.column(key,width=width,minwidth=95)
        self.table.tag_configure("better",foreground=theme.TEAL)
        self.table.tag_configure("worse",foreground=theme.RED)
        self.table.tag_configure("neutral",foreground=theme.MUTED)
        self.table.tag_configure("group",background=theme.RAISED)
        scrollbar = ttk.Scrollbar(listing,command=self.table.yview)
        self.table.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right",fill="y")
        self.table.pack(side="left",fill="both",expand=True)
        ttk.Label(body,text="误差衡量下一个时刻的数值观测预测。基线假设画面等特征保持上一时刻。"
                           "这些结果不表示已经理解动作、剧情或笑点。",
                  style="Caption.TLabel",wraplength=900).pack(anchor="w",pady=(9,4))
        self.bar = ttk.Progressbar(body,mode="indeterminate")
        self.bar.pack(fill="x",pady=(3,5))
        ttk.Label(body,textvariable=self.status,style="Muted.TLabel",wraplength=900).pack(anchor="w")
        existing = read_json(self.run/"movie_evaluation.json")
        if existing.get("kind")=="movie-temporal-evaluation":
            self.show_report(existing)
        else:
            self.evaluate()
        self.poll()

    def close(self):
        if self.closed.is_set():
            return
        self._stop()
        self.win.destroy()

    def _destroyed(self,event):
        if event.widget==self.win:
            self._stop()

    def _stop(self):
        self.closed.set()
        if self.timer is not None:
            try:
                self.win.after_cancel(self.timer)
            except tk.TclError:
                pass
            self.timer = None
        try:
            self.bar.stop()
        except tk.TclError:
            pass

    def evaluate(self):
        if self.working or self.closed.is_set():
            return
        self.working = True
        self.evaluate_button.configure(state="disabled")
        self.status.set("正在加载模型并检查学习效果…")
        self.bar.start(12)
        def progress(message):
            if self.closed.is_set():
                raise RuntimeError("评估窗口已关闭")
            self.events.put(("progress",str(message)))
        def work():
            try:
                from movie_sequence_model import evaluate_run
                result = evaluate_run(self.run,report=progress)
            except Exception as exc:
                self.events.put(("error",str(exc)))
            else:
                self.events.put(("done",result))
        threading.Thread(target=work,daemon=True).start()

    def poll(self):
        self.timer = None
        if self.closed.is_set():
            return
        while not self.events.empty():
            kind,value = self.events.get_nowait()
            if kind=="progress":
                self.status.set(value)
            else:
                self.working = False
                self.bar.stop()
                self.evaluate_button.configure(state="normal")
                if kind=="done":
                    self.show_report(value)
                else:
                    self.status.set("评估未完成 · "+value)
        self.timer = self.win.after(150,self.poll)

    def show_report(self,report):
        change,_ = improvement_text(report.get("model_loss"),report.get("baseline_loss"))
        scope = "验证集" if report.get("split")=="val" else "训练集内部检查"
        self.headline.set(f"{scope} · {change}")
        self.scope.set(VALIDATION_LABELS.get(report.get("validation_kind"),"验证范围未知")+
                       f" · {report.get('sample_count',0)} 个评估窗口")
        self.table.delete(*self.table.get_children())
        groups = {}
        for index,row in enumerate(report.get("groups",[])):
            key = f"group-{index}"
            groups[row.get("name")] = key
            self.insert_measurement("",key,row.get("label",row.get("name","其他观测")),row,group=True)
        for index,row in enumerate(report.get("features",[])):
            name = row.get("name","")
            parent = groups.get(row.get("group"),"")
            self.insert_measurement(parent,f"feature-{index}",FEATURE_LABELS.get(name,name),row)
        warnings = report.get("warnings",[])
        text = " · ".join(str(item) for item in warnings)
        self.status.set(text or "评估完成，报告已保存到本批次训练目录")
        if report.get("finite_predictions") is False:
            self.status.set("模型预测含无效数值，请检查训练记录。"+text)
        self.draw_chart()

    def insert_measurement(self,parent,key,label,row,*,group=False):
        change,tag = measurement_change(row)
        self.table.insert(parent,"end",iid=key,text=label,open=True,
                          values=(metric_number(row.get("model_loss")),metric_number(row.get("baseline_loss")),
                                  row.get("sample_count","—"),change),
                          tags=(tag,"group") if group else (tag,))

    def draw_chart(self):
        canvas = self.chart
        canvas.delete("all")
        width,height = max(300,canvas.winfo_width()),max(100,canvas.winfo_height())
        series = loss_series(self.chart_metrics)
        points = series["train"]+series["val"]
        if not points:
            canvas.create_text(width/2,height/2,text="暂无逐轮损失曲线",fill=theme.MUTED)
            return
        left,right,top,bottom = 57,width-18,25,height-27
        maximum = max(value for _,value in points) or 1
        last = max(epoch for epoch,_ in points) or 1
        for step in range(3):
            value = maximum*step/2
            y = bottom-(bottom-top)*step/2
            canvas.create_line(left,y,right,y,fill=theme.BORDER)
            canvas.create_text(left-8,y,text=f"{value:.3f}",anchor="e",fill=theme.SOFT)
        for name,label,color,x in (("train","训练",theme.ACCENT,80),("val","验证",theme.TEAL,160)):
            canvas.create_line(x,12,x+18,12,fill=color,width=2)
            canvas.create_text(x+24,12,text=label,anchor="w",fill=theme.MUTED)
            coords = [(left+(right-left)*(epoch-1)/max(1,last-1),bottom-(bottom-top)*value/maximum)
                      for epoch,value in series[name]]
            if len(coords)>1:
                canvas.create_line(*[value for pair in coords for value in pair],fill=color,width=2)
            for x,y in coords:
                if len(coords)<=50:
                    canvas.create_oval(x-2,y-2,x+2,y+2,fill=color,outline="")
        canvas.create_text(left,bottom+15,text="第 1 轮",anchor="w",fill=theme.SOFT)
        canvas.create_text(right,bottom+15,text=f"第 {last:g} 轮",anchor="e",fill=theme.SOFT)


def log_tail(path):
    try:
        with path.open("rb") as stream:
            size = stream.seek(0,2)
            stream.seek(max(0,size-24000))
            content = stream.read().decode("utf-8","replace")
    except OSError:
        return ""
    if size>24000:
        content = content.partition("\n")[2]
    return ANSI.sub("",content)


def legacy_logs(root):
    logs = {}
    for log in (root/"logs").glob("*-train.log"):
        if log.with_suffix(".json").exists():
            continue
        for line in reversed(log_tail(log).splitlines()):
            if line.startswith("AECPP 训练完成 · "):
                run_id = Path(line.rpartition(" · ")[2]).name
                if (root/"runs"/run_id/"completed.json").is_file():
                    logs[run_id] = log
                break
    return logs


class TrainingWindow:
    def __init__(self,parent,root,active,status,queued,container=None):
        self.root,self.active,self.status = root,active,status
        self.older_logs = legacy_logs(root)
        self.embedded = container is not None
        self._closed = False
        if self.embedded:
            self.win = ttk.Frame(container)
            body = ttk.Frame(self.win,padding=(20,16))
            body.pack(fill="both",expand=True)
            header = ttk.Frame(body)
            header.pack(fill="x")
            ttk.Label(header,text="训练中心",style="Title.TLabel").pack(side="left")
            ttk.Label(header,text=root.name,style="Muted.TLabel").pack(side="right")
            ttk.Separator(body).pack(fill="x",pady=(12,14))
        else:
            self.win,body = theme.sheet(parent,"训练记录",context=root.name)
            self.win.minsize(750,480)
            self.win.geometry("980x620")
            self.win.protocol("WM_DELETE_WINDOW",self.close)
        self.body = body
        self.win.bind("<Destroy>",self._destroyed,add="+")
        self.previous_active = None
        self.records = {}
        self.log_text = ""
        self.timer = None
        self.exports = queue.Queue()
        self.exporting = False
        self.effect_windows = {}

        head = ttk.Frame(body)
        head.pack(fill="x",pady=(0,10))
        head.columnconfigure(0,weight=1)
        self.live_message = tk.StringVar(self.win)
        live = ttk.Label(head,textvariable=self.live_message,style="Muted.TLabel",wraplength=620)
        live.grid(row=0,column=0,sticky="w")
        ttk.Label(head,textvariable=queued,style="Accent.TLabel",wraplength=200).grid(row=0,column=1,sticky="e",padx=(12,0))
        head.bind("<Configure>",lambda event:live.configure(wraplength=max(200,event.width-220)),add="+")
        panels = ttk.Panedwindow(body,orient="horizontal")
        panels.pack(fill="both",expand=True)
        left = ttk.Frame(panels)
        detail = ttk.Frame(panels,padding=(10,0,0,0))
        panels.add(left,weight=0)
        panels.add(detail,weight=1)
        ttk.Label(left,text="训练批次",style="Section.TLabel").pack(anchor="w",pady=(0,7))
        wheel = ttk.Scrollbar(left,orient="vertical")
        self.history = ttk.Treeview(left,columns=("time","kind","status"),show="headings",selectmode="browse",
                                    yscrollcommand=wheel.set,height=8)
        for column,label,width in (("time","启动时间",120),("kind","任务",70),("status","状态",60)):
            self.history.heading(column,text=label,anchor="w")
            self.history.column(column,width=width,stretch=False)
        wheel.configure(command=self.history.yview)
        wheel.pack(side="right",fill="y")
        self.history.pack(fill="both",expand=True)
        self.history.bind("<<TreeviewSelect>>",lambda _:self.show_run())

        self.summary = tk.StringVar(self.win)
        self.progress_text = tk.StringVar(self.win)
        self.metrics = tk.StringVar(self.win)
        self.capability = tk.StringVar(self.win)
        detail.columnconfigure(0,weight=1)
        detail.rowconfigure(4,weight=1)
        summary = ttk.Label(detail,textvariable=self.summary,style="Section.TLabel",wraplength=620)
        summary.grid(row=0,column=0,sticky="ew",pady=(0,6))
        detail.bind("<Configure>",lambda event:summary.configure(wraplength=max(240,event.width-12)),add="+")
        # Keep rich metrics readable without pushing the actions below the page.
        overview = ttk.Frame(detail)
        overview.grid(row=1,column=0,sticky="ew")
        overview.columnconfigure(0,weight=1)
        self.overview = tk.Text(overview,width=1,height=7,state="disabled",wrap="word",font=(theme.FONT,9),
                                background=theme.PANEL,foreground=theme.MUTED,relief="flat",borderwidth=0,
                                highlightthickness=0,takefocus=False)
        self.overview.grid(row=0,column=0,sticky="ew")
        overview_scroll = ttk.Scrollbar(overview,orient="vertical",command=self.overview.yview)
        overview_scroll.grid(row=0,column=1,sticky="ns")
        self.overview.configure(yscrollcommand=overview_scroll.set)
        self.overview.tag_configure("metrics",foreground=theme.ACCENT)
        self.overview.tag_configure("capability",foreground=theme.SOFT)
        for variable in (self.progress_text,self.metrics,self.capability):
            variable.trace_add("write",self._update_overview)
        self.progress = ttk.Progressbar(detail,mode="determinate")
        self.progress.grid(row=2,column=0,sticky="ew",pady=(6,9))
        ttk.Label(detail,text="训练日志",style="Section.TLabel").grid(row=3,column=0,sticky="w",pady=(3,6))
        log_panel = ttk.Frame(detail)
        log_panel.grid(row=4,column=0,sticky="nsew")
        self.log = tk.Text(log_panel,width=48,height=10,state="disabled",wrap="word",font=("Consolas",10))
        scroll = ttk.Scrollbar(log_panel,command=self.log.yview,orient="vertical")
        scroll.pack(side="right",fill="y")
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(fill="both",expand=True)
        actions = ttk.Frame(detail)
        actions.grid(row=5,column=0,sticky="ew",pady=(9,0))
        self.result_button = ttk.Button(actions,text="打开训练产物",command=lambda:self.open_path("run"))
        self.result_button.pack(side="left")
        self.log_button = ttk.Button(actions,text="打开原始日志",command=lambda:self.open_path("log"))
        self.log_button.pack(side="left",padx=7)
        self.export_button = ttk.Button(actions,text="导出电影模型",command=self.export_model,state="disabled")
        self.export_button.pack(side="left")
        self.effect_button = ttk.Button(detail,text="查看学习效果",command=self.show_effect,state="disabled",
                                       style="Accent.TButton")
        self.effect_button.grid(row=6,column=0,sticky="w",pady=(8,0))
        self.refresh()

    def _update_overview(self,*_):
        if self._closed:
            return
        top = self.overview.yview()[0]
        self.overview.configure(state="normal")
        self.overview.delete("1.0","end")
        for variable,tag in ((self.progress_text,"progress"),(self.metrics,"metrics"),(self.capability,"capability")):
            value = variable.get()
            if value:
                self.overview.insert("end",value+"\n",tag)
        self.overview.configure(state="disabled")
        self.overview.yview_moveto(top)

    def _cancel_timer(self):
        if self.timer is not None:
            try:
                self.win.after_cancel(self.timer)
            except tk.TclError:
                pass
            self.timer = None

    def _destroyed(self,event):
        if event.widget==self.win:
            self._closed = True
            self._cancel_timer()
            for window in self.effect_windows.values():
                window.close()

    def show(self):
        """Bring an existing page forward; the caller maps embedded frames."""
        if not self._closed:
            if not self.embedded:
                self.win.deiconify()
            self.win.lift()

    def close(self):
        if self._closed:
            return True
        self._closed = True
        self._cancel_timer()
        for window in self.effect_windows.values():
            window.close()
        self.win.destroy()
        return True

    def refresh(self):
        self._cancel_timer()
        if self._closed:
            return
        while not self.exports.empty():
            succeeded,message = self.exports.get_nowait()
            self.exporting = False
            self.export_button.configure(text="导出电影模型")
            if succeeded:
                messagebox.showinfo("电影模型已导出",message,parent=self.win)
            else:
                messagebox.showerror("导出电影模型",message,parent=self.win)
        active = self.active()
        message = self.status.get().split("\n",1)[0]
        if message.startswith("训练完成 · "):
            message = message.rpartition(" · ")[0]
        self.live_message.set(message.partition(" · 日志 ")[0])
        records = run_history(self.root,active,self.older_logs)
        if records!=self.records:
            selected = self.history.selection()
            self.history.delete(*self.history.get_children())
            for key,record in records.items():
                stamp = record.get("started","").replace("T"," ")[5:19]
                label = "电影时序" if record.get("kind")=="movie-temporal" else "截图标注"
                self.history.insert("","end",iid=key,values=(stamp,label,record["state_label"]))
            self.records = records
            prefer = active if active in records and (not selected or selected[0]==self.previous_active) else None
            choice = prefer or (selected[0] if selected and selected[0] in records else next(iter(records),None))
            if choice:
                self.history.selection_set(choice)
                self.history.see(choice)
        self.previous_active = active
        self.show_run()
        self.timer = self.win.after(800,self.refresh)

    def selected(self):
        selected = self.history.selection()
        return self.records.get(selected[0]) if selected else None

    def show_run(self):
        record = self.selected()
        self.result_button.configure(state="normal" if record and record["run"].is_dir() else "disabled")
        self.log_button.configure(state="normal" if record and record["log"].is_file() else "disabled")
        self.export_button.configure(state="disabled")
        self.effect_button.configure(state="disabled")
        if not record:
            self.summary.set("暂无训练记录")
            self.progress_text.set("")
            self.progress.configure(value=0,maximum=1)
            self.metrics.set("")
            self.capability.set("")
            self.set_log("")
            return
        run = record["run"]
        completed = read_json(run/"completed.json")
        if record.get("kind",completed.get("kind"))=="movie-temporal":
            view = movie_result(run,record,completed)
            self.summary.set(view["summary"])
            self.progress_text.set(view["progress"])
            self.metrics.set(view["metrics"])
            self.capability.set(view["capability"])
            maximum = view["count"] if completed.get("early_stopped") else view["limit"]
            self.progress.configure(maximum=max(1,maximum),value=view["count"])
            if completed and (run/"movie_temporal.pt").is_file() and not self.exporting:
                self.export_button.configure(state="normal")
            if completed and (run/"movie_temporal.pt").is_file():
                self.effect_button.configure(state="normal")
            self.set_log(log_tail(record["log"]) or record.get("message",""))
            return
        self.capability.set("")
        epoch = epoch_result(run)
        count = int(epoch.get("epoch",0))
        limit = record.get("epochs",0)
        config = f" · {limit} 轮 · {record.get('device') or '自动选设备'}  /  {record.get('weights','')}" if limit else ""
        self.summary.set(record["state_label"]+config)
        self.progress.configure(maximum=max(1,limit or count),value=count or int(bool(completed) and not limit))
        measured = " · ".join(f"{name} {epoch[key]}" for key,name in METRICS) if epoch else ""
        if completed:
            checked = ("独立验证集" if completed.get("detector_holdout") else "训练集内部检查") if measured else ""
            samples = (f"{completed.get('train_images',0)} 张训练图 · {completed.get('val_images',0)} 张验证图 · "
                       f"{completed.get('context_entries',0)} 条人物与台词索引")
            self.progress_text.set(f"{samples}  /  {checked}" if checked else samples)
            if not measured:
                self.progress.configure(maximum=1,value=1)
        else:
            text = f"已完成 {count}/{limit} 轮" if limit else ""
            self.progress_text.set(text if record.get("status")=="running" else record.get("message",text))
        self.metrics.set(measured)
        self.set_log(log_tail(record["log"]) or record.get("message",""))

    def set_log(self,text):
        if text==self.log_text:
            return
        bottom = not self.log_text or self.log.yview()[1]>=.995
        top = self.log.yview()[0]
        self.log_text = text
        self.log.configure(state="normal")
        self.log.delete("1.0","end")
        self.log.insert("1.0",text)
        self.log.configure(state="disabled")
        self.log.yview_moveto(1 if bottom else top)

    def open_path(self, key):
        record = self.selected()
        if record:
            try:
                os.startfile(record[key])
            except OSError as exc:
                messagebox.showerror("打开训练文件",str(exc),parent=self.win)

    def show_effect(self):
        record = self.selected()
        if not record:
            return
        run = record["run"]
        if read_json(run/"completed.json").get("kind")!="movie-temporal":
            return
        window = self.effect_windows.get(run)
        if window and not window.closed.is_set() and window.win.winfo_exists():
            window.win.lift()
        else:
            self.effect_windows[run] = MovieEffectWindow(self.win,run)

    def export_model(self):
        record = self.selected()
        if not record or self.exporting:
            return
        run = record["run"]
        if read_json(run/"completed.json").get("kind")!="movie-temporal":
            return
        target = filedialog.asksaveasfilename(parent=self.win,title="导出电影时序模型",
                initialfile=f"电影时序模型-{run.name}.zip",defaultextension=".zip",
                filetypes=[("电影时序模型包","*.zip")])
        if not target:
            return
        self.exporting = True
        self.export_button.configure(state="disabled",text="正在导出…")
        def work():
            try:
                from movie_sequence_model import export_bundle
                export_bundle(run,Path(target))
            except Exception as exc:
                self.exports.put((False,str(exc)))
            else:
                self.exports.put((True,str(target)))
        threading.Thread(target=work,daemon=True).start()
