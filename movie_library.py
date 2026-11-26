"""Embedded film library: review, learn and correct existing movie resources."""
from contextlib import closing
import json
from pathlib import Path
import queue
import sqlite3
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from urllib.parse import urlsplit

from movie_usage import MovieUsageIndex
import theme


GROUPS = {"time":"片内划分", "train":"训练集", "val":"验证集"}
PROFILE_STATES = {"confirmed":"已确认", "manual-needed":"待填写", "draft":"待核对"}
FILTERS = ("全部影片", "待核对资料", "原片不可用", "已学影片", "未学影片")


def _object(value):
    try:
        value = json.loads(value)
        return value if isinstance(value,dict) else {}
    except (TypeError,ValueError):
        return {}


def source_status(movie):
    """Check availability without opening or decoding a movie."""
    source = movie.get("source_path")
    if not source:
        return False,"待定位原片"
    try:
        path = Path(source)
        if not path.is_file():
            return False,"原片缺失"
        stat = path.stat()
        if (movie.get("source_size") is not None and movie.get("source_mtime") is not None
                and (stat.st_size,stat.st_mtime_ns)!=(movie["source_size"],movie["source_mtime"])):
            return False,"原片已变化"
    except (OSError,ValueError):
        return False,"无法读取原片"
    return True,"可用"


def read_library(root):
    """One read transaction for local/online films and their review state."""
    root = Path(root).resolve()
    path = root/"annotations.sqlite"
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.as_uri()+"?mode=ro",uri=True,timeout=.2)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        movies = [dict(row) for row in db.execute("SELECT * FROM movies ORDER BY id DESC")]
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        profiles = {row[0]:_object(row[1]) for row in db.execute("SELECT movie,data FROM movie_profiles")} if "movie_profiles" in tables else {}
        frames = {row[0]:(row[1],row[2]) for row in db.execute(
            "SELECT movie,COUNT(*),SUM(state='reviewed') FROM frames WHERE deleted=0 GROUP BY movie")} if "frames" in tables else {}
    online = {}
    jobs_path = root/"online_jobs.sqlite"
    if jobs_path.is_file():
        try:
            with closing(sqlite3.connect(jobs_path.as_uri()+"?mode=ro",uri=True,timeout=.2)) as db:
                for row in db.execute("SELECT data FROM online_jobs ORDER BY created"):
                    job = _object(row[0])
                    result = job.get("result") or {}
                    if isinstance(result,dict) and type(result.get("movie_id")) is int:
                        online[result["movie_id"]] = job
        except sqlite3.Error:
            pass  # Film records remain usable when online history is unavailable.
    for movie in movies:
        profile = profiles.get(movie["id"],{})
        movie["title"] = str(profile.get("title") or movie["name"])
        movie["year"] = str(profile.get("year") or "")
        movie["profile_status"] = profile.get("status","draft")
        movie["frame_count"],movie["reviewed_count"] = frames.get(movie["id"],(0,0))
        movie["source_available"],movie["source_status"] = source_status(movie)
        job = online.get(movie["id"],{})
        movie["source_url"] = str(job.get("url") or "")
        movie["source_kind"] = "在线视频" if job else "本地影片"
        try:
            movie["source_host"] = urlsplit(movie["source_url"]).hostname or ""
        except ValueError:
            movie["source_host"] = ""
        if not job and movie.get("source_path"):
            try:
                if Path(movie["source_path"]).resolve().is_relative_to(root/"online_media"):
                    movie["source_kind"] = "在线视频"
            except (OSError,ValueError):
                pass
    return movies


def visible_movies(movies, usage, query="", state="全部影片"):
    query = query.strip().casefold()
    result = []
    for movie in movies:
        learned = usage.get(movie["id"],{}).get("completed",0)>0
        if query and query not in " ".join(str(movie.get(key) or "") for key in ("title","name","year","source_host","source_path")).casefold():
            continue
        if state=="待核对资料" and movie["profile_status"]=="confirmed":
            continue
        if state=="原片不可用" and movie["source_available"]:
            continue
        if state=="已学影片" and not learned:
            continue
        if state=="未学影片" and learned:
            continue
        result.append(movie)
    return result


class MovieLibrary:
    def __init__(self,parent,root,*,on_import,on_profiles,on_learn,on_annotate,on_subtitles,on_training,
                 on_online=None,active_training=lambda:None):
        self.root = Path(root).resolve()
        self.on_import,self.on_profiles,self.on_learn = on_import,on_profiles,on_learn
        self.on_annotate,self.on_subtitles,self.on_training = on_annotate,on_subtitles,on_training
        self.on_online = on_online
        self.frame = tk.Frame(parent,bg=theme.BACKGROUND,padx=16,pady=12)
        self.frame.bind("<Destroy>",self._destroyed,add="+")
        self.frame.bind("<Configure>",self._resize,add="+")
        self.closed = False
        self._timer,self._worker = None,None
        self._events = queue.Queue()
        self._movies,self._by_id,self.usage = [],{},{}
        self._next_refresh = 0.
        self.usage_index = MovieUsageIndex(self.root,active_training)
        self.search = tk.StringVar(self.frame)
        self.filter = tk.StringVar(self.frame,value="全部影片")
        self.status = tk.StringVar(self.frame,value="正在读取影片资源…")
        self.details = tk.StringVar(self.frame,value="选择影片，可准备资料、开始学习或进入人工校正。")
        self.summary = tk.StringVar(self.frame,value="—")
        self._build()
        self.search.trace_add("write",lambda *_:self._render())
        self.filter.trace_add("write",lambda *_:self._render())
        self.refresh()
        self._timer = self.frame.after(100,self._poll)

    def _build(self):
        self.frame.columnconfigure(0,weight=1)
        self.frame.rowconfigure(3,weight=1)
        top = tk.Frame(self.frame,bg=theme.BACKGROUND)
        top.grid(row=0,column=0,sticky="ew",pady=(0,10))
        top.columnconfigure(0,weight=1)
        tk.Label(top,text="影片资源",font=(theme.FONT,20,"bold"),bg=theme.BACKGROUND,fg=theme.TEXT).grid(row=0,column=0,sticky="w")
        self.subtitle = tk.Label(top,text="在线与本地影片集中管理，资料与校正随时补充。",font=(theme.FONT,9),bg=theme.BACKGROUND,fg=theme.MUTED,anchor="w")
        self.subtitle.grid(row=1,column=0,columnspan=3,sticky="w",pady=(3,0))
        if self.on_online:
            ttk.Button(top,text="添加在线视频",command=lambda:self._perform(self.on_online),style="Primary.TButton").grid(row=0,column=1,padx=(10,7))
        ttk.Button(top,text="导入本地影片",command=lambda:self._perform(self.on_import)).grid(row=0,column=2)
        card = tk.Frame(self.frame,bg=theme.PANEL,highlightbackground=theme.BORDER,highlightthickness=1,padx=12,pady=10)
        card.grid(row=1,column=0,sticky="ew",pady=(0,10))
        tk.Label(card,textvariable=self.summary,bg=theme.PANEL,fg=theme.ACCENT,font=(theme.FONT,11,"bold"),anchor="w").pack(fill="x")
        search = tk.Frame(self.frame,bg=theme.BACKGROUND)
        search.grid(row=2,column=0,sticky="ew",pady=(0,8))
        search.columnconfigure(1,weight=1)
        tk.Label(search,text="搜索影片",bg=theme.BACKGROUND,fg=theme.MUTED,font=(theme.FONT,9)).grid(row=0,column=0,padx=(0,10))
        self.search_entry = ttk.Entry(search,textvariable=self.search)
        self.search_entry.grid(row=0,column=1,sticky="ew",padx=(0,8))
        ttk.Combobox(search,textvariable=self.filter,state="readonly",values=FILTERS,width=12).grid(row=0,column=2,padx=(0,8))
        ttk.Button(search,text="刷新",command=self.refresh,style="Compact.TButton").grid(row=0,column=3)
        listing = ttk.Frame(self.frame)
        listing.grid(row=3,column=0,sticky="nsew")
        listing.columnconfigure(0,weight=1)
        listing.rowconfigure(0,weight=1)
        self.tree = ttk.Treeview(listing,columns=("title","source","file","profile","learning","group","frames"),
                                 show="headings",selectmode="extended",height=5)
        for key,label,width,minimum in (("title","影片",225,130),("source","来源",90,80),("file","原片",95,85),
                                       ("profile","影片资料",90,80),("learning","学习记录",100,90),
                                       ("group","分组",90,80),("frames","确认 / 截图",105,85)):
            self.tree.heading(key,text=label,anchor="w")
            self.tree.column(key,width=width,minwidth=minimum,stretch=key=="title",anchor="w")
        self.tree.grid(row=0,column=0,sticky="nsew")
        self.tree.tag_configure("missing",foreground=theme.RED)
        self.tree.tag_configure("attention",foreground=theme.ACCENT)
        self.tree.tag_configure("ready",foreground=theme.TEAL)
        vertical = ttk.Scrollbar(listing,orient="vertical",command=self.tree.yview)
        vertical.grid(row=0,column=1,sticky="ns")
        horizontal = ttk.Scrollbar(listing,orient="horizontal",command=self.tree.xview)
        horizontal.grid(row=1,column=0,sticky="ew")
        self.tree.configure(yscrollcommand=vertical.set,xscrollcommand=horizontal.set)
        self.tree.bind("<<TreeviewSelect>>",lambda _:self._selection())
        self.tree.bind("<Double-1>",lambda _:self._profiles())
        self.tree.bind("<Control-a>",self._select_all)
        self.tree.bind("<Button-3>",self._menu)
        self.empty = tk.Label(listing,text="",bg=theme.FIELD,fg=theme.MUTED,font=(theme.FONT,11),justify="center")
        self.empty.place(relx=.5,rely=.48,anchor="center")
        detail = tk.Frame(self.frame,bg=theme.BACKGROUND)
        detail.grid(row=4,column=0,sticky="ew",pady=(9,8))
        detail.columnconfigure(0,weight=1)
        self.detail_text = tk.Text(detail,height=3,width=1,wrap="word",background=theme.BACKGROUND,foreground=theme.MUTED,
                                   font=(theme.FONT,9),relief="flat",highlightthickness=0,takefocus=False)
        self.detail_text.grid(row=0,column=0,sticky="ew")
        scroll = ttk.Scrollbar(detail,orient="vertical",command=self.detail_text.yview)
        scroll.grid(row=0,column=1,sticky="ns")
        self.detail_text.configure(yscrollcommand=scroll.set,state="disabled")
        self.details.trace_add("write",lambda *_:self._detail_changed())
        self._detail_changed()
        actions = tk.Frame(self.frame,bg=theme.BACKGROUND)
        actions.grid(row=5,column=0,sticky="ew",pady=(0,8))
        self.action_buttons = {}
        choices = (("profiles","影片资料",self._profiles,"Accent.TButton"),
                   ("learn","学习所选",self._learn,"Primary.TButton"),
                   ("annotate","人工校正",lambda:self._single(self.on_annotate),"TButton"),
                   ("subtitles","字幕 / 剧本",lambda:self._single(self.on_subtitles),"TButton"),
                   ("training","训练结果",lambda:self._perform(self.on_training),"TButton"))
        for index,(key,text,command,style) in enumerate(choices):
            actions.columnconfigure(index,weight=1,uniform="actions")
            button = ttk.Button(actions,text=text,command=command,style=style)
            button.grid(row=0,column=index,sticky="ew",padx=(0 if index==0 else 7,0))
            self.action_buttons[key] = button
        footer = tk.Frame(self.frame,bg=theme.BACKGROUND)
        footer.grid(row=6,column=0,sticky="ew")
        footer.columnconfigure(0,weight=1)
        self.status_text = tk.Text(footer,height=2,width=1,wrap="word",background=theme.BACKGROUND,foreground=theme.SOFT,
                                  font=(theme.FONT,9),relief="flat",highlightthickness=0,takefocus=False)
        self.status_text.grid(row=0,column=0,sticky="ew")
        status_scroll = ttk.Scrollbar(footer,orient="vertical",command=self.status_text.yview)
        status_scroll.grid(row=0,column=1,sticky="ns")
        self.status_text.configure(yscrollcommand=status_scroll.set,state="disabled")
        self.status.trace_add("write",lambda *_:self._status_changed())
        self._status_changed()

    def _resize(self,event):
        if event.widget==self.frame and not self.closed and hasattr(self,"subtitle"):
            width = max(200,event.width-40)
            self.subtitle.configure(wraplength=width)

    def refresh(self):
        if self.closed or (self._worker and self._worker.is_alive()):
            return
        self._next_refresh = time.monotonic()+3
        def read():
            try:
                self._events.put(("movies",read_library(self.root)))
            except Exception as exc:
                self._events.put(("error",str(exc)))
        self._worker = threading.Thread(target=read,daemon=True,name="movie-library-read")
        self._worker.start()

    def _poll(self):
        self._timer = None
        if self.closed:
            return
        while not self._events.empty():
            kind,value = self._events.get_nowait()
            if kind=="movies":
                self._movies = value
                self._by_id = {str(movie["id"]):movie for movie in value}
                if not self.status.get() or self.status.get().startswith("正在读取"):
                    self.status.set("Ctrl / Shift 多选 · 双击影片核对资料 · 已入库不等于已完成学习")
            else:
                self.status.set("暂时无法读取资源："+value)
        self.usage = self.usage_index.snapshot()
        self._render()
        if self.frame.winfo_ismapped() and time.monotonic()>=self._next_refresh:
            self.refresh()
        self._timer = self.frame.after(500,self._poll)

    def _render(self):
        if self.closed:
            return
        visible = visible_movies(self._movies,self.usage,self.search.get(),self.filter.get())
        visible_ids = {str(movie["id"]) for movie in visible}
        for iid in self.tree.get_children():
            if iid not in visible_ids:
                self.tree.delete(iid)
        for index,movie in enumerate(visible):
            iid = str(movie["id"])
            usage = self.usage.get(movie["id"],{})
            learning = "训练中" if usage.get("last_state")=="running" else f"已学 {usage['completed']} 批" if usage.get("completed") else "未学习"
            title = movie["title"]+(f" · {movie['year']}" if movie["year"] else "")
            values = (title,movie["source_kind"],movie["source_status"],PROFILE_STATES.get(movie["profile_status"],"待核对"),
                      learning,GROUPS.get(movie["mode"],movie["mode"]),f"{movie['reviewed_count']} / {movie['frame_count']}")
            tag = "missing" if not movie["source_available"] else "attention" if movie["profile_status"]!="confirmed" else "ready"
            if self.tree.exists(iid):
                if tuple(self.tree.item(iid,"values"))!=tuple(str(value) for value in values):
                    self.tree.item(iid,values=values,tags=(tag,))
                self.tree.move(iid,"",index)
            else:
                self.tree.insert("",index,iid=iid,values=values,tags=(tag,))
        needs = sum(movie["profile_status"]!="confirmed" for movie in self._movies)
        learned = sum(self.usage.get(movie["id"],{}).get("completed",0)>0 for movie in self._movies)
        missing = sum(not movie["source_available"] for movie in self._movies)
        self.summary.set(f"{len(self._movies)} 部影片    ·    待核对资料 {needs}    ·    已学影片 {learned}    ·    原片不可用 {missing}")
        if visible:
            self.empty.place_forget()
        else:
            self.empty.configure(text=("还没有影片资源\n\n添加在线视频，或导入已有的本地影片。" if not self._movies else
                                       "没有符合条件的影片\n\n试试其他关键词或切换筛选条件。"))
            self.empty.place(relx=.5,rely=.48,anchor="center")
        self._selection()

    def _selected(self):
        return [self._by_id[iid] for iid in self.tree.selection() if iid in self._by_id]

    def _selection(self):
        movies = self._selected()
        for key in ("profiles","learn"):
            self.action_buttons[key].state(["!disabled" if movies else "disabled"])
        for key in ("annotate","subtitles"):
            self.action_buttons[key].state(["!disabled" if len(movies)==1 else "disabled"])
        if not movies:
            self.details.set("选择影片查看资料与学习情况。缺少原片仍可修改资料和已有标注；重新导入同一影片可恢复原片定位。")
        elif len(movies)>1:
            missing = sum(not movie["source_available"] for movie in movies)
            self.details.set(f"已选择 {len(movies)} 部影片 · {missing} 部原片不可用\n可一起准备影片资料、提交电影学习；人工校正与字幕处理请单选一部。")
        else:
            movie = movies[0]
            usage = self.usage.get(movie["id"],{})
            source = movie.get("source_host") or movie["source_kind"]
            self.details.set(f"{movie['title']} · {source} · {movie['source_status']} · 已完成学习 {usage.get('completed',0)} 批\n"
                             f"已确认截图 {movie['reviewed_count']} / {movie['frame_count']} · 可补充资料后再次学习\n"
                             f"原片：{movie.get('source_path') or '尚未定位；重新导入同一影片即可定位'}")

    def _detail_changed(self):
        if self.closed:
            return
        text = self.details.get()
        if self.detail_text.get("1.0","end-1c")!=text:
            self.detail_text.configure(state="normal")
            self.detail_text.delete("1.0","end")
            self.detail_text.insert("1.0",text)
            self.detail_text.configure(state="disabled")

    def _status_changed(self):
        if self.closed:
            return
        self.status_text.configure(state="normal")
        self.status_text.delete("1.0","end")
        self.status_text.insert("1.0",self.status.get())
        self.status_text.configure(state="disabled")

    def _perform(self,callback):
        try:
            return callback()
        except Exception as exc:
            if not self.closed:
                self.status.set(str(exc))
                messagebox.showerror("影片资源",str(exc),parent=self.frame.winfo_toplevel())
            return False

    def _profiles(self):
        ids = [movie["id"] for movie in self._selected()]
        if ids:
            self._perform(lambda:self.on_profiles(ids))

    def _learn(self):
        movies = self._selected()
        if not movies:
            self.status.set("请先选择要学习的影片。")
            return
        missing = [movie["title"] for movie in movies if not source_status(movie)[0]]
        if missing:
            self.status.set("无法开始学习，以下影片原片不可用："+"、".join(missing[:4])+"。请重新导入同一影片定位，或恢复在线视频缓存。")
            self.refresh()
            return
        accepted = self._perform(lambda:self.on_learn([movie["id"] for movie in movies]))
        if accepted=="waiting-profiles":
            self.status.set("学习前请先核对演员、角色和剧情资料，确认后会继续。")
        elif accepted is False:
            self.status.set("当前任务暂不能接收学习请求，请稍后重试。")
        else:
            self.status.set(f"已提交 {len(movies)} 部影片的学习请求；可在训练中心查看进度。")

    def _single(self,callback):
        movies = self._selected()
        if len(movies)!=1:
            self.status.set("请只选择一部影片，再进入校正或字幕处理。")
            return
        if self._perform(lambda:callback(movies[0]["id"])) is False:
            self.status.set("当前任务尚未结束，请稍后重试。")

    def _select_all(self,event=None):
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def _menu(self,event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        self.tree.focus(iid)
        movies = self._selected()
        menu = tk.Menu(self.frame,tearoff=False)
        menu.add_command(label="准备 / 修改影片资料",command=self._profiles)
        menu.add_command(label="学习所选影片",command=self._learn)
        menu.add_separator()
        menu.add_command(label="人工校正已有截图",command=lambda:self._single(self.on_annotate),state="normal" if len(movies)==1 else "disabled")
        menu.add_command(label="字幕 / 剧本纠错",command=lambda:self._single(self.on_subtitles),state="normal" if len(movies)==1 else "disabled")
        menu.add_command(label="查看训练结果",command=lambda:self._perform(self.on_training))
        menu.add_separator()
        menu.add_command(label="重新导入 / 定位原片",command=lambda:self._perform(self.on_import))
        try:
            menu.tk_popup(event.x_root,event.y_root)
        finally:
            menu.grab_release()

    def show(self):
        self.refresh()

    def _destroyed(self,event):
        if event.widget==self.frame:
            self._finish_close()

    def _finish_close(self):
        if self.closed:
            return
        self.closed = True
        self.usage_index.close()
        if self._timer is not None:
            try:
                self.frame.after_cancel(self._timer)
            except tk.TclError:
                pass
            self._timer = None

    def close(self):
        if not self.closed:
            self._finish_close()
            self.frame.destroy()
