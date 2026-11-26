"""Non-modal management of persistent video caches and storage locations."""
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import cache_storage as storage
import theme


def human_size(value):
    value = float(value or 0)
    for unit in ("B","KB","MB","GB","TB"):
        if value < 1024 or unit=="TB":
            return f"{value:.1f} {unit}"
        value /= 1024


class CacheWindow:
    def __init__(self,parent,root,*,is_busy,on_changed=None,container=None):
        self.root = Path(root).resolve()
        self.is_busy = is_busy
        self.on_changed = on_changed
        self.busy = False
        self._closed = False
        self._timer = None
        self._events = queue.Queue()
        self._rows = {}
        self._summary = None
        self.embedded = container is not None
        if self.embedded:
            self.win = ttk.Frame(container)
            self.body = ttk.Frame(self.win,padding=(20,16))
            self.body.pack(fill="both",expand=True)
            header = ttk.Frame(self.body)
            header.pack(fill="x")
            ttk.Label(header,text="缓存管理",style="Title.TLabel").pack(side="left")
            ttk.Label(header,text="本地资源 · 存储与维护",style="Muted.TLabel").pack(side="right")
            ttk.Separator(self.body).pack(fill="x")
        else:
            self.win,self.body = theme.sheet(parent,"视频缓存管理",context="本地资源 · 存储与维护")
            self.win.geometry("980x660")
            self.win.minsize(800,560)
            self.win.protocol("WM_DELETE_WINDOW",self.close)
        self.win.bind("<Destroy>",self._destroyed,add="+")
        self.status = tk.StringVar(self.win,value="正在读取缓存清单…")
        self.location = tk.StringVar(self.win,value=storage.get_settings(self.root)["current"])
        self.stats = tk.StringVar(self.win,value="—")
        self.detail = tk.StringVar(self.win,value="选择缓存可查看位置与原片依赖。")
        self.filter = tk.StringVar(self.win,value="全部缓存")
        self._build()
        self._timer = self.win.after(120,self._poll)
        self.refresh()

    def _build(self):
        # The sheet supplies a header and separator using pack. Move these to
        # the same grid so the list alone absorbs vertical resizing.
        header,separator = self.body.winfo_children()
        header.pack_forget()
        separator.pack_forget()
        self.body.columnconfigure(0,weight=1)
        self.body.rowconfigure(6,weight=1)
        header.grid(row=0,column=0,sticky="ew")
        separator.grid(row=1,column=0,sticky="ew",pady=(10,10))
        self.stats_label = ttk.Label(self.body,textvariable=self.stats,style="Section.TLabel")
        self.stats_label.grid(row=2,column=0,sticky="ew",pady=(0,7))
        location = ttk.Frame(self.body)
        location.grid(row=3,column=0,sticky="ew",pady=(0,7))
        location.columnconfigure(1,weight=1)
        ttk.Label(location,text="新视频缓存位置",style="Muted.TLabel").grid(row=0,column=0,padx=(0,10))
        ttk.Entry(location,textvariable=self.location,state="readonly").grid(row=0,column=1,sticky="ew")
        ttk.Button(location,text="切换位置…",command=self._change,style="Accent.TButton").grid(row=0,column=2,padx=(8,0))
        ttk.Button(location,text="打开目录",command=self._open_current).grid(row=0,column=3,padx=(8,0))
        self.location_hint = ttk.Label(self.body,text="切换后旧缓存仍可复用。迁移会校验文件并更新原片路径；已完成的标注和模型保留。",
                                       style="Caption.TLabel",wraplength=700)
        self.location_hint.grid(row=4,column=0,sticky="ew",pady=(0,8))
        bar = ttk.Frame(self.body)
        bar.grid(row=5,column=0,sticky="ew",pady=(0,7))
        ttk.Label(bar,text="缓存资源",style="Section.TLabel").pack(side="left")
        choices = ttk.Combobox(bar,textvariable=self.filter,state="readonly",values=["全部缓存","正在使用","可清理缓存","未完成缓存"],width=12)
        choices.pack(side="left",padx=12)
        choices.bind("<<ComboboxSelected>>",lambda _:self._render())
        ttk.Button(bar,text="刷新",command=self.refresh,style="Compact.TButton").pack(side="right")
        listing = ttk.Frame(self.body)
        listing.grid(row=6,column=0,sticky="nsew")
        listing.rowconfigure(0,weight=1)
        listing.columnconfigure(0,weight=1)
        self.tree = ttk.Treeview(listing,columns=("title","state","size","uses","location"),show="headings",selectmode="extended",height=8)
        for name,title,width in (("title","影片 / 来源",260),("state","缓存状态",90),("size","磁盘占用",90),("uses","原片依赖",120),("location","所在目录",230)):
            self.tree.heading(name,text=title)
            self.tree.column(name,width=width,minwidth=65,stretch=name in {"title","location"})
        self.tree.grid(row=0,column=0,sticky="nsew")
        vertical = ttk.Scrollbar(listing,orient="vertical",command=self.tree.yview)
        vertical.grid(row=0,column=1,sticky="ns")
        horizontal = ttk.Scrollbar(listing,orient="horizontal",command=self.tree.xview)
        horizontal.grid(row=1,column=0,sticky="ew")
        self.tree.configure(yscrollcommand=vertical.set,xscrollcommand=horizontal.set)
        self.tree.tag_configure("referenced",foreground=theme.TEAL)
        self.tree.tag_configure("incomplete",foreground=theme.ACCENT)
        self.tree.tag_configure("unsafe",foreground=theme.RED)
        self.tree.bind("<<TreeviewSelect>>",lambda _:self._selection())
        self.tree.bind("<Double-1>",lambda _:self._open_selected())
        self.tree.bind("<Control-a>",self._select_all)
        self.tree.bind("<Button-3>",self._menu)
        self.detail_text = self._bounded_text(self.body,self.detail,3,theme.MUTED)
        self.detail_text.master.grid(row=7,column=0,sticky="ew",pady=(7,5))
        actions = ttk.Frame(self.body)
        actions.grid(row=8,column=0,sticky="ew",pady=(0,5))
        actions.columnconfigure(3,weight=1)
        self.action_buttons = []
        for index,(text,command,style) in enumerate((
                ("全选",self._select_all,"Compact.TButton"),
                ("打开所选目录",self._open_selected,"Compact.TButton"),
                ("清理未使用缓存…",self._cleanup,"TButton"),
                ("删除所选缓存…",self._delete,"Danger.TButton"))):
            button = ttk.Button(actions,text=text,command=command,style=style)
            button.grid(row=0,column=index,sticky="e" if index==3 else "w",padx=(0 if index==0 else 6,0))
            self.action_buttons.append(button)
        ttk.Separator(self.body).grid(row=9,column=0,sticky="ew",pady=(2,5))
        self.status_text = self._bounded_text(self.body,self.status,2,theme.SOFT)
        self.status_text.master.grid(row=10,column=0,sticky="ew")
        self.body.bind("<Configure>",self._resize,add="+")

    def _bounded_text(self,parent,variable,lines,color):
        """Long paths/statuses scroll without consuming the action row."""
        frame = ttk.Frame(parent)
        frame.columnconfigure(0,weight=1)
        text = tk.Text(frame,height=lines,width=1,wrap="word",background=theme.PANEL,
                       foreground=color,font=(theme.FONT,9),relief="flat",borderwidth=0,
                       highlightthickness=0,padx=0,pady=0,cursor="arrow",takefocus=False)
        text.grid(row=0,column=0,sticky="ew")
        scroll = ttk.Scrollbar(frame,orient="vertical",command=text.yview)
        scroll.grid(row=0,column=1,sticky="ns")
        text.configure(yscrollcommand=scroll.set)
        def changed(*_):
            if self._closed:
                return
            text.configure(state="normal")
            text.delete("1.0","end")
            text.insert("1.0",variable.get())
            text.configure(state="disabled")
        variable.trace_add("write",changed)
        changed()
        return text

    def _resize(self,event):
        if event.widget==self.body:
            available = max(300,event.width-40)
            self.location_hint.configure(wraplength=available)
            self.stats_label.configure(wraplength=available)

    def _select_all(self,event=None):
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def _selected(self):
        return [self._rows[iid] for iid in self.tree.selection() if iid in self._rows]

    def _selection(self):
        rows = self._selected()
        if not rows:
            self.detail.set("选择缓存可查看位置与原片依赖。双击打开目录，Ctrl / Shift 多选。")
            return
        used = list(dict.fromkeys(name for row in rows for name in row["movie_names"]))
        text = f"已选 {len(rows)} 项 · {human_size(sum(row['size'] for row in rows))}"
        text += "\n仍用于原片："+"、".join(used[:5]) if used else "\n没有电影把这些缓存作为原片，可清理以释放空间。"
        if len(rows)==1:
            text += "\n"+rows[0]["path"]
        self.detail.set(text)

    def _render(self):
        selected = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        for iid,row in self._rows.items():
            value = self.filter.get()
            if value=="正在使用" and not row["movie_ids"]:
                continue
            if value=="可清理缓存" and not row["disposable"]:
                continue
            if value=="未完成缓存" and row["status"]!="incomplete":
                continue
            state = {"complete":"完整","incomplete":"未完成 / 失效","unsafe":"路径异常"}[row["status"]]
            tag = "referenced" if row["movie_ids"] else row["status"]
            self.tree.insert("","end",iid=iid,values=(row["title"],state,human_size(row["size"]),
                             "、".join(row["movie_names"]) or "可清理",row["root"]),tags=(tag,))
        self.tree.selection_set([iid for iid in selected if self.tree.exists(iid)])
        self._selection()

    def _run(self,action,*,changed=False):
        if self.busy:
            self.status.set("正在整理缓存，请等待本次任务结束。")
            return False
        self.busy = True
        self.status.set("正在处理缓存…")
        def work():
            try:
                result = action(lambda message:self._events.put(("progress",str(message))))
                summary = storage.scan_caches(self.root)
                self._events.put(("done",(result,summary,changed)))
            except Exception as exc:
                self._events.put(("error",str(exc)))
        threading.Thread(target=work,daemon=True,name="video-cache-management").start()
        return True

    def refresh(self):
        self._run(lambda report:None)

    def _idle(self):
        if self.busy or self.is_busy():
            self.status.set("请先结束下载、截图或训练任务，再切换位置、迁移或清理缓存。")
            return False
        return True

    def _change(self):
        if not self._idle():
            return
        selected = filedialog.askdirectory(parent=self.win,title="选择新的专用视频缓存目录",initialdir=self.location.get(),mustexist=False)
        if not selected:
            return
        choice = messagebox.askyesnocancel("切换缓存位置",
            "是否把已有缓存一并迁移？\n\n是：复制校验后切换原片路径，并移除旧缓存副本。\n否：仅改变新视频的存放位置，旧缓存仍可读取。\n取消：保持当前设置。",
            parent=self.win)
        if choice is None or not self._idle():
            return
        self._run(lambda report:storage.change_location(self.root,selected,migrate=choice,report=report),changed=True)

    def _delete(self):
        if not self._idle():
            return
        rows = self._selected()
        if not rows:
            self.status.set("请先选择要删除的缓存。")
            return
        referenced = list(dict.fromkeys(name for row in rows for name in row["movie_names"]))
        message = f"删除所选 {len(rows)} 个视频缓存，预计释放 {human_size(sum(row['size'] for row in rows))}？\n\n"
        if referenced:
            message += "其中原片仍用于："+"、".join(referenced[:8])+"。\n删除后需重新下载或重新导入原片，才能继续抽帧、播放和再次训练。\n\n"
        message += "截图、字幕、人工标注和已训练模型会保留；视频缓存文件将从磁盘移除。"
        if messagebox.askyesno("删除视频缓存",message,parent=self.win) and self._idle():
            self._run(lambda report:storage.remove_caches(self.root,[row["path"] for row in rows],allow_referenced=bool(referenced),report=report),changed=True)

    def _cleanup(self):
        if not self._idle():
            return
        rows = [row for row in self._rows.values() if row["disposable"]]
        if not rows:
            self.status.set("当前没有未被电影作为原片使用的缓存。")
            return
        message = f"清理 {len(rows)} 个未使用缓存，预计释放 {human_size(sum(row['size'] for row in rows))}？\n\n包含未完成下载和已有其他原片副本的缓存。需要时可重新下载，电影原片、标注和模型保留。"
        if messagebox.askyesno("清理未使用缓存",message,parent=self.win) and self._idle():
            self._run(lambda report:storage.remove_caches(self.root,[row["path"] for row in rows],report=report),changed=True)

    def _open(self,path):
        path = Path(path)
        if not path.is_dir():
            self.status.set("目录尚未创建或已不存在。")
            return
        try:
            if os.name=="nt":
                os.startfile(str(path))
            else:
                subprocess.Popen(["open" if sys.platform=="darwin" else "xdg-open",str(path)])
        except OSError as exc:
            self.status.set(str(exc))

    def _open_current(self):
        self._open(self.location.get())

    def _open_selected(self):
        rows = self._selected()
        if rows:
            self._open(rows[0]["path"])

    def _menu(self,event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        menu = tk.Menu(self.win,tearoff=False)
        menu.add_command(label="打开缓存目录",command=self._open_selected)
        menu.add_command(label="删除所选缓存…",command=self._delete)
        try:
            menu.tk_popup(event.x_root,event.y_root)
        finally:
            menu.grab_release()

    def _poll(self):
        self._timer = None
        if self._closed:
            return
        while not self._events.empty():
            kind,value = self._events.get_nowait()
            if kind=="progress":
                self.status.set(value)
            elif kind=="error":
                self.busy = False
                self.status.set(value)
                messagebox.showerror("视频缓存管理",value,parent=self.win)
            else:
                result,summary,changed = value
                self.busy = False
                self._summary = summary
                self._rows = {str(i):row for i,row in enumerate(summary["items"])}
                self.location.set(summary["settings"]["current"])
                self.stats.set(f"{len(summary['items'])} 个缓存    ·    占用 {human_size(summary['size'])}    ·    新位置可用 {human_size(summary['free'])}")
                self._render()
                warnings = summary["warnings"]+((result or {}).get("warnings",[]))
                message = "缓存清单已更新。"
                if result and "migrated" in result:
                    message = f"缓存位置已更新 · 迁移 {result['migrated']} 项。"
                elif result and "removed" in result:
                    message = f"已删除 {result['removed']} 个缓存 · 释放 {human_size(result['size'])}。"
                self.status.set(message+("\n"+"\n".join(warnings) if warnings else ""))
                if changed and self.on_changed:
                    try:
                        self.on_changed()
                    except Exception as exc:
                        self.status.set(message+f" 主界面刷新失败：{exc}")
        if not self._closed:
            self._timer = self.win.after(120,self._poll)

    def _destroyed(self,event):
        if event.widget==self.win:
            self._closed = True
            self._cancel_timer()

    def _cancel_timer(self):
        if self._timer is not None:
            try:
                self.win.after_cancel(self._timer)
            except tk.TclError:
                pass
            self._timer = None

    def show(self):
        """Bring an existing page forward; the caller maps embedded frames."""
        if not self._closed:
            if not self.embedded:
                self.win.deiconify()
            self.win.lift()

    def close(self):
        if self._closed:
            return True
        if self.busy:
            self.status.set("缓存整理正在进行，请等待完成后关闭；已校验前不会移除原片。")
            return False
        if not self._closed:
            self._closed = True
            self._cancel_timer()
            self.win.destroy()
        return True
