import json
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

import dataset
import theme
from scripts import candidates as script_candidates, normalized
from subtitles import candidates, correct_cue, ocr_frame


KINDS = {"画外人物":"offscreen","旁白":"narration","来源未确定":"unknown"}
NEARBY_MS = 5000


def cue_time(ms):
    seconds,millis = divmod(ms,1000)
    minutes,seconds = divmod(seconds,60)
    return f"{minutes:02}:{seconds:02}.{millis:03}"


def cue_key(cue):
    return cue["track_id"],cue["start_ms"],cue["end_ms"],cue["original_text"],cue["text"]


def confirmed_speakers(root,movie,captions):
    if not captions:
        return {}
    def key(cue):
        return (cue.get("source"),cue.get("start_ms"),cue.get("end_ms"),normalized(cue["text"]))
    keys = {key(cue):cue_key(cue) for cue in captions}
    speakers = {}
    start = min(c["start_ms"] for c in captions)-NEARBY_MS
    end = max(c["end_ms"] for c in captions)+NEARBY_MS
    with dataset.connect(root) as db:
        rows = db.execute("""SELECT boxes,voices FROM frames WHERE movie=? AND deleted=0 AND state='reviewed'
                            AND ms BETWEEN ? AND ?""",(movie,start,end))
        for row in rows:
            notes = [box[5] for box in json.loads(row["boxes"]) if len(box)>5]
            notes.extend(json.loads(row["voices"]))
            for note in notes:
                role = note.get("role","").strip()
                if not role:
                    continue
                for ref in note.get("subtitle_refs",[]):
                    if ref.get("text") in note.get("lines",[]) and (caption := keys.get(key(ref))):
                        speakers.setdefault(caption,set()).add(role)
    return speakers


def subtitle_choices(root,frame):
    ms = frame["ms"]
    cues = candidates(root,frame["movie"],ms)
    nearby = not cues
    if nearby:
        cues = candidates(root,frame["movie"],ms,nearby_ms=NEARBY_MS)
        cues.sort(key=lambda cue:min(abs(cue["start_ms"]-ms),abs(cue["end_ms"]-ms)))
    captions = []
    for cue in cues:
        parts = [text.strip() for text in cue["text"].splitlines() if text.strip()]
        for text in parts:
            caption = {**cue,"text":text,"cue_text":cue["text"]}
            if len(parts)>1:
                caption["full_text"] = cue["text"]
            captions.append(caption)
    matches = script_candidates(root,frame["movie"],ms,captions)
    speakers = {cue_key(s["subtitle"]):s["role"] for s in matches if "subtitle" in s}
    confirmed = confirmed_speakers(root,frame["movie"],captions)
    for key,roles in confirmed.items():
        speakers[key] = next(iter(roles)) if len(roles)==1 else ""
    aligned = {}
    for caption in captions:
        key = cue_key(caption)
        role = speakers.get(key,"")
        if key not in confirmed and len(normalized(caption["text"]))<3:
            role = ""
        aligned.setdefault(key[1:3],{}).setdefault(key[0],set()).add(role)
    for caption in captions:
        key = cue_key(caption)
        if speakers.get(key) or key in confirmed:
            continue
        tracks = aligned.get(key[1:3],{})
        roles = set().union(*tracks.values())-{""}
        if len(roles)==1 and any(track!=key[0] and known==roles for track,known in tracks.items()):
            speakers[key] = next(iter(roles))
    choices = [{"label":s["label"],"text":s["line"],"script":s,
                "role":speakers[cue_key(s["subtitle"])] if "subtitle" in s else s["role"]} for s in matches]
    for caption in captions:
        source = {k:v for k,v in caption.items() if k!="full_text"}
        choices.append({"label":caption["label"],"text":caption["text"],"subtitle":source,
                        "role":speakers.get(cue_key(caption),"")})
    hint = ""
    if nearby:
        hint = ("本帧无正在显示的字幕，已列出前后 5 秒内的字幕。" if cues else
                "当前截图附近无文字字幕。可导入字幕文件，或识别画面底部字幕。")
    return choices,hint


def source_caption(choice):
    return choice.get("subtitle") or choice.get("script",{}).get("subtitle") or {}


def edit_subtitle(parent,root,movie,cue):
    dlg,panel = theme.sheet(parent,"纠正字幕",context=f"{cue_time(cue['start_ms'])}–{cue_time(cue['end_ms'])}")
    ttk.Label(panel,text=cue["label"],style="Muted.TLabel").pack(anchor="w",pady=(0,4))
    ttk.Label(panel,text="修正保存在工程中。已绑定的台词可在标注窗口里单独修改。",style="Caption.TLabel").pack(anchor="w")
    editor = tk.Text(panel,width=48,height=5,wrap="word",undo=True,font=(theme.FONT,11))
    editor.pack(fill="both",expand=True,pady=(8,0))
    editor.insert("1.0",cue["cue_text"])
    editor.edit_reset()
    saved = False

    def save():
        nonlocal saved
        try:
            correct_cue(root,movie,cue,editor.get("1.0","end-1c"))
        except ValueError as exc:
            messagebox.showinfo("纠正字幕",str(exc),parent=dlg)
            return
        saved = True
        dlg.destroy()

    buttons = ttk.Frame(panel)
    buttons.pack(fill="x",pady=(10,0))
    ttk.Button(buttons,text="保存修正",command=save,style="Primary.TButton").pack(side="right")
    ttk.Button(buttons,text="取消",command=dlg.destroy,style="Compact.TButton").pack(side="right",padx=8)
    dlg.bind("<Escape>",lambda _:dlg.destroy())
    dlg.bind("<Control-Return>",lambda _:save())
    previous = parent.grab_current()
    dlg.tk.call("::tk::PlaceWindow",dlg,"widget",parent)
    dlg.grab_set()
    editor.focus_set()
    parent.wait_window(dlg)
    if previous and previous.winfo_exists():
        previous.grab_set()
    return saved


def edit_cast(parent,root,movie):
    dlg,body = theme.sheet(parent,"演员表",context=movie["name"])
    ttk.Label(body,text="每行输入一个角色名。",style="Muted.TLabel").pack(anchor="w",pady=(0,8))
    editor = ttk.Frame(body)
    editor.pack(fill="both",expand=True)
    names = tk.Text(editor,width=46,height=12,wrap="word",font=(theme.FONT,11),undo=True)
    scroll = ttk.Scrollbar(editor,orient="vertical",command=names.yview)
    names.configure(yscrollcommand=scroll.set)
    scroll.pack(side="right",fill="y")
    names.pack(side="left",fill="both",expand=True)
    names.insert("1.0","\n".join(dataset.cast_names(root,movie["id"])))
    names.edit_reset()
    saved = None

    def save():
        nonlocal saved
        try:
            saved = dataset.save_cast(root,movie["id"],names.get("1.0","end-1c"))
        except ValueError as exc:
            messagebox.showerror("演员表",str(exc),parent=dlg)
            return
        dlg.destroy()

    actions = ttk.Frame(body)
    actions.pack(fill="x",pady=(8,0))
    ttk.Button(actions,text="保存演员表",command=save,style="Primary.TButton").pack(side="right")
    ttk.Button(actions,text="取消",command=dlg.destroy).pack(side="right",padx=6)
    dlg.bind("<Control-Return>",lambda _:save())
    dlg.bind("<Escape>",lambda _:dlg.destroy())
    dlg.grab_set()
    names.focus_set()
    parent.wait_window(dlg)
    return saved


def edit_note(parent,root,frame,note,detached=False):
    title = "画外台词 / 旁白" if detached else "人物与台词"
    dlg,panel = theme.sheet(parent,title,context=f"{frame['ms']/1000:.3f} 秒")
    if detached:
        ttk.Label(panel,text="发言来源").pack(anchor="w")
        kind = ttk.Combobox(panel,values=list(KINDS),state="readonly")
        kind.current(list(KINDS.values()).index(note.get("kind","offscreen")))
        kind.pack(fill="x",pady=(2,6))
    role_header = ttk.Frame(panel)
    role_header.pack(fill="x")
    ttk.Label(role_header,text="本片角色名（未知可留空）" if detached else "本片角色名").pack(side="left")
    role_hint = tk.StringVar(dlg)
    ttk.Label(role_header,textvariable=role_hint,style="Caption.TLabel").pack(side="right")
    role_value = tk.StringVar(dlg,note.get("role",""))
    role = ttk.Combobox(panel,textvariable=role_value,values=dataset.role_names(root,frame["movie"]))
    role.pack(fill="x",pady=(4,12))
    auto_role = None

    def keep_role(event=None):
        nonlocal auto_role
        auto_role = None

    selection_area = ttk.Frame(panel)
    selection_area.pack(fill="both",expand=True)
    cast_panel = ttk.Frame(selection_area)
    cast_panel.pack(side="left",fill="y",padx=(0,12))
    ttk.Label(cast_panel,text="演员表",style="Section.TLabel").pack(anchor="w",pady=(0,6))
    cast_list = tk.Listbox(cast_panel,width=16,height=7,exportselection=False)
    cast_scroll = ttk.Scrollbar(cast_panel,orient="vertical",command=cast_list.yview)
    cast_list.configure(yscrollcommand=cast_scroll.set)
    cast_scroll.pack(side="right",fill="y")
    cast_list.pack(side="left",fill="both",expand=True)
    cast = dataset.cast_names(root,frame["movie"])
    if cast:
        cast_list.insert("end",*cast)
    else:
        cast_list.insert("end","尚未录入演员表")
        cast_list.configure(state="disabled")

    def select_role(event):
        selected = cast_list.curselection()
        if selected:
            keep_role()
            role_value.set(cast[selected[0]])

    def highlight_role(*_):
        if role_value.get()!=auto_role:
            keep_role()
        cast_list.selection_clear(0,"end")
        name = role_value.get().strip()
        if name:
            role_hint.set("")
        if name in cast:
            idx = cast.index(name)
            cast_list.selection_set(idx)
            cast_list.see(idx)

    cast_list.bind("<<ListboxSelect>>",select_role)
    role_value.trace_add("write",highlight_role)
    role.bind("<<ComboboxSelected>>",keep_role)
    highlight_role()

    caption_panel = ttk.Frame(selection_area)
    caption_panel.pack(side="left",fill="both",expand=True)
    choices,hint = subtitle_choices(root,frame)
    subtitle_header = ttk.Frame(caption_panel)
    subtitle_header.pack(fill="x")
    ttk.Label(subtitle_header,text="可选字幕与剧本台词",style="Section.TLabel").pack(side="left")
    ttk.Label(subtitle_header,text="Ctrl 或 Shift 多选",style="Caption.TLabel").pack(side="right")
    if hint:
        ttk.Label(caption_panel,text=hint,style="Muted.TLabel").pack(anchor="w",pady=(3,4))
    table = ttk.Frame(caption_panel,style="Outline.TFrame",padding=1)
    table.pack(fill="both",expand=True,pady=(6,0))
    picker = ttk.Treeview(table,columns=("text","source","time","role"),show="headings",selectmode="extended",height=6)
    for column,label,width in [("text","台词",290),("source","来源",120),("time","时间",150),("role","建议角色",90)]:
        picker.heading(column,text=label,anchor="w")
        picker.column(column,width=width,minwidth=width//2,stretch=column=="text")
    scroll = ttk.Scrollbar(table,orient="vertical",command=picker.yview)
    picker.configure(yscrollcommand=scroll.set)
    scroll.pack(side="right",fill="y")
    picker.pack(side="left",fill="both",expand=True)

    def suggest_role(picked):
        nonlocal auto_role
        names = {choice["role"] for choice in picked if choice.get("role")}
        if auto_role is not None or not role_value.get().strip():
            suggested = next(iter(names)) if len(names)==1 else ""
            auto_role = suggested or None
            if role_value.get()!=suggested:
                role_value.set(suggested)
        hint = ""
        if picked and not role_value.get().strip():
            hint = "所选台词涉及不同角色，请选择或输入角色。" if len(names)>1 else "未匹配到角色，可从演员表选择或手动输入。"
        role_hint.set(hint)

    selected_text = {}

    def fill_lines(picked):
        wanted = dict.fromkeys(choice["text"] for choice in picked)
        if selected_text.keys()==wanted.keys():
            return
        lines.edit_separator()
        for text in selected_text.keys()-wanted.keys():
            tag = selected_text.pop(text)
            if tag is None:
                continue
            ranges = lines.tag_ranges(tag)
            if len(ranges)==2:
                first,last = ranges
                if (lines.get(first,last)==text and lines.compare(first,"==",f"{first} linestart")
                        and lines.compare(last,"==",f"{last} lineend")):
                    if lines.compare(last,"<","end-1c"):
                        lines.delete(first,f"{last}+1c")
                    elif lines.compare(first,">","1.0"):
                        lines.delete(f"{first}-1c",last)
                    else:
                        lines.delete(first,last)
            lines.tag_delete(tag)
        current = lines.get("1.0","end-1c")
        existing = {line.strip() for line in current.splitlines()}
        for text in wanted:
            if text in selected_text:
                continue
            if text in existing:
                selected_text[text] = None
                continue
            if current and not current.endswith("\n"):
                lines.insert("end-1c","\n")
            tag = f"auto_dialogue:{text}"
            lines.insert("end-1c",text,(tag,))
            selected_text[text] = tag
            current = text
        lines.edit_separator()

    def update_selection(event=None):
        selected = sorted(map(int,picker.selection()))
        picked = [choices[idx] for idx in selected]
        suggest_role(picked)
        fill_lines(picked)
        editable = len(selected)==1 and "track_id" in source_caption(choices[selected[0]])
        correct.configure(state="normal" if editable else "disabled")
        return picked

    def show_choices(start=0):
        for idx in range(start,len(choices)):
            choice = choices[idx]
            script = choice.get("script",{})
            source = source_caption(choice) or script
            timestamp = ""
            if source.get("start_ms") is not None:
                timestamp = f"{cue_time(source['start_ms'])}–{cue_time(source['end_ms'])}"
            elif "frame_ms" in source:
                timestamp = cue_time(source["frame_ms"])
            picker.insert("","end",iid=str(idx),values=(choice["text"],choice["label"],timestamp,choice.get("role","")))
        if start<len(choices):
            picker.see(str(start))
        update_selection()

    actions = ttk.Frame(panel)
    actions.pack(fill="x",pady=(6,3))
    feedback = tk.StringVar(dlg)

    def correct_selected():
        selected = picker.selection()
        if len(selected)!=1:
            return
        cue = source_caption(choices[int(selected[0])])
        if "track_id" not in cue or not edit_subtitle(dlg,root,frame["movie"],cue):
            return
        from_ocr = [choice for choice in choices if source_caption(choice).get("source")=="ocr"]
        choices[:] = subtitle_choices(root,frame)[0]+from_ocr
        picker.delete(*picker.get_children())
        show_choices()
        keys = ("track_id","start_ms","end_ms","original_text")
        for i,choice in enumerate(choices):
            caption = source_caption(choice)
            if all(caption.get(key)==cue[key] for key in keys):
                picker.selection_set(str(i))
                picker.see(str(i))
                break
        feedback.set("已保存字幕修正")

    correct = ttk.Button(actions,text="纠正所选字幕",command=correct_selected,state="disabled",style="Compact.TButton")
    correct.pack(side="left")
    picker.bind("<<TreeviewSelect>>",update_selection)
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
                cue = {"source":"ocr","label":"画面 OCR","frame_ms":frame["ms"],"text":text}
                match = next((s for s in script_candidates(root,frame["movie"],frame["ms"],[cue]) if "subtitle" in s),None)
                if match:
                    choice = {"label":match["label"],"text":match["line"],"script":match,"role":match["role"]}
                    if choice not in choices:
                        choices.append(choice)
                choice = {"label":cue["label"],"text":text,"subtitle":cue,"role":match["role"] if match else ""}
                if choice not in choices:
                    choices.append(choice)
            show_choices(idx)
            feedback.set(f"识别到 {len(words)} 行")
        threading.Thread(target=work,daemon=True).start()
        poll_id = dlg.after(120,poll)
    ocr = ttk.Button(actions,text="识别画面底部字幕",command=recognize,style="Compact.TButton")
    ocr.pack(side="left",padx=4)
    ttk.Label(actions,textvariable=feedback,style="Muted.TLabel").pack(side="left",padx=5)
    ttk.Label(panel,text="台词（每行一句，可直接修改）",style="Section.TLabel").pack(anchor="w",pady=(5,0))
    lines = tk.Text(panel,width=52,height=5,wrap="word",font=(theme.FONT,11),undo=True)
    lines.pack(fill="both",expand=True,pady=4)
    lines.insert("1.0","\n".join(note.get("lines",[])))
    lines.edit_reset()
    show_choices()
    result = None
    def save():
        nonlocal result
        picked = update_selection()
        name = role.get().strip()
        words = [s.strip() for s in lines.get("1.0","end-1c").splitlines() if s.strip()]
        if detached and not words:
            messagebox.showinfo("画外台词","请选择字幕或输入台词。",parent=dlg)
            return
        if not detached and words and not name:
            messagebox.showinfo("绑定台词","请填写本片角色名或代称。",parent=dlg)
            return
        if detached and not name and len({choice["role"] for choice in picked
                                          if choice.get("role") and choice["text"] in words})>1:
            messagebox.showinfo("绑定台词","所选台词涉及多个角色，请先选择或输入说话人。",parent=dlg)
            return
        refs = [r for r in note.get("subtitle_refs",[]) if r["text"] in words]
        script_refs = [r for r in note.get("script_refs",[]) if r["line"] in words]
        for cue in picked:
            if cue["text"] not in words:
                continue
            if script := cue.get("script"):
                ref = {key:value for key,value in script.items() if key!="subtitle"}
                if ref not in script_refs:
                    script_refs.append(ref)
            subtitle = {key:value for key,value in source_caption(cue).items() if key!="cue_text"}
            if subtitle and subtitle not in refs:
                refs.append(subtitle)
        result = {"role":name,"lines":words}
        if refs:
            result["subtitle_refs"] = refs
        if script_refs:
            result["script_refs"] = script_refs
        if detached:
            result["kind"] = KINDS[kind.get()]
        dlg.destroy()
    tail = ttk.Frame(panel)
    tail.pack(fill="x",pady=(8,0))
    ttk.Button(tail,text="保存台词" if detached else "保存到人物框",command=save,style="Primary.TButton").pack(side="right")
    ttk.Button(tail,text="取消",command=dlg.destroy,style="Compact.TButton").pack(side="right",padx=8)
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
    dlg,body = theme.sheet(parent,"画外台词 / 旁白",context=f"{frame['ms']/1000:.3f} 秒")
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
    for text,fn,style in (("新增发言",lambda:change(True),"Accent.TButton"),("编辑",change,"TButton"),
                          ("删除",remove,"Danger.TButton"),("完成",dlg.destroy,"Primary.TButton")):
        ttk.Button(bar,text=text,command=fn,style=style).pack(side="right" if text=="完成" else "left",padx=2)
    listing.bind("<Double-Button-1>",lambda _:change())
    refresh()
    dlg.grab_set()
    parent.wait_window(dlg)
