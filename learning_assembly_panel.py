"""Novel-to-native-assembly dashboard tab; its task survives closing the view."""
from collections import OrderedDict
import os
from pathlib import Path
import time
import tkinter as tk
from tkinter import filedialog,ttk
from PIL import Image,ImageTk
import theme
from story_assembly import LABELS,METHODS


class LearningAssemblyPanel:
    def __init__(self,parent,task,*,on_evidence=None,can_write=lambda:True):
        self.task,self.on_evidence,self.can_write=task,on_evidence,can_write
        self.win=ttk.Frame(parent,padding=8);self.win.columnconfigure(0,weight=1);self.win.rowconfigure(2,weight=1)
        self._closed=False;self._timer=None;self._result=None;self._signature=None;self._photos=OrderedDict()
        self._rows={};self._restore_text=True;self.playing=True;self._origin=time.monotonic();self._range=None;self._seek=0
        self._state_revision=None
        self.title=tk.StringVar(self.win,value='仪表盘动画');self.resources=tk.StringVar(self.win)
        self.status=tk.StringVar(self.win,value='正在读取素材匹配记录…');self.frame_text=tk.StringVar(self.win)
        self.hint=tk.StringVar(self.win,value='生成时按小说上下文检索合格学习参考，再匹配现有素材。')
        self._build();self._poll()

    def _build(self):
        form=ttk.Frame(self.win);form.grid(row=0,column=0,sticky='ew');form.columnconfigure(1,weight=1)
        self.form=form
        self.compact=ttk.Frame(self.win);self.compact.columnconfigure(0,weight=1)
        ttk.Label(self.compact,textvariable=self.title,style='Accent.TLabel').grid(row=0,column=0,sticky='w')
        self.edit_button=ttk.Button(self.compact,text='编辑小说与素材',command=self._expand_form);self.edit_button.grid(row=0,column=1,padx=6)
        self.compact_cancel=ttk.Button(self.compact,text='取消生成/预览',command=self.task.cancel);self.compact_cancel.grid(row=0,column=2)
        ttk.Label(self.compact,textvariable=self.hint,style='Caption.TLabel',wraplength=1000).grid(row=1,column=0,columnspan=3,sticky='w',pady=(3,0))
        ttk.Label(form,text='动画标题').grid(row=0,column=0,padx=(0,8))
        ttk.Entry(form,textvariable=self.title,width=18).grid(row=0,column=1,sticky='ew')
        self.generate_button=ttk.Button(form,text='一键生成有声工程',command=self.generate,style='Primary.TButton');self.generate_button.grid(row=0,column=2,padx=6)
        self.cancel_button=ttk.Button(form,text='取消生成/预览',command=self.task.cancel);self.cancel_button.grid(row=0,column=3)
        ttk.Label(form,text='素材目录').grid(row=1,column=0,pady=5)
        self.resource_entry=ttk.Entry(form,textvariable=self.resources);self.resource_entry.grid(row=1,column=1,sticky='ew')
        self.browse_button=ttk.Button(form,text='选择素材…',command=self._browse);self.browse_button.grid(row=1,column=2,padx=6)
        ttk.Label(form,text='留空自动发现 AECPP',style='Caption.TLabel').grid(row=1,column=3)
        self.source=tk.Text(form,height=3,wrap='word',bg=theme.FIELD,fg=theme.TEXT,insertbackground=theme.TEXT,font=(theme.FONT,10),undo=True)
        self.source.grid(row=2,column=0,columnspan=4,sticky='ew');self.source.bind('<Key>',lambda _:setattr(self,'_restore_text',False))
        ttk.Label(form,textvariable=self.hint,style='Caption.TLabel',wraplength=1000).grid(row=3,column=0,columnspan=4,sticky='w',pady=(3,0))
        ttk.Label(self.win,textvariable=self.status,style='Accent.TLabel',wraplength=1020).grid(row=1,column=0,sticky='w',pady=5)
        panes=ttk.Panedwindow(self.win,orient='horizontal');panes.grid(row=2,column=0,sticky='nsew')
        left=ttk.Frame(panes,width=460);left.columnconfigure(0,weight=1);left.rowconfigure(0,weight=1);left.rowconfigure(1,weight=1)
        right=ttk.Frame(panes,width=510);right.columnconfigure(0,weight=1);right.rowconfigure(0,weight=1)
        panes.add(left,weight=1);panes.add(right,weight=1)
        listing=ttk.Frame(left);listing.grid(row=0,column=0,sticky='nsew');listing.columnconfigure(0,weight=1);listing.rowconfigure(0,weight=1)
        self.tree=ttk.Treeview(listing,columns=('shot','from','action','face','time'),show='headings',height=4,selectmode='browse')
        for key,title,width in [('shot','镜头',58),('from','参考来源',94),('action','动作素材',110),('face','表情素材',105),('time','时长',56)]:
            self.tree.heading(key,text=title);self.tree.column(key,width=width,minwidth=45,stretch=True)
        self.tree.grid(row=0,column=0,sticky='nsew');scroll=ttk.Scrollbar(listing,command=self.tree.yview);scroll.grid(row=0,column=1,sticky='ns');self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind('<<TreeviewSelect>>',self._select)
        self.detail=tk.Text(left,height=5,wrap='word',bg=theme.FIELD,fg=theme.TEXT,font=(theme.FONT,9),state='disabled')
        self.detail.grid(row=1,column=0,sticky='nsew',pady=(5,0))
        detail_scroll=ttk.Scrollbar(left,command=self.detail.yview);detail_scroll.grid(row=1,column=1,sticky='ns');self.detail.configure(yscrollcommand=detail_scroll.set)
        self.evidence_button=ttk.Button(left,text='查看该镜头的学习证据',command=self._evidence,state='disabled');self.evidence_button.grid(row=2,column=0,sticky='w',pady=4)
        self.preview=tk.Canvas(right,height=185,bg=theme.MONITOR,highlightthickness=0);self.preview.grid(row=0,column=0,sticky='nsew',padx=(8,0))
        self.timeline=tk.Canvas(right,height=24,bg=theme.FIELD,highlightthickness=0);self.timeline.grid(row=1,column=0,sticky='ew',padx=(8,0));self.timeline.bind('<Button-1>',self._scrub)
        ttk.Label(right,textvariable=self.frame_text,style='Caption.TLabel',wraplength=510).grid(row=2,column=0,sticky='w',padx=8)
        controls=ttk.Frame(right);controls.grid(row=3,column=0,sticky='ew',padx=8,pady=4)
        self.play_button=ttk.Button(controls,text='暂停画面',command=self._toggle,style='Compact.TButton');self.play_button.pack(side='left')
        self.video_button=ttk.Button(controls,text='播放有声预览',command=lambda:self._open('video'),style='Compact.TButton');self.video_button.pack(side='left',padx=5)
        self.retry_button=ttk.Button(controls,text='重试预览',command=self.task.retry_preview,style='Compact.TButton');self.retry_button.pack(side='left')
        self.open_button=ttk.Button(controls,text='工程目录',command=lambda:self._open('project'),style='Compact.TButton');self.open_button.pack(side='left',padx=5)

    def _browse(self):
        path=filedialog.askdirectory(parent=self.win,title='选择 AECPP 或 assets 素材目录')
        if path:self.resources.set(path)

    def _expand_form(self):self.compact.grid_remove();self.form.grid()

    def generate(self):
        if not self.can_write():self.status.set('请等待其他生成或工程操作完成');return False
        text=self.source.get('1.0','end-1c')
        if not text.strip():self.status.set('请先粘贴小说或剧本正文');return False
        self._restore_text=False
        return self.task.start(text,self.title.get().strip() or '仪表盘动画',resources=self.resources.get().strip() or None,sound=True)

    def _poll(self):
        if self._closed:return
        state=self.task.snapshot();busy=state['busy']
        if state['revision']!=self._state_revision:self.status.set(state['message']);self._state_revision=state['revision']
        for button in [self.generate_button,self.browse_button]:button.configure(state='disabled' if busy else 'normal')
        self.cancel_button.configure(state='normal' if busy else 'disabled')
        self.compact_cancel.configure(state='normal' if busy else 'disabled')
        self.edit_button.configure(state='disabled' if busy else 'normal')
        value=state.get('result')
        signature=(value['output'],(value.get('preview') or {}).get('directory'),value.get('preview_error')) if value else None
        if value and signature!=self._signature:
            self._signature=signature;self._result=value;self._render_result()
        if self._restore_text and state.get('input') and not self.source.get('1.0','end-1c').strip():
            self.source.insert('1.0',state['input']);self._restore_text=False
        self.open_button.configure(state='normal' if self._result else 'disabled')
        self.video_button.configure(state='normal' if self._result and self._result.get('preview') else 'disabled')
        self.retry_button.configure(state='normal' if self._result and not busy else 'disabled')
        try:self._draw()
        except (OSError,ValueError,KeyError,tk.TclError) as exc:self.frame_text.set('组合预览暂不可用：'+str(exc))
        self._timer=self.win.after(120,self._poll)

    def _render_result(self):
        self._photos.clear();self._range=None;self._seek=0;self._origin=time.monotonic()
        self.title.set(self._result['title']);self.form.grid_remove();self.compact.grid(row=0,column=0,sticky='ew')
        for item in self.tree.get_children():self.tree.delete(item)
        self._rows={row['id']:row for row in self._result['rows']}
        count=sum(bool(row['reference']) for row in self._rows.values())
        sound=self._result['sound']
        self.hint.set(f"{len(self._rows)} 个镜头 · {count} 个使用视觉学习参考 · {sound.get('voiced_shots',0)} 句配音 · "
                      f"{sound.get('mouth_clips',0)} 个角色口型轨 · {sound.get('effect_events',0)} 处音效")
        for i,row in enumerate(self._rows.values(),1):
            self.tree.insert('', 'end',iid=row['id'],values=(str(i),'视觉学习' if row['reference'] else '原文/编排规则',
                row['action']['name'],row['expression']['name'],f"{row['duration']:.2f}s"))
        if self._rows:
            first=next(iter(self._rows));self._initial_selection=True;self.tree.selection_set(first);self._show_detail(self._rows[first])

    def _selected(self):
        selection=self.tree.selection();return self._rows.get(selection[0]) if selection else None

    def _select(self,event=None):
        row=self._selected()
        if row:
            self._show_detail(row)
            if getattr(self,'_initial_selection',False):self._initial_selection=False;return
            self._range=(row['start'],row['start']+row['duration']);self._seek=row['start'];self._origin=time.monotonic()

    def _show_detail(self,row):
        reference=row['reference'];changes=reference.get('changes',{})
        lines=[f"{row['character']}：{row['text']}"]
        if reference:
            lines.append(f"学习来源：视频 #{reference['movie_id']} · {reference['start_ms']/1000:.1f}–{reference['end_ms']/1000:.1f}s · "
                         f"置信度 {reference['confidence']:.0%} · 待审核观察")
            lines.append('实际应用：'+ '、'.join({'action':'动作','emotion':'表情','camera':'景别','duration':'镜头节奏'}.get(k,k) for k in reference.get('applied_fields',[])))
        else:lines.append('参考来源：小说原文、人工偏好或基础编排规则。')
        for field,title in [('actor','角色'),('action','动作'),('expression','表情'),('background','场景')]:
            item=row[field];learned=(field=='action' and 'action' in changes) or (field=='expression' and 'emotion' in changes)
            lines.append(f"{title}：{'学习建议 ' if learned else ''}{LABELS.get(item['requested'],item['requested'])} → {item['name']} "
                f"[{item['asset_id']}] · {METHODS.get(item['method'],item['method'])}"+('（替代）' if item['substituted'] else ''))
            if item['reason']:lines.append('  '+item['reason'])
        lines.append(f"景别：{LABELS[row['camera']]} → 角色缩放 {row['scale']:.2f}（工程变换关键帧）")
        rhythm=changes.get('duration')
        if rhythm:lines.append(f"节奏：切镜间隔 {rhythm['observed_seconds']:.2f}s → 阅读时长 {rhythm['before']:.2f}s 调整为 {rhythm['after']:.2f}s")
        timing=row['sound_timing']
        if timing:lines.append(f"配音对齐：{timing['before']:.2f}s → {timing['after']:.2f}s；语音 {timing['speech_seconds']:.2f}s")
        lines.append(f"最终镜头：{row['start']:.2f}–{row['start']+row['duration']:.2f}s")
        if self._result.get('warnings'):lines.append('组装说明：'+'；'.join(self._result['warnings'][:8]))
        if self._result.get('preview_error'):lines.append('预览：'+self._result['preview_error'])
        self.detail.configure(state='normal');self.detail.delete('1.0','end');self.detail.insert('1.0','\n'.join(lines));self.detail.configure(state='disabled')
        self.evidence_button.configure(state='normal' if reference and self.on_evidence else 'disabled')

    def _evidence(self):
        row=self._selected()
        if row and row['reference'] and self.on_evidence:self.on_evidence(row['reference'])

    def _toggle(self):
        self._seek=self._position();self._origin=time.monotonic();self.playing=not self.playing
        self.play_button.configure(text='暂停画面' if self.playing else '播放画面')

    def _position(self):
        preview=(self._result or {}).get('preview')
        if not preview:return 0
        start,end=self._range or (0,preview['duration']);end=min(end,preview['duration'])
        if start>=end:return start
        return start+((self._seek-start+(time.monotonic()-self._origin if self.playing else 0))%max(.001,end-start))

    def _scrub(self,event):
        preview=(self._result or {}).get('preview')
        if preview:self._range=None;self._seek=max(0,min(.999,event.x/max(1,self.timeline.winfo_width())))*preview['duration'];self._origin=time.monotonic()

    def _draw(self):
        c=self.preview;c.delete('all');w=max(1,c.winfo_width());h=max(1,c.winfo_height())
        preview=(self._result or {}).get('preview');self.timeline.delete('all')
        if not preview:
            c.create_text(w/2,h/2,text='输入小说或剧本，生成现有素材组合预览',fill=theme.MUTED);return
        at=self._position()
        if at>=preview['duration']:
            c.create_text(w/2,h/2,text='该镜头在预览范围之后\n可打开工程查看完整动画',fill=theme.MUTED,justify='center');return
        index=min(len(preview['frames'])-1,int(at*preview['fps']));size=(max(32,w-10),max(32,h-10));key=(index,size)
        if key not in self._photos:
            base=Path(preview['directory']).resolve();path=(base/preview['frames'][index]).resolve()
            if not path.is_relative_to(base) or path.stat().st_size>4*1024*1024:raise ValueError('预览帧路径或大小无效')
            with Image.open(path) as image:
                if image.width*image.height>2000000:raise ValueError('预览帧尺寸无效')
                image.thumbnail(size);self._photos[key]=ImageTk.PhotoImage(image.copy(),master=self.win)
            while len(self._photos)>24:self._photos.popitem(last=False)
        c.create_image(w/2,h/2,image=self._photos[key])
        row=next((r for r in self._rows.values() if r['start']<=at<r['start']+r['duration']),None)
        self.frame_text.set(f"AECPP 原生组合 · {at:.2f}/{preview['duration']:.2f}s · "+(row['id'] if row else '')+
            (f" · 全片 {preview['full_duration']:.2f}s，预览前30秒" if preview['full_duration']>30 else ' · 完整画面预览'))
        tw=max(1,self.timeline.winfo_width());self.timeline.create_line(0,12,tw,12,fill=theme.BORDER,width=3)
        for shot in self._rows.values():
            if shot['start']<preview['duration']:
                x=shot['start']/preview['duration']*tw;self.timeline.create_line(x,5,x,19,fill=theme.BLUE)
        x=at/preview['duration']*tw;self.timeline.create_line(x,2,x,22,fill=theme.ACCENT,width=2)

    def _open(self,kind):
        if not self._result:return
        try:
            target=Path(self._result['output']).parent if kind=='project' else Path(self._result['preview']['directory'])/self._result['preview']['video']
            if not target.exists():raise ValueError('文件已移动或删除')
            os.startfile(str(target))
        except Exception as exc:self.status.set(str(exc))

    def close(self):
        self._closed=True
        if self._timer:
            try:self.win.after_cancel(self._timer)
            except tk.TclError:pass
        self._photos.clear()
        self.on_evidence=None
        for key in ('title','resources','status','frame_text','hint'):setattr(self,key,None)
