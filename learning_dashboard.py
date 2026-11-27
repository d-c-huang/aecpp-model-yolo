"""Unified learning monitor; all database reads run off the Tk thread."""
from datetime import datetime,timedelta,timezone
import hashlib
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk
import warnings

from PIL import Image,ImageTk
import theme
from learning_telemetry import dashboard_snapshot
from online_window import continuous_source_url

METRICS={'字幕节奏参考（条）':'subtitle_reference_count','视觉片段（段）':'visual_clip_count',
         '证据画面（帧）':'visual_frame_count','已学习视频（部）':'video_count','跳过视频（个）':'skipped_count'}
LABELS={'idle':'站立','talk':'说话','walk':'走路','run':'奔跑','jump':'跳跃','fall':'摔倒','shake':'抖动',
        'neutral':'平静','happy':'开心','angry':'生气','sad':'伤心','shocked':'震惊',
        'wide':'全景','medium':'中景','close':'近景','unknown':'未知'}
STAGES=('准备','查找','下载','转写','内容','画面','入库')


def media_time(ms):
    seconds=max(0,float(ms)/1000)
    return f'{int(seconds)//60:02d}:{seconds%60:04.1f}'


def phase_stage(phase):
    phase=str(phase or '')
    if phase in {'preparing','pulling','downloading-model','load_model','checking'}:return '准备'
    if phase in {'searching','query','fetch','parse'}:return '查找'
    if phase in {'downloading','resolving','merging','probing','importing'}:return '下载'
    if phase in {'transcribing','transcribe','extract_audio','transcript-ready'}:return '转写'
    if phase in {'understanding','analysis','model-request','saving-analysis','analysing','snapshot'}:return '内容'
    if phase.startswith(('visual','shot-rhythm')):return '画面'
    if phase in {'learning','training','publishing'}:return '入库'
    return ''


class LearningDashboard:
    def __init__(self,parent,root,*,runtime=lambda:{},on_start=None,on_stop=None,on_jobs=None,on_sources=None,on_close=None,
                 loader=dashboard_snapshot,assembly_task=None,can_write=lambda:True):
        self.root=Path(root).resolve();self.runtime=runtime;self.loader=loader
        self.on_start,self.on_stop,self.on_close=on_start,on_stop,on_close
        self._closed=False;self._worker=None;self._events=queue.Queue();self._timers=set()
        self.data={};self.photos=[];self._images=[];self.frame_index=0;self._signature=None;self._movie_ids={};self._last_refresh=0
        self._animation_count=0;self.playing=True;self._blink=False
        self.win=tk.Toplevel(parent);self.win.title('学习可视化仪表盘');self.win.geometry('1120x780');self.win.minsize(900,640)
        self.win.protocol('WM_DELETE_WINDOW',self.close);self.win.bind('<Destroy>',self._destroyed,add='+')
        self.follow=tk.BooleanVar(self.win,value=True);self.movie=tk.StringVar(self.win)
        self.source=tk.StringVar(self.win,value='尚无正在获取的来源')
        self.status=tk.StringVar(self.win,value='正在读取实际学习状态…')
        self.totals=tk.StringVar(self.win,value='累计成果：待统计');self.skip=tk.StringVar(self.win)
        self.observation=tk.StringVar(self.win,value='动作、表情、景别：等待实际分析')
        self.previous=tk.StringVar(self.win);self.frame_label=tk.StringVar(self.win,value='暂无连续帧')
        self.evidence=tk.StringVar(self.win)
        self.efficiency=tk.StringVar(self.win)
        self._evidence_selection=None
        if assembly_task is None:
            from story_assembly import AssemblyTask
            assembly_task=AssemblyTask(self.root);assembly_task.restore()
        self.assembly_task=assembly_task;self.can_write=can_write
        self._build(on_jobs,on_sources)
        self._poll();self._animate()

    def _after(self,delay,fn):
        token=None
        def call():
            self._timers.discard(token)
            if not self._closed:fn()
        token=self.win.after(delay,call);self._timers.add(token)

    def _build(self,on_jobs,on_sources):
        body=ttk.Frame(self.win,padding=(14,10));body.pack(fill='both',expand=True);body.columnconfigure(0,weight=1);body.rowconfigure(4,weight=1)
        head=ttk.Frame(body);head.grid(row=0,column=0,sticky='ew')
        ttk.Label(head,text='学习可视化仪表盘',style='Title.TLabel').pack(side='left')
        for title,fn in [('来源详情',on_sources),('学习任务',on_jobs)]:
            if fn:ttk.Button(head,text=title,command=fn,style='Compact.TButton').pack(side='right',padx=4)
        self.stop_button=ttk.Button(head,text='停止自动学习',command=lambda:self._action(self.on_stop));self.stop_button.pack(side='right',padx=4)
        self.start_button=ttk.Button(head,text='一键启动',command=lambda:self._action(self.on_start),style='Primary.TButton');self.start_button.pack(side='right',padx=4)
        info=ttk.Frame(body);info.grid(row=1,column=0,sticky='ew',pady=(8,4));info.columnconfigure(1,weight=1)
        ttk.Label(info,text='当前来源').grid(row=0,column=0,padx=(0,8))
        ttk.Entry(info,textvariable=self.source,state='readonly').grid(row=0,column=1,sticky='ew')
        ttk.Label(info,textvariable=self.status,style='Muted.TLabel',wraplength=1020).grid(row=1,column=0,columnspan=2,sticky='w',pady=(5,0))
        ttk.Label(info,textvariable=self.efficiency,style='Caption.TLabel',wraplength=1020).grid(row=2,column=0,columnspan=2,sticky='w')
        self.stage_canvas=tk.Canvas(body,height=36,bg=theme.PANEL,highlightthickness=0)
        self.stage_canvas.grid(row=2,column=0,sticky='ew');self.stage_canvas.bind('<Configure>',lambda _:self._draw_stages())
        ttk.Label(body,textvariable=self.totals,style='Accent.TLabel',wraplength=1050).grid(row=3,column=0,sticky='w',pady=(3,6))
        self.tabs=ttk.Notebook(body);self.tabs.grid(row=4,column=0,sticky='nsew')
        self.process_page=ttk.Frame(self.tabs);self.tabs.add(self.process_page,text='学习过程与成果')
        panes=ttk.Panedwindow(self.process_page,orient='horizontal');panes.pack(fill='both',expand=True)
        left=ttk.Frame(panes,width=310);left.columnconfigure(0,weight=1);left.rowconfigure(1,weight=1);left.rowconfigure(3,weight=1)
        right=ttk.Frame(panes,width=690);right.columnconfigure(0,weight=1);right.rowconfigure(1,weight=1)
        panes.add(left,weight=1);panes.add(right,weight=2)
        self.chart_vars=[];self.charts=[]
        for index,title in enumerate(('字幕节奏参考（条）','视觉片段（段）')):
            var=tk.StringVar(self.win,value=title);self.chart_vars.append(var)
            box=ttk.Combobox(left,textvariable=var,values=list(METRICS),state='readonly',width=23)
            box.grid(row=index*2,column=0,sticky='ew',pady=(2,4));box.bind('<<ComboboxSelected>>',lambda _:self._draw_charts())
            canvas=tk.Canvas(left,height=145,bg=theme.FIELD,highlightthickness=0);self.charts.append(canvas)
            canvas.grid(row=index*2+1,column=0,sticky='nsew',padx=(0,8),pady=(0,8));canvas.bind('<Configure>',lambda _:self._draw_charts())
        ttk.Label(left,text='真实累计快照；首次记录前不回填。删除或更换来源后，计数可回落。',
                  style='Caption.TLabel',wraplength=300).grid(row=4,column=0,sticky='w')
        toolbar=ttk.Frame(right);toolbar.grid(row=0,column=0,sticky='ew');toolbar.columnconfigure(0,weight=1)
        self.movie_box=ttk.Combobox(toolbar,textvariable=self.movie,state='readonly',width=26)
        self.movie_box.grid(row=0,column=0,sticky='ew');self.movie_box.bind('<<ComboboxSelected>>',self._select_movie)
        ttk.Checkbutton(toolbar,text='跟随当前任务',variable=self.follow,command=self._refresh_now).grid(row=0,column=1,padx=6)
        self.play_button=ttk.Button(toolbar,text='暂停帧播放',command=self._toggle_play,style='Compact.TButton');self.play_button.grid(row=0,column=2)
        self.preview=tk.Canvas(right,height=245,bg=theme.MONITOR,highlightthickness=0)
        self.preview.grid(row=1,column=0,sticky='nsew',pady=(6,3));self.preview.bind('<Configure>',lambda _:self._draw_frame())
        ttk.Label(right,textvariable=self.frame_label,style='Caption.TLabel',wraplength=660).grid(row=2,column=0,sticky='w')
        ttk.Label(right,textvariable=self.observation,style='Accent.TLabel',wraplength=660).grid(row=3,column=0,sticky='w',pady=(5,2))
        ttk.Label(right,textvariable=self.previous,style='Muted.TLabel',wraplength=660).grid(row=4,column=0,sticky='w')
        ttk.Label(right,textvariable=self.evidence,style='Muted.TLabel',wraplength=650).grid(row=5,column=0,sticky='w',pady=(3,0))
        self.rhythm_canvas=tk.Canvas(right,height=65,bg=theme.FIELD,highlightthickness=0)
        self.rhythm_canvas.grid(row=6,column=0,sticky='ew',pady=(6,2));self.rhythm_canvas.bind('<Configure>',lambda _:self._draw_rhythm())
        ttk.Label(right,text='循环播放真实抽样连续帧，可暂停或选择历史任务。模型观察待审核。',
                  style='Caption.TLabel',wraplength=660).grid(row=7,column=0,sticky='w')
        ttk.Label(body,textvariable=self.skip,style='Muted.TLabel',wraplength=1030).grid(row=5,column=0,sticky='w',pady=(7,0))
        from learning_assembly_panel import LearningAssemblyPanel
        self.assembly=LearningAssemblyPanel(self.tabs,self.assembly_task,on_evidence=self._show_assembly_evidence,can_write=self.can_write)
        self.tabs.add(self.assembly.win,text='素材匹配与组合')

    def _action(self,fn):
        if fn:
            try:fn()
            except Exception as exc:self.status.set(str(exc))
        self._refresh_now()

    def _refresh_now(self):self._last_refresh=0

    def _select_movie(self,event=None):
        self._evidence_selection=None;self.follow.set(False);self._refresh_now()

    def _show_assembly_evidence(self,reference):
        self.follow.set(False);self._evidence_selection=dict(reference)
        self.movie.set(next((name for name,value in self._movie_ids.items() if value==reference['movie_id']),''))
        self.tabs.select(self.process_page);self._refresh_now()

    def _poll(self):
        try:
            runtime=self.runtime() or {}
            self.start_button.configure(state='disabled' if runtime.get('active') or not self.on_start else 'normal')
            self.stop_button.configure(state='normal' if runtime.get('active') and self.on_stop else 'disabled')
            self._runtime=runtime
            if time.monotonic()-self._last_refresh>=1 and (not self._worker or not self._worker.is_alive()):
                selected=None if self.follow.get() else self._evidence_selection or self._movie_ids.get(self.movie.get())
                self._last_refresh=time.monotonic()
                def work():
                    try:self._events.put(('data',self.loader(self.root,selected)))
                    except Exception as exc:self._events.put(('error',str(exc)))
                self._worker=threading.Thread(target=work,daemon=True,name='learning-dashboard-reader');self._worker.start()
            while True:
                try:kind,value=self._events.get_nowait()
                except queue.Empty:break
                if kind=='error':self.status.set('状态暂未刷新：'+value)
                else:self._apply(value)
        except Exception as exc:self.status.set('仪表盘暂未刷新：'+str(exc))
        self._after(120,self._poll)

    def _apply(self,data):
        self.data=data
        totals=data['totals'];job=data.get('learning');download=data.get('download');chosen=data.get('job') or {}
        runtime=getattr(self,'_runtime',{})
        source=(data.get('source') if job else (download or {}).get('url')) or runtime.get('current_url') or data.get('source') or ''
        self.source.set(continuous_source_url(source) or source or '暂无来源，启动学习后自动更新')
        parts=[]
        if download:parts.append('下载：'+str(download.get('title') or '')+' · '+str(download.get('message') or '进行中'))
        if job:parts.append('学习：'+job['title']+' · '+job.get('message',''))
        if not parts:parts.append(runtime.get('message') or ('当前没有运行任务，可查看已保存成果' if chosen else '尚未开始学习'))
        if data.get('warning'):parts.append(data['warning'])
        self.status.set('\n'.join(parts)[:1000])
        perf=(chosen.get('model_analysis') or {}).get('performance') or {}
        live=(chosen.get('live_visual') or {}).get('performance') if job else None
        visual_perf=live or perf.get('visual') or {}
        fields=[]
        if live:fields.append('当前视觉进度')
        elif perf:fields.append(('上次分析' if job else '分析耗时')+f" {perf.get('wall_seconds',0):.2f} 秒")
        if visual_perf:fields.append(f"视觉复用 {visual_perf.get('cache_hits',0)} 段 / 新增推理 {visual_perf.get('model_requests',0)} 次")
        if not live and perf.get('text'):fields.append(f"文字复用 {perf['text'].get('cache_hits',0)} 段")
        if not job and perf.get('scan_cache_hit'):fields.append('画面扫描已复用')
        if not job and perf.get('rhythm_cache_hit'):fields.append('切镜已复用')
        self.efficiency.set(' · '.join(fields))
        self.totals.set(f"本工程累计：{totals['video_count']} 部视频（完成 {totals['completed_count']} / 部分 {totals['partial_count']}） · "
            f"字幕参考 {totals['subtitle_reference_count']} 条 · 视觉 {totals['visual_clip_count']} 段 / {totals['visual_frame_count']} 帧 · 跳过 {totals['skipped_count']} 个")
        latest=totals.get('latest_skip')
        self.skip.set(('最近跳过：'+(latest.get('title') or latest.get('url',''))+' · '+latest['reason']) if latest else '暂无已跳过的视频')
        options={f"{m['title'][:50]} · #{m['id']}":m['id'] for m in data.get('movies',[])}
        self._movie_ids=options;self.movie_box.configure(values=list(options))
        if self.follow.get() or self.movie.get() not in options:
            self.movie.set(next((name for name,value in options.items() if value==chosen.get('movie_id')),''))
        visual=data.get('visual') or {}
        signature=tuple((f.get('path'),f.get('sha256'),f.get('time_ms')) for f in visual.get('frames',[]))
        if signature!=self._signature:
            self._signature=signature;self.frame_index=0;self.photos=[];self._images=[]
            try:
                for frame in visual.get('frames',[]):
                    path=(self.root/frame['path']).resolve()
                    if not path.is_relative_to(self.root) or not path.is_file() or path.stat().st_size>1024*1024:raise ValueError('证据帧缺失或不安全')
                    if hashlib.sha256(path.read_bytes()).hexdigest()!=frame['sha256']:raise ValueError('证据帧内容已变化')
                    with warnings.catch_warnings():
                        warnings.simplefilter('error',Image.DecompressionBombWarning)
                        with Image.open(path) as im:
                            if im.width*im.height>12000000:raise ValueError('证据帧尺寸过大')
                            im.thumbnail((620,310));image=im.convert('RGB')
                    self._images.append(image)
                self._resize_photos()
            except Exception as exc:self.photos=[];self._images=[];self.frame_label.set('证据帧暂不可用：'+str(exc))
        observation=visual.get('observation')
        self.observation.set(self._labels(observation) if observation else '动作、表情、景别：等待当前片段的真实模型结果')
        self.evidence.set(('观察依据：'+str(observation.get('evidence') or observation.get('text') or '')[:180]) if observation else '')
        previous=chosen.get('last_visual') or {}
        self.previous.set(('上一段 '+media_time(previous.get('start_ms',0))+'：'+self._labels(previous.get('observation')))
                          if previous and previous.get('start_ms')!=visual.get('start_ms') else '')
        self._draw_charts();self._draw_stages();self._draw_frame();self._draw_rhythm()

    @staticmethod
    def _labels(observation):
        if not observation:return '等待分析'
        confidence=observation.get('confidence')
        return ' · '.join([LABELS.get(observation.get(k),'未知') for k in ('action','emotion','camera')])+(
            f' · 模型置信度 {confidence:.0%}' if isinstance(confidence,(float,int)) else '')

    def _toggle_play(self):
        self.playing=not self.playing;self.play_button.configure(text='暂停帧播放' if self.playing else '播放连续帧')

    def _animate(self):
        self._blink=not self._blink
        if self.playing and len(self.photos)>1:
            self.frame_index=(self.frame_index+1)%len(self.photos);self._animation_count+=1;self._draw_frame();self._draw_rhythm()
        self._draw_stages();self._after(650,self._animate)

    def _draw_frame(self):
        c=self.preview;c.delete('all');w=max(1,c.winfo_width());h=max(1,c.winfo_height())
        self._resize_photos()
        visual=self.data.get('visual') or {}
        if self.photos:
            c.create_image(w/2,h/2,image=self.photos[self.frame_index])
            frame=visual['frames'][self.frame_index]
            self.frame_label.set(f"证据帧 {self.frame_index+1}/{len(self.photos)} · 原片 {media_time(frame['time_ms'])} · "
                                 f"片段 {visual.get('clip_index','—')}/{visual.get('clip_count','—')} · {visual.get('model','')}"+
                                 (' · 复用校验缓存' if visual.get('cache_hit') else ''))
        else:c.create_text(w/2,h/2,text='等待连续帧\n开始画面分析后自动播放',fill=theme.MUTED,justify='center')

    def _resize_photos(self):
        size=(max(40,self.preview.winfo_width()-12),max(40,self.preview.winfo_height()-12))
        if size==getattr(self,'_photo_size',None) and len(self.photos)==len(self._images):return
        self._photo_size=size;self.photos=[]
        for original in self._images:
            image=original.copy();image.thumbnail(size)
            self.photos.append(ImageTk.PhotoImage(image,master=self.win))

    def _draw_stages(self):
        c=self.stage_canvas;c.delete('all');w=max(600,c.winfo_width());active=set()
        runtime=getattr(self,'_runtime',{})
        for phase in [runtime.get('phase',''),(self.data.get('learning') or {}).get('phase',''),
                      (self.data.get('download') or {}).get('phase','')]:
            stage=phase_stage(phase)
            if stage:active.add(stage)
        for i,label in enumerate(STAGES):
            x=22+i*(w-65)/len(STAGES)
            if i:c.create_line(x-50,18,x-8,18,fill=theme.BORDER,width=2)
            color=theme.ACCENT if label in active and self._blink else theme.TEAL if label in active else theme.MUTED
            c.create_oval(x-5,13,x+5,23,fill=color,outline='')
            c.create_text(x+15,18,text=label,anchor='w',fill=color,font=(theme.FONT,10))

    def _draw_charts(self):
        history=self.data.get('history',[])
        for i,c in enumerate(self.charts):
            c.delete('all');w=max(180,c.winfo_width());h=max(90,c.winfo_height());key=METRICS[self.chart_vars[i].get()]
            c.create_line(34,14,34,h-26,w-10,h-26,fill=theme.BORDER)
            if not history:
                c.create_text(w/2,h/2,text='暂无真实记录',fill=theme.MUTED);continue
            begin,end=history[0]['time'],history[-1]['time'];span=max(1,end-begin)
            maximum=max(1,max(p.get(key,0) for p in history));points=[]
            for p in history:
                x=34+(p['time']-begin)/span*(w-48);y=h-26-p.get(key,0)/maximum*(h-45);points.extend((x,y))
            color=theme.ACCENT if i==0 else theme.TEAL
            if len(points)>2:c.create_line(*points,fill=color,width=2)
            for x,y in zip(points[::2],points[1::2]):c.create_oval(x-2,y-2,x+2,y+2,fill=color,outline='')
            c.create_text(30,15,text=str(maximum),fill=theme.MUTED,anchor='e');c.create_text(30,h-26,text='0',fill=theme.MUTED,anchor='e')
            for x,t,anchor in [(34,begin,'w'),(w-10,end,'e')]:
                c.create_text(x,h-11,text=datetime.fromtimestamp(t,timezone(timedelta(hours=8))).strftime('%m-%d %H:%M'),fill=theme.MUTED,anchor=anchor,font=(theme.FONT,8))
            if len(history)==1:c.create_text(w/2,30,text='首次记录',fill=theme.MUTED,font=(theme.FONT,9))

    def _draw_rhythm(self):
        c=self.rhythm_canvas;c.delete('all');w=max(200,c.winfo_width());visual=self.data.get('visual') or {};r=visual.get('rhythm') or {}
        duration=r.get('duration_ms') or visual.get('end_ms') or 1
        c.create_line(12,43,w-12,43,fill=theme.BORDER,width=2)
        start,end=visual.get('start_ms',0),visual.get('end_ms',0)
        c.create_rectangle(12+start/duration*(w-24),32,12+end/duration*(w-24),53,fill=theme.SELECTED,outline='')
        cuts=r.get('cut_times_ms',[])
        for t in cuts[::max(1,len(cuts)//300)]:
            x=12+t/duration*(w-24);c.create_line(x,34,x,51,fill=theme.BLUE)
        if self.photos:
            t=visual['frames'][self.frame_index]['time_ms'];x=12+t/duration*(w-24);c.create_line(x,28,x,56,fill=theme.ACCENT,width=2)
        median=r.get('median_seconds')
        text=f"原片切镜候选 {r.get('cut_count',0)} 个"+ (f' · 镜头中位数 {median:.2f} 秒' if isinstance(median,(float,int)) else ' · 间隔数据不足')
        c.create_text(10,14,text=text,anchor='w',fill=theme.MUTED,font=(theme.FONT,9))

    def show(self):self.win.deiconify();self.win.lift()

    def _destroyed(self,event):
        if event.widget is self.win:self._dispose()

    def _dispose(self):
        self._closed=True
        if hasattr(self,'assembly'):self.assembly.close()
        for token in list(self._timers):
            try:self.win.after_cancel(token)
            except tk.TclError:pass
        self._timers.clear();self.photos=[];self._images=[]
        # Release Tk-owned objects here on the UI thread. The background reader
        # or assembly worker may otherwise trigger cyclic GC after view close.
        self.chart_vars=[]
        for key in ('follow','movie','source','status','totals','skip','observation','previous','frame_label','evidence','efficiency'):
            setattr(self,key,None)

    def close(self):
        self._dispose()
        self.win.destroy()
        if self.on_close:self.on_close()
