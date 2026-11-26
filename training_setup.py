"""Choose full-movie learning or the existing screenshot detector task."""
import math
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import theme


def training_setup(parent, movies, current, weights, *, initial=None, movie_only=False):
    initial = initial or {}
    win, body = theme.sheet(parent, "电影学习设置" if movie_only else "开始后台训练", context="整部电影 · 连续学习")
    win.resizable(False, False)
    task = tk.StringVar(win, "整部电影时序学习")
    scope = tk.StringVar(win, "当前电影" if initial.get("movie_ids") == [current] and current is not None else "工程内全部电影")
    fps = tk.StringVar(win, str(initial.get("sample_fps", 1)))
    epochs = tk.StringVar(win, str(initial.get("epochs", 30)))
    device = tk.StringVar(win, {"":"自动", "cpu":"CPU", "0":"GPU 0"}.get(initial.get("device", ""), "自动"))
    weight = tk.StringVar(win, initial.get("weights", weights))
    people = tk.BooleanVar(win, bool(initial.get("weights", weights)))
    patience = tk.StringVar(win, str(initial.get("patience", 5)))
    learning_rate = tk.StringVar(win, str(initial.get("learning_rate", .001)))
    restart = tk.StringVar(win, "从头重新训练" if initial.get("restart") else "继续上一兼容模型")
    defaults = (initial.get("epochs", 30), initial.get("patience", 5))
    preset_name = {(5, 3): "快速检查 · 5 轮", (30, 5): "标准学习 · 30 轮", (80, 8): "深入学习 · 80 轮"}.get(defaults, "自定义")
    preset = tk.StringVar(win, preset_name if initial.get("learning_rate", .001) == .001 else "自定义")
    action_label = tk.StringVar(win)
    result = {}
    ttk.Label(body, text="直接从电影学习", style="Section.TLabel").pack(anchor="w")
    hint = ttk.Label(body, text="", style="Muted.TLabel", wraplength=510)
    hint.pack(anchor="w", pady=(6, 12))
    fields = ttk.Frame(body)
    fields.pack(fill="x")
    fields.columnconfigure(1, weight=1)

    def entry(row, title, variable, values=None):
        label = ttk.Label(fields, text=title)
        label.grid(row=row, column=0, sticky="w", pady=3, padx=(0, 14))
        widget = (ttk.Combobox(fields, textvariable=variable, values=values, state="readonly", width=34)
                  if values else ttk.Entry(fields, textvariable=variable, width=36))
        widget.grid(row=row, column=1, sticky="ew", pady=3)
        return label, widget

    task_label, task_picker = entry(0, "训练任务", task, ["整部电影时序学习", "截图检测与台词检索"])
    if movie_only:
        task_label.grid_remove()
        task_picker.grid_remove()
    scopes = ["工程内全部电影"] + (["当前电影"] if current is not None else [])
    scope_row = entry(1, "学习范围", scope, scopes)
    fps_row = entry(2, "每秒采样帧数", fps, ["0.25", "0.5", "1", "2", "4"])
    epoch_label, _ = entry(3, "最大训练轮数", epochs)
    entry(4, "训练设备", device, ["自动", "CPU", "GPU 0"])
    checkbox = ttk.Checkbutton(fields, text="分析人物数量与画面分布", variable=people)
    checkbox.grid(row=5, column=0, columnspan=2, sticky="w", pady=(9, 5))
    weight_label, _ = entry(6, "人物检测权重", weight)
    def browse():
        path = filedialog.askopenfilename(parent=win, title="选择本地检测权重", filetypes=[("YOLO 权重", "*.pt")])
        if path:
            weight.set(path)
    ttk.Button(fields, text="浏览", command=browse).grid(row=6, column=2, padx=(6, 0))
    preset_row = entry(7, "训练预设", preset, ["快速检查 · 5 轮", "标准学习 · 30 轮", "深入学习 · 80 轮", "自定义"])
    patience_row = entry(8, "未改善停止轮数", patience)
    rate_row = entry(9, "初始学习率", learning_rate)
    restart_row = entry(10, "已有模型", restart, ["继续上一兼容模型", "从头重新训练"])

    def apply_preset(*_):
        choices = {"快速检查 · 5 轮": (5, 3), "标准学习 · 30 轮": (30, 5), "深入学习 · 80 轮": (80, 8)}
        if preset.get() in choices:
            count, wait = choices[preset.get()]
            epochs.set(str(count))
            patience.set(str(wait))
            learning_rate.set("0.001")
    preset_row[1].bind("<<ComboboxSelected>>", apply_preset)
    note = ttk.Label(body, text="", style="Caption.TLabel", wraplength=510)
    note.pack(anchor="w", pady=(12, 7))

    previous_task = [task.get()]
    def changed(*_):
        movie = task.get() == "整部电影时序学习"
        action_label.set("保存学习设置" if movie_only else "准备资料并开始训练" if movie else "开始后台训练")
        for widget in (*scope_row, *fps_row, checkbox, *preset_row, *patience_row, *rate_row, *restart_row):
            widget.grid() if movie else widget.grid_remove()
        if previous_task[0] != task.get():
            epochs.set("30" if movie else "5")
            previous_task[0] = task.get()
        epoch_label.configure(text="最大训练轮数" if movie else "训练轮数")
        weight_label.configure(text="人物检测权重" if movie else "目标检测初始权重")
        hint.configure(text=("读取整部电影的画面、声音和字幕。不限角色人数，包含无对白与无人画面，无需先标注截图。"
                             if movie else "使用已确认截图训练辅助识别，并建立人物与台词检索索引。"))
        note.configure(text=("5 轮适合检查流程，30/80 是上限，未改善时自动停止并保留最佳模型。先确认影片资料；资料作为额外条件，不代表剧情理解或动画生成。"
                             if movie else "只有已确认且未删除的截图参与训练。"))
    task_picker.bind("<<ComboboxSelected>>", changed)
    changed()

    def submit():
        try:
            count = int(epochs.get())
            rate = float(fps.get())
            movie = task.get() == "整部电影时序学习"
            wait, lr = (int(patience.get()), float(learning_rate.get())) if movie else (5, .001)
            if not 1 <= count <= 500:
                raise ValueError("训练轮数须在 1–500 之间")
            if movie and (not math.isfinite(rate) or not .25 <= rate <= 4):
                raise ValueError("每秒采样帧数须在 0.25–4 之间")
            if movie and not movies and not movie_only:
                raise ValueError("请先导入电影")
            if movie and not 1 <= wait <= 100:
                raise ValueError("未改善停止轮数须在 1–100 之间")
            if movie and (not math.isfinite(lr) or not 1e-6 <= lr <= .1):
                raise ValueError("初始学习率须在 0.000001–0.1 之间")
            chosen_weight = weight.get().strip() if not movie or people.get() else ""
            if (not movie or people.get()) and not Path(chosen_weight).is_file():
                raise ValueError("请选择存在的本地检测权重，或取消人物分布分析")
            result.update(kind="movie-temporal" if movie else "annotations", epochs=count,
                          sample_fps=rate, device={"自动":"", "CPU":"cpu", "GPU 0":"0"}[device.get()],
                          weights=chosen_weight, movie_ids=[current] if scope.get()=="当前电影" else None,
                          patience=wait, learning_rate=lr, restart=restart.get()=="从头重新训练")
        except (ValueError, OSError) as exc:
            messagebox.showerror("训练设置", str(exc), parent=win)
            return
        win.destroy()
    actions = ttk.Frame(body)
    actions.pack(fill="x", pady=(8, 0))
    ttk.Button(actions, textvariable=action_label, style="Primary.TButton", command=submit).pack(side="right")
    ttk.Button(actions, text="取消", command=win.destroy).pack(side="right", padx=8)
    win.transient(parent)
    win.grab_set()
    parent.wait_window(win)
    return result or None
