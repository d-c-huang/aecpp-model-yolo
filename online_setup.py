"""Online source settings for the existing local learning workflow."""
import tkinter as tk
from tkinter import messagebox, ttk

import theme


def online_setup(parent):
    win, body = theme.sheet(parent, "导入在线视频", context="网页链接 / 视频直链")
    win.resizable(False, False)
    result = {}
    url = tk.StringVar(win)
    mode = tk.StringVar(win, "按片内时段")
    quality = tk.StringVar(win, "720p")
    subtitles = tk.BooleanVar(win, True)
    learn = tk.BooleanVar(win, True)
    ttk.Label(body, text="粘贴视频链接", style="Section.TLabel").pack(anchor="w")
    entry = ttk.Entry(body, textvariable=url, width=66)
    entry.pack(fill="x", pady=(8, 6))
    ttk.Label(body, text="支持可解析的视频网页、MP4 和有限时长的 M3U8 等链接。视频先缓存到工程，之后可离线标注和学习。",
              style="Muted.TLabel", wraplength=530).pack(anchor="w", pady=(0, 14))
    fields = ttk.Frame(body)
    fields.pack(fill="x")
    for row, (title, variable, values) in enumerate([
        ("首次导入分组", mode, ["按片内时段", "整部训练", "整部验证"]),
        ("最高清晰度", quality, ["480p", "720p", "1080p", "原始"]),
    ]):
        ttk.Label(fields, text=title).grid(row=row, column=0, sticky="w", padx=(0, 18), pady=6)
        ttk.Combobox(fields, textvariable=variable, values=values, state="readonly", width=25).grid(
            row=row, column=1, sticky="ew", pady=6)
    ttk.Checkbutton(body, text="同时读取可用的中英文字幕", variable=subtitles).pack(anchor="w", pady=(12, 5))
    ttk.Checkbutton(body, text="导入后开始 / 投喂电影学习", variable=learn).pack(anchor="w", pady=5)
    ttk.Label(body, text="使用当前电影训练设置。已有训练任务时，新资源排队到后续训练；也可以取消勾选，仅导入素材。",
              style="Caption.TLabel", wraplength=530).pack(anchor="w", pady=(2, 10))

    def submit():
        try:
            from online_video import validate_url
            address = validate_url(url.get())
        except ValueError as exc:
            messagebox.showerror("视频链接", str(exc), parent=win)
            return
        result.update(url=address, mode={"按片内时段":"time", "整部训练":"train", "整部验证":"val"}[mode.get()],
                      height={"480p":480, "720p":720, "1080p":1080, "原始":0}[quality.get()],
                      subtitles=subtitles.get(), learn=learn.get())
        win.destroy()

    actions = ttk.Frame(body)
    actions.pack(fill="x", pady=(8, 0))
    ttk.Button(actions, text="导入在线视频", command=submit, style="Primary.TButton").pack(side="right")
    ttk.Button(actions, text="取消", command=win.destroy).pack(side="right", padx=8)
    entry.focus_set()
    win.transient(parent)
    win.grab_set()
    parent.wait_window(win)
    return result or None
