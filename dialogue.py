import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

import dataset
from subtitles import candidates, ocr_frame


KINDS = {"画外人物":"offscreen","旁白":"narration","来源未确定":"unknown"}


def edit_note(parent,root,frame,note,detached=False):
    dlg = tk.Toplevel(parent)
    dlg.title(f"{'无人物框台词' if detached else '人物与台词'} · {frame['ms']/1000:.2f} 秒")
    dlg.transient(parent)
    panel = ttk.Frame(dlg,padding=12)
    panel.pack(fill="both",expand=True)
    if detached:
        ttk.Label(panel,text="发言来源").pack(anchor="w")
        kind = ttk.Combobox(panel,values=list(KINDS),state="readonly")
        kind.current(list(KINDS.values()).index(note.get("kind","offscreen")))
        kind.pack(fill="x",pady=(2,6))
    ttk.Label(panel,text="本片角色名（未知可留空）" if detached else "本片角色名").pack(anchor="w")
    role = ttk.Combobox(panel,values=dataset.role_names(root,frame["movie"]))
    role.pack(fill="x",pady=(2,6))
    role.set(note.get("role",""))
    ttk.Label(panel,text="当前画面字幕候选").pack(anchor="w")
    choices = []
    for cue in candidates(root,frame["movie"],frame["ms"]):
        for text in cue["text"].splitlines():
            if text.strip():
                choices.append({**cue,"text":text.strip()})
    picker = ttk.Combobox(panel,state="readonly",width=64)
    picker.pack(fill="x")
    def show_choices(index=0):
        picker["values"] = [f"{c['label']}  ·  {c['text']}" for c in choices]
        if choices:
            picker.current(index)
        else:
            picker.set("此刻没有匹配的文字字幕")
    show_choices()
    actions = ttk.Frame(panel)
    actions.pack(fill="x",pady=3)
    refs = list(note.get("subtitle_refs",[]))
    def insert():
        idx = picker.current()
        if idx<0:
            return
        cue = choices[idx]
        current = lines.get("1.0","end-1c")
        if cue["text"] not in current.splitlines():
            lines.insert("end-1c",("\n" if current and not current.endswith("\n") else "")+cue["text"])
        if cue not in refs:
            refs.append(cue)
    ttk.Button(actions,text="采用所选字幕",command=insert).pack(side="left")
    feedback = tk.StringVar()
    poll_id = None
    def cancel_poll(event):
        if event.widget is dlg and poll_id is not None:
            dlg.after_cancel(poll_id)
    dlg.bind("<Destroy>",cancel_poll,add="+")
    def recognize():
        nonlocal poll_id
        ocr.configure(state="disabled")
        feedback.set("识别中…")
        result = queue.Queue()
        def work():
            try:
                result.put((ocr_frame(root/frame["path"]),None))
            except Exception as exc:
                result.put(([],str(exc)))
        def poll():
            nonlocal poll_id
            poll_id = None
            if result.empty():
                poll_id = dlg.after(120,poll)
                return
            words,error = result.get()
            ocr.configure(state="normal")
            if error:
                feedback.set("识别未完成")
                messagebox.showinfo("识别画面字幕",error,parent=dlg)
                return
            idx = len(choices)
            for text in words:
                choice = {"source":"ocr","label":"画面 OCR","frame_ms":frame["ms"],"text":text}
                if choice not in choices:
                    choices.append(choice)
            if choices:
                show_choices(min(idx,len(choices)-1))
            feedback.set(f"识别到 {len(words)} 行")
        threading.Thread(target=work,daemon=True).start()
        poll_id = dlg.after(120,poll)
    ocr = ttk.Button(actions,text="识别画面底部字幕",command=recognize)
    ocr.pack(side="left",padx=4)
    ttk.Label(actions,textvariable=feedback).pack(side="left")
    ttk.Label(panel,text="台词（每行一句）").pack(anchor="w")
    lines = tk.Text(panel,width=52,height=5,wrap="word",font=("Microsoft YaHei UI",11))
    lines.pack(fill="both",expand=True,pady=4)
    lines.insert("1.0","\n".join(note.get("lines",[])))
    result = None
    def save():
        nonlocal result
        name = role.get().strip()
        words = [s.strip() for s in lines.get("1.0","end-1c").splitlines() if s.strip()]
        if detached and not words:
            messagebox.showinfo("画外台词","请选择字幕或输入台词。",parent=dlg)
            return
        if not detached and words and not name:
            messagebox.showinfo("绑定台词","请填写本片角色名或代称。",parent=dlg)
            return
        result = {"role":name,"lines":words}
        matched = [r for r in refs if r["text"] in words]
        if matched:
            result["subtitle_refs"] = matched
        if detached:
            result["kind"] = KINDS[kind.get()]
        dlg.destroy()
    ttk.Button(panel,text="保存台词" if detached else "保存到人物框",command=save).pack(anchor="e",pady=3)
    dlg.bind("<Escape>",lambda e:dlg.destroy())
    dlg.bind("<Control-Return>",lambda e:save())
    prev = parent.grab_current()
    dlg.grab_set()
    role.focus_set()
    parent.wait_window(dlg)
    if prev and prev.winfo_exists():
        prev.grab_set()
    return result


def manage_voices(parent,root,frame,voices,save):
    dlg = tk.Toplevel(parent)
    dlg.title(f"画外台词 / 旁白 · {frame['ms']/1000:.2f} 秒")
    dlg.transient(parent)
    body = ttk.Frame(dlg,padding=10)
    body.pack(fill="both",expand=True)
    listing = tk.Listbox(body,width=65,height=6,exportselection=False)
    listing.pack(fill="both",expand=True)
    labels = {v:k for k,v in KINDS.items()}
    def refresh():
        listing.delete(0,"end")
        for entry in voices:
            listing.insert("end",f"{labels[entry['kind']]} · {entry['role'] or '未具名'}  {' / '.join(entry['lines'])}")
    def change(new=False):
        sel = listing.curselection()
        if not new and not sel:
            return
        note = edit_note(dlg,root,frame,{} if new else voices[sel[0]],True)
        if note is not None:
            if new:
                voices.append(note)
            else:
                voices[sel[0]] = note
            save()
            refresh()
    def remove():
        sel = listing.curselection()
        if sel:
            voices.pop(sel[0])
            save()
            refresh()
    bar = ttk.Frame(body,padding=(0,6))
    bar.pack(fill="x")
    for text,fn in [("新增发言",lambda:change(True)),("编辑",change),("删除",remove),("完成",dlg.destroy)]:
        ttk.Button(bar,text=text,command=fn).pack(side="left",padx=2)
    listing.bind("<Double-Button-1>",lambda _:change())
    refresh()
    dlg.grab_set()
    parent.wait_window(dlg)
