"""Colors and Tk styles for the annotation workspace."""
import tkinter as tk
from tkinter import font, ttk


BACKGROUND = "#11141a"
PANEL = "#1a1f27"
FIELD = "#141920"
RAISED = "#232b35"
BORDER = "#303b49"
TEXT = "#ebeff4"
MUTED = "#a7b1bf"
SOFT = "#8795a6"
ACCENT = "#f2b571"
ACCENT_HOVER = "#ffc88b"
INK = "#24180f"
SELECTED = "#403329"
DISABLED = "#657284"
RED = "#f2a793"
TEAL = "#a0d6c0"
BLUE = "#abc6ec"
MONITOR = "#0d1015"
FONT = "Microsoft YaHei UI"

STATE_COLORS = {
    "new": (MUTED, RAISED),
    "draft": (ACCENT, "#382e27"),
    "predicted": (BLUE, "#233245"),
    "reviewed": (TEAL, "#203830"),
    "empty": (SOFT, FIELD),
}


def apply_theme(win):
    win.configure(background=BACKGROUND)
    style = ttk.Style(win)
    style.theme_use("clam")
    for name,size in (("TkDefaultFont",10),("TkTextFont",11),("TkMenuFont",10)):
        font.nametofont(name,root=win).configure(family=FONT,size=size)
    style.configure(".",background=PANEL,foreground=TEXT,font=(FONT,10))
    style.configure("TFrame",background=PANEL)
    style.configure("Outline.TFrame",background=BORDER)
    style.configure("TLabel",background=PANEL,foreground=TEXT)
    style.configure("Title.TLabel",font=(FONT,17,"bold"))
    style.configure("Brand.TLabel",font=(FONT,22,"bold"),background=PANEL)
    style.configure("Section.TLabel",font=(FONT,11,"bold"))
    style.configure("Muted.TLabel",foreground=MUTED)
    style.configure("Caption.TLabel",foreground=SOFT,font=(FONT,9))
    style.configure("Accent.TLabel",foreground=ACCENT)
    style.configure("TSeparator",background=BORDER,bordercolor=BORDER,lightcolor=BORDER,darkcolor=BORDER)
    style.configure("Horizontal.TProgressbar",background=ACCENT,troughcolor=FIELD,
                    bordercolor=FIELD,lightcolor=ACCENT,darkcolor=ACCENT,thickness=7)
    style.configure("TPanedwindow",background=BACKGROUND)
    style.configure("Sash",sashthickness=9,gripcount=0,background=BACKGROUND)

    for name,bg,fg,edge,hover,pad in (
        ("TButton",RAISED,TEXT,BORDER,"#2e3845",(13,6)),
        ("Primary.TButton",ACCENT,INK,ACCENT,ACCENT_HOVER,(15,6)),
        ("Accent.TButton","#362a23",ACCENT,"#574235","#463426",(13,6)),
        ("Compact.TButton",PANEL,MUTED,BORDER,RAISED,(6,3)),
        ("Danger.TButton",PANEL,RED,BORDER,"#382522",(6,3)),
        ("TMenubutton",RAISED,TEXT,BORDER,"#2e3845",(10,6)),
    ):
        style.configure(name,background=bg,foreground=fg,bordercolor=edge,
                        lightcolor=bg,darkcolor=bg,focuscolor=ACCENT,borderwidth=1,padding=pad,width=0,
                        arrowcolor=MUTED,arrowsize=9,relief="flat")
        state_bg = [("disabled",PANEL),("pressed",hover),("active",hover)]
        style.map(name,background=state_bg,lightcolor=state_bg,darkcolor=state_bg,
                  foreground=[("disabled",DISABLED)],
                  bordercolor=[("disabled",BORDER),("focus",ACCENT),("active",edge)],
                  relief=[("pressed","flat"),("!pressed","flat")])

    for name in ("TEntry","TCombobox"):
        style.configure(name,fieldbackground=FIELD,background=FIELD,foreground=TEXT,
                        bordercolor=BORDER,lightcolor=FIELD,darkcolor=FIELD,insertcolor=TEXT,
                        arrowcolor=MUTED,arrowsize=10,selectbackground=SELECTED,selectforeground=TEXT,
                        padding=(8,5),borderwidth=1)
        style.map(name,fieldbackground=[("disabled",PANEL),("readonly",FIELD)],
                  foreground=[("disabled",DISABLED),("readonly",TEXT)],
                  bordercolor=[("focus",ACCENT),("active",SOFT)],
                  arrowcolor=[("disabled",DISABLED),("active",ACCENT)])

    height = font.nametofont("TkDefaultFont",root=win).metrics("linespace")
    style.configure("Treeview",background=FIELD,fieldbackground=FIELD,foreground=MUTED,
                    bordercolor=FIELD,lightcolor=FIELD,darkcolor=FIELD,
                    borderwidth=0,relief="flat",rowheight=height+11)
    style.map("Treeview",background=[("selected",SELECTED)],foreground=[("selected",TEXT)])
    style.configure("Treeview.Heading",background=RAISED,foreground=MUTED,relief="flat",
                    bordercolor=RAISED,lightcolor=RAISED,darkcolor=RAISED,padding=(6,6),font=(FONT,9,"bold"))
    style.map("Treeview.Heading",background=[("active",RAISED)])
    style.configure("TScrollbar",background=BORDER,troughcolor=FIELD,bordercolor=FIELD,
                    lightcolor=BORDER,darkcolor=BORDER,arrowcolor=SOFT,arrowsize=10,width=10)
    style.map("TScrollbar",background=[("active",SOFT)])
    style.configure("TCheckbutton",indicatorbackground=FIELD,indicatorforeground=ACCENT,
                    indicatorcolor=FIELD,indicatormargin=(0,0,6,0),focuscolor=ACCENT)
    style.map("TCheckbutton",background=[("active",PANEL)],foreground=[("disabled",DISABLED)])

    for state,(fg,bg) in STATE_COLORS.items():
        style.configure(f"{state}.Badge.TLabel",background=bg,foreground=fg,padding=(8,3),font=(FONT,9,"bold"))
    for state,color in (("idle",SOFT),("running",ACCENT),("ready",TEAL),("error",RED)):
        style.configure(f"{state}.Dot.TLabel",foreground=color,font=("Segoe UI",10))

    for widget in ("Text","Listbox","Entry","Menu"):
        for setting,value in (
            ("background",FIELD),("foreground",TEXT),("selectBackground",SELECTED),
            ("selectForeground",TEXT),("highlightBackground",BORDER),("highlightColor",ACCENT),
            ("highlightThickness",1),("borderWidth",0),("relief","flat"),
        ):
            win.option_add(f"*{widget}.{setting}",value)
    win.option_add("*Text.insertBackground",TEXT)
    win.option_add("*Text.padX",10)
    win.option_add("*Text.padY",7)
    win.option_add("*Listbox.disabledForeground",SOFT)
    win.option_add("*Menu.activeBackground",SELECTED)
    win.option_add("*Menu.activeForeground",TEXT)
    win.option_add("*Menu.disabledForeground",DISABLED)


def sheet(parent, title, *, context=""):
    window = tk.Toplevel(parent,background=PANEL)
    window.title(title)
    window.transient(parent)
    panel = ttk.Frame(window,padding=(20,16))
    panel.pack(fill="both",expand=True)
    head = ttk.Frame(panel)
    head.pack(fill="x")
    ttk.Label(head,text=title,style="Title.TLabel").pack(side="left")
    if context:
        ttk.Label(head,text=context,style="Muted.TLabel",wraplength=320).pack(side="right",padx=(16,0))
    ttk.Separator(panel).pack(fill="x",pady=(12,14))
    return window,panel
