import io
import json
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import uuid

from PIL import Image, ImageTk
import theme
from dataset import connect, role_names
from tracking import assign_role, film_tracks, films_with_tracks


class RoleReview:
    PAGE = 6

    def __init__(self,parent,root):
        self.root = root
        self.films = films_with_tracks(root)
        if not self.films:
            messagebox.showinfo("角色确认","先点击「分析片段」，选择电影和人物出现的时段。",parent=parent)
            return
        self.win,panel = theme.sheet(parent,"角色确认")
        self.win.grab_set()
        self.selected = set()
        self.anchor = None
        self.page = 0
        self.images = []
        head = ttk.Frame(panel)
        head.pack(fill="x")
        self.film = ttk.Combobox(head,state="readonly",width=38,
                                 values=[f"{f['name']}  ·  {f['id']}" for f in self.films])
        self.film.pack(side="left",padx=4)
        self.film.current(len(self.films)-1)
        self.film.bind("<<ComboboxSelected>>",lambda _:self.load())
        self.scope = ttk.Combobox(head,state="readonly",values=["全部轨迹","待确认","已命名"],width=10)
        self.scope.current(0)
        self.scope.pack(side="left")
        self.scope.bind("<<ComboboxSelected>>",lambda _:self.load())
        ttk.Button(head,text="导出已确认轨迹",command=self.export).pack(side="right")
        ttk.Label(panel,text="勾选同一角色的轨迹后统一命名。",style="Muted.TLabel").pack(anchor="w",pady=8)
        self.body = ttk.Frame(panel)
        self.body.pack(fill="both",expand=True)
        nav = ttk.Frame(panel,padding=(0,8))
        nav.pack(fill="x")
        ttk.Button(nav,text="上一页",command=lambda:self.turn(-1)).pack(side="left")
        ttk.Button(nav,text="下一页",command=lambda:self.turn(1)).pack(side="left")
        self.summary = tk.StringVar()
        ttk.Label(nav,textvariable=self.summary).pack(side="left",padx=8)
        foot = ttk.Frame(panel)
        foot.pack(fill="x")
        ttk.Label(foot,text="本片角色").pack(side="left")
        self.name = ttk.Combobox(foot,width=22)
        self.name.pack(side="left",padx=4)
        ttk.Button(foot,text="确认所选轨迹",command=self.apply,style="Primary.TButton").pack(side="left")
        ttk.Button(foot,text="撤回所选角色名",command=lambda:self.apply(clear=True)).pack(side="left",padx=4)
        self.load()

    def load(self):
        self.movie = self.films[self.film.current()]["id"]
        rows = film_tracks(self.root,self.movie)
        self.name["values"] = role_names(self.root,self.movie)
        self.name.set("")
        self.rows = [r for r in rows if self.scope.current()==0 or
                     bool(r["role"])==(self.scope.current()==2)]
        self.selected.clear()
        self.anchor = None
        self.page = 0
        self.draw()

    def turn(self,delta):
        self.page = max(0,min(self.page+delta,(len(self.rows)-1)//self.PAGE))
        self.draw()

    def toggle(self,idx,value):
        if value:
            self.selected.add(idx)
        else:
            self.selected.discard(idx)
        self.counter()

    def counter(self):
        self.summary.set(f"{self.page+1}/{max(1,(len(self.rows)+self.PAGE-1)//self.PAGE)} 页  ·  已选 {len(self.selected)} 条")

    def similar(self,ref):
        self.anchor = ref["id"]
        self.selected.add(ref["id"])
        def score(row):
            # Simultaneously visible people are poor candidates for one identity.
            concurrent = (ref["clip"]==row["clip"] and ref["points"][0][0]<=row["points"][-1][0]
                          and row["points"][0][0]<=ref["points"][-1][0])
            similarity = sum(x*y for x,y in zip(ref["feature"],row["feature"]))
            return (row["id"]==ref["id"],not concurrent,similarity)
        self.rows.sort(key=score,reverse=True)
        self.page = 0
        self.draw()

    def draw(self):
        for w in self.body.winfo_children():
            w.destroy()
        self.images.clear()
        for i,r in enumerate(self.rows[self.page*self.PAGE:(self.page+1)*self.PAGE]):
            outline = ttk.Frame(self.body,style="Outline.TFrame",padding=1)
            outline.grid(row=i//3,column=i%3,sticky="nsew",padx=3,pady=3)
            card = ttk.Frame(outline,padding=7)
            card.pack(fill="both",expand=True)
            with Image.open(io.BytesIO(r["preview"])) as im:
                pic = ImageTk.PhotoImage(im)
            self.images.append(pic)
            selected = tk.BooleanVar(card,r["id"] in self.selected)
            check = ttk.Checkbutton(card,text=r["role"] or "待确认",variable=selected,
                                    command=lambda idx=r["id"],v=selected:self.toggle(idx,v.get()))
            check.var = selected
            check.pack(anchor="w")
            preview = ttk.Label(card,image=pic)
            preview.pack()
            preview.bind("<Button-1>",lambda e,c=check:c.invoke())
            pts = r["points"]
            ttk.Label(card,text=f"#{r['id']}   {pts[0][0]/1000:.2f}–{pts[-1][0]/1000:.2f} 秒 · {len(pts)} 帧").pack()
            ttk.Button(card,text="参考轨迹" if r["id"]==self.anchor else "按此外观找相似",
                       command=lambda row=r:self.similar(row)).pack()
        if not self.rows:
            ttk.Label(self.body,text="当前筛选没有轨迹",padding=20).pack()
        self.counter()

    def apply(self,clear=False):
        name = "" if clear else self.name.get().strip()
        if not self.selected or (not clear and not name):
            messagebox.showinfo("角色确认","请先勾选轨迹，并在命名时填写或选择本片角色。",parent=self.win)
            return
        assign_role(self.root,self.movie,self.selected,name)
        self.load()

    def export(self):
        rows = [r for r in film_tracks(self.root,self.movie) if r["role"]]
        if not rows:
            messagebox.showinfo("导出轨迹","这部电影还没有已确认角色。",parent=self.win)
            return
        target = filedialog.asksaveasfilename(parent=self.win,defaultextension=".json",
                                              filetypes=[("角色轨迹","*.json")])
        if not target:
            return
        with connect(self.root) as db:
            film = dict(db.execute("SELECT name,digest FROM movies WHERE id=?",(self.movie,)).fetchone())
        data = {"version":1,"movie":film,"columns":["time_ms","x1","y1","x2","y2"],
                "tracks":[{"track":r["id"],"clip":r["clip"],"shot":r["shot"],"role":r["role"],
                           "points":r["points"]} for r in rows]}
        target = Path(target)
        tmp = target.with_name(uuid.uuid4().hex+".tmp")
        try:
            tmp.write_text(json.dumps(data,ensure_ascii=False),encoding="utf-8")
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)
        messagebox.showinfo("导出轨迹",f"已导出 {len(rows)} 条已确认轨迹。",parent=self.win)
