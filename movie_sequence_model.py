"""Learn measurable film feature sequences, without a fixed cast or scene type.

This model forecasts numeric observations. It does not infer story meaning,
recognize semantic actions, or produce an AECPP animation plan.
"""
import hashlib
import json
import math
from pathlib import Path
import pickle
import zipfile

import numpy as np


FORMAT = "yingge-movie-temporal"
VERSION = 1
CONTEXT = 16
HIDDEN = 64
LIMITATIONS = [
    "学习画面、运动、声音、字幕和人物数量等观测特征的时间变化。",
    "字幕出现不等于实际开口，画面变化不等于已确认的切镜或动作。",
    "不识别剧情、具体动作、情绪或笑点，不直接生成动画时间轴。",
    "资料文本通过固定哈希作为额外条件，不具备语义理解；剧情条件验证不等同无剧情先验预测。",
    "需新增 AECPP 适配器，不能导入现有 aecpp-learning-bundle 接口。",
]


def _write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def _device(torch, value):
    value = str(value or "")
    if not value:
        return torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    if value == "cpu":
        return torch.device("cpu")
    index = value.removeprefix("cuda:")
    if index.isdigit() and torch.cuda.is_available() and int(index) < torch.cuda.device_count():
        return torch.device("cuda:" + str(int(index)))
    raise ValueError("电影时序训练设备无效或 GPU 不可用，请选择 CPU 或可用的 GPU")


def _network(torch, count, hidden=HIDDEN, context_count=0):
    class TemporalGRU(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.gru = torch.nn.GRU(count + context_count, hidden, batch_first=True)
            self.output = torch.nn.Linear(hidden, count)
            torch.nn.init.zeros_(self.output.weight)
            torch.nn.init.zeros_(self.output.bias)

        def forward(self, batch, lengths):
            packed = torch.nn.utils.rnn.pack_padded_sequence(
                batch, lengths.cpu(), batch_first=True, enforce_sorted=False)
            _, state = self.gru(packed)
            last = batch[torch.arange(batch.shape[0], device=batch.device), lengths.to(batch.device) - 1, :count]
            return last + self.output(state[-1])

    return TemporalGRU()


def _corpus(run):
    manifest = json.loads((run / "movie_corpus.json").read_text(encoding="utf-8"))
    names = manifest.get("feature_names", [])
    if (manifest.get("version") != 1 or not names or len(set(names)) != len(names)
            or not all(isinstance(name, str) and name for name in names)):
        raise ValueError("电影特征格式无效")
    with np.load(run / "movie_corpus.npz", allow_pickle=False) as data:
        features = np.asarray(data["features"], dtype=np.float32)
        times = np.asarray(data["times_ms"])
    if (features.ndim != 2 or features.shape[1] != len(names) or times.shape != (len(features),)
            or not np.isfinite(features).all() or not np.isfinite(times).all()):
        raise ValueError("电影特征或时间轴含无效数据")
    covered = np.zeros(len(features), dtype=bool)
    selected = {"train": [], "val": []}
    windows = {"train": [], "val": []}
    for sequence in manifest.get("sequences", []):
        start, length, split = sequence.get("offset"), sequence.get("length"), sequence.get("split")
        if (type(start) is not int or type(length) is not int or length < 1 or start < 0
                or start + length > len(features) or split not in selected):
            raise ValueError("电影片段范围或训练分组无效")
        end = start + length
        if covered[start:end].any() or (np.diff(times[start:end]) <= 0).any():
            raise ValueError("电影片段重复或时间顺序无效")
        covered[start:end] = True
        selected[split].append((start, end))
        for target in range(start + 1, end):
            windows[split].append((max(start, target - CONTEXT), target))
    if not covered.all():
        raise ValueError("电影特征存在未分组的样本")
    if not windows["train"]:
        raise ValueError("没有可训练的连续电影片段，每段至少需要两个采样时刻，且不能全部属于验证集")
    if _overlaps(_training_ranges(manifest), _training_ranges(manifest, "val")):
        raise ValueError("同一电影的训练和验证时段重叠")
    return manifest, features, selected, windows


def _context_spec(manifest):
    source = manifest.get("profile_context")
    if not source:
        return None
    from movie_context import NAMES, VERSION as CONTEXT_VERSION
    if source.get("version") != CONTEXT_VERSION or source.get("names") != NAMES:
        raise ValueError("影片资料条件编码格式不兼容")
    return {"version": CONTEXT_VERSION, "names": NAMES}


def _contexts(run, manifest, rows):
    spec = _context_spec(manifest)
    count = len(spec["names"]) if spec else 0
    with np.load(run / "movie_corpus.npz", allow_pickle=False) as stored:
        values = np.asarray(stored["profile_context"], dtype=np.float32) if "profile_context" in stored else np.zeros((rows, 0), dtype=np.float32)
    if values.shape != (rows, count) or not np.isfinite(values).all():
        raise ValueError("影片资料条件特征缺失、维度不符或含无效数值")
    return spec, values


def _standardize(features, selected):
    # No validation observations contribute to these statistics.
    rows = np.concatenate([features[start:end] for start, end in selected["train"]]).astype(np.float64)
    mean = rows.mean(axis=0).astype(np.float32)
    scale = np.maximum(rows.std(axis=0), .05).astype(np.float32)
    transformed = (features - mean) / scale
    if not np.isfinite(transformed).all():
        raise ValueError("电影特征数值范围过大")
    return mean, scale, transformed


def _loader(torch, values, windows, *, shuffle, target_count=None, sample_weights=None):
    target_count = values.shape[1] if target_count is None else target_count
    class Windows(torch.utils.data.Dataset):
        def __len__(self):
            return len(windows)

        def __getitem__(self, index):
            start, target = windows[index]
            return values[start:target], values[target, :target_count]

    def collate(items):
        sequences, targets = zip(*items)
        lengths = torch.tensor([len(sequence) for sequence in sequences], dtype=torch.long)
        return torch.nn.utils.rnn.pad_sequence(sequences, batch_first=True), lengths, torch.stack(targets)

    sampler = (torch.utils.data.WeightedRandomSampler(sample_weights, len(windows), replacement=True,
                                                     generator=torch.Generator().manual_seed(1729))
               if shuffle and sample_weights is not None else None)
    return torch.utils.data.DataLoader(Windows(), batch_size=128, shuffle=shuffle and sampler is None,
                                      sampler=sampler,
                                      collate_fn=collate, num_workers=0,
                                      generator=torch.Generator().manual_seed(1729))


def _balanced_weights(manifest, windows):
    """Give each training film equal expected mass; keep evaluation untouched."""
    from collections import Counter
    owner = {}
    for sequence in manifest["sequences"]:
        if sequence["split"] == "train":
            for target in range(sequence["offset"] + 1, sequence["offset"] + sequence["length"]):
                owner[target] = sequence["film_id"]
    counts = Counter(owner[target] for _, target in windows)
    if len(counts) < 2:
        return None
    return [1. / counts[owner[target]] for _, target in windows]


def _evaluate(torch, model, loader, device, *, persistence=False):
    count, huber, squared = 0, 0., 0.
    model.eval()
    with torch.inference_mode():
        for batch, lengths, target in loader:
            batch, target = batch.to(device), target.to(device)
            prediction = (batch[torch.arange(len(batch), device=device), lengths.to(device) - 1, :target.shape[-1]]
                          if persistence else model(batch, lengths))
            huber += float(torch.nn.functional.smooth_l1_loss(prediction, target, reduction="sum").item())
            squared += float(torch.nn.functional.mse_loss(prediction, target, reduction="sum").item())
            count += target.numel()
    return (huber / count, squared / count) if count else (None, None)


def _previous_checkpoint(previous):
    path = Path(previous)
    return path / "movie_temporal.pt" if path.is_dir() else path if path.suffix == ".pt" else path.parent / "movie_temporal.pt"


def _scaling(saved, count):
    mean, scale = np.asarray(saved["mean"], dtype=np.float32), np.asarray(saved["scale"], dtype=np.float32)
    if (mean.shape != (count,) or scale.shape != mean.shape or not np.isfinite(mean).all()
            or not np.isfinite(scale).all() or not (scale > 0).all()):
        raise ValueError("电影模型标准化参数无效")
    return mean, scale


def _training_ranges(manifest, split="train"):
    return _merge_ranges([{key: row[key] for key in ("film_id", "start_ms", "end_ms")}
                          for row in manifest.get("sequences", []) if row["split"] == split])


def _merge_ranges(ranges):
    if not isinstance(ranges, list):
        raise ValueError("训练来源时间区间格式无效")
    films = {}
    for row in ranges:
        if not isinstance(row, dict):
            raise ValueError("训练来源时间区间格式无效")
        film, start, end = row.get("film_id"), row.get("start_ms"), row.get("end_ms")
        if (not isinstance(film, str) or not film or type(start) is not int or type(end) is not int
                or not 0 <= start < end):
            raise ValueError("训练来源时间区间无效")
        films.setdefault(film, []).append((start, end))
    merged = []
    for film in sorted(films):
        intervals = []
        for start, end in sorted(films[film]):
            if intervals and start <= intervals[-1][1]:
                intervals[-1] = (intervals[-1][0], max(intervals[-1][1], end))
            else:
                intervals.append((start, end))
        merged.extend({"film_id": film, "start_ms": start, "end_ms": end} for start, end in intervals)
    return merged


def _overlaps(previous, validation):
    by_film = {}
    for row in previous:
        by_film.setdefault(row["film_id"], []).append(row)
    return any(max(row["start_ms"], old["start_ms"]) < min(row["end_ms"], old["end_ms"])
               for row in validation for old in by_film.get(row["film_id"], []))


def _improvement(model_loss, baseline_loss):
    return (100 * (baseline_loss - model_loss) / baseline_loss
            if model_loss is not None and baseline_loss is not None and baseline_loss > 1e-12 else None)


def _validation_kind(manifest, windows):
    train_films = {s["film_id"] for s in manifest["sequences"] if s["split"] == "train"}
    val_films = {s["film_id"] for s in manifest["sequences"] if s["split"] == "val" and s["length"] > 1}
    return ("independent-films" if val_films and not train_films.intersection(val_films)
            else "within-film" if windows["val"] else "none")


def train_movie(root, source_snapshot, epochs, device, report, *, run, sample_fps=1., weights=None, previous=None,
                patience=5, learning_rate=.001, restart=False):
    """Train a real next-observation model from a frozen source snapshot.

    A new run is always evaluated against persistence on its own held-out data.
    Compatible warm starts retain the previous training-only normalization and
    reject any overlap between historical training and current validation data.
    """
    if type(epochs) is not int or not 1 <= epochs <= 500:
        raise ValueError("训练轮数须在 1–500 之间")
    if type(patience) is not int or not 1 <= patience <= 100:
        raise ValueError("提前停止耐心轮数须在 1–100 之间")
    if (isinstance(learning_rate, bool) or not isinstance(learning_rate, (int, float))
            or not math.isfinite(learning_rate) or not 1e-6 <= learning_rate <= .1):
        raise ValueError("学习率须在 0.000001–0.1 之间")
    initial_learning_rate = float(learning_rate)
    if restart:
        previous = None
    if not isinstance(sample_fps, (int, float)) or not math.isfinite(sample_fps) or not .25 <= sample_fps <= 4:
        raise ValueError("电影采样频率须在 0.25–4 帧/秒之间")
    import torch
    from movie_sequence_data import prepare_corpus

    target_device = _device(torch, device)
    if target_device.type == "cpu":
        torch.set_num_threads(min(4, torch.get_num_threads()))
    torch.manual_seed(1729)
    if target_device.type == "cuda":
        torch.cuda.manual_seed_all(1729)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False
    root, run = Path(root), Path(run)
    if (run / "completed.json").exists():
        raise ValueError("此训练目录已经完成，请创建新的训练批次")
    run.mkdir(parents=True, exist_ok=True)
    _write_json(run / "movie_sources.json", source_snapshot)
    report("正在读取整部电影，训练不限制人物数量，也不要求有对白")
    prepare_corpus(root, source_snapshot, run, sample_fps, report, weights=weights, device=str(target_device))
    manifest, features, selected, windows = _corpus(run)
    names = manifest["feature_names"]
    context_spec, contexts = _contexts(run, manifest, len(features))
    context_count = contexts.shape[1]
    extraction = {"extractor_version": manifest.get("extractor_version"),
                  "weights_sha256": manifest.get("weights_sha256")}
    fingerprint = hashlib.sha256(json.dumps({"source": source_snapshot, "sample_fps": sample_fps,
                                             "extraction": extraction, "version": VERSION},
                                            ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    model = _network(torch, len(names), context_count=context_count).to(target_device)
    warm_start = False
    warm_start_reason = "已选择重新训练，使用当前训练集重新拟合" if restart else "未指定上一模型，使用当前训练集拟合"
    history = []
    if previous:
        checkpoint = _previous_checkpoint(previous)
        warm_start_reason = "找不到上一模型，本批重新拟合"
        if checkpoint.is_file():
            try:
                saved = torch.load(checkpoint, map_location="cpu", weights_only=True)
                compatible = (saved.get("format") == FORMAT and saved.get("version") == VERSION
                              and saved.get("feature_names") == names and saved.get("hidden_size") == HIDDEN
                              and saved.get("sample_fps") == float(sample_fps)
                              and saved.get("extraction") == extraction
                              and saved.get("context_steps") == CONTEXT)
                if not compatible:
                    raise ValueError("电影特征、采样率或检测权重不同")
                old_context = saved.get("profile_context")
                if old_context is not None and old_context != context_spec:
                    raise ValueError("影片资料条件编码不同")
                if "training_ranges" in saved:
                    history = _merge_ranges(saved["training_ranges"])
                else:
                    # Older checkpoints stored this provenance only in their corpus.
                    source_manifest = checkpoint.parent / "movie_corpus.json"
                    if not source_manifest.is_file():
                        raise ValueError("缺少上一模型的训练来源，无法确认验证隔离")
                    old_manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
                    history = _training_ranges(old_manifest)
                if not history:
                    raise ValueError("缺少上一模型的训练来源时间区间")
                if _overlaps(history, _training_ranges(manifest, "val")):
                    raise ValueError("上一模型曾训练过本轮验证时段，为避免验证泄漏")
                mean, scale = _scaling(saved, len(names))
                candidate = _network(torch, len(names), context_count=context_count).to(target_device)
                state = dict(saved["state_dict"])
                if old_context is None and context_count:
                    # Preserve the old predictor exactly before learning how to
                    # use new context. Fresh conditional columns start at zero.
                    key = "gru.weight_ih_l0"
                    extended = torch.zeros((state[key].shape[0], len(names) + context_count))
                    extended[:, :len(names)] = state[key]
                    state[key] = extended
                candidate.load_state_dict(state)
                model = candidate
                warm_start = True
                warm_start_reason = "特征格式兼容，保留上一模型权重和训练集标准化参数继续学习"
            except (OSError, ValueError, RuntimeError, KeyError, TypeError, pickle.UnpicklingError) as exc:
                reason = str(exc).split("\n", 1)[0]
                warm_start_reason = f"{reason}，本批重新拟合时序模型"
        report(warm_start_reason)
    if warm_start:
        normalized = (features - mean) / scale
        if not np.isfinite(normalized).all():
            raise ValueError("新增电影特征超出上一模型标准化范围")
    else:
        history = []
        mean, scale, normalized = _standardize(features, selected)
    provenance = _merge_ranges(history + _training_ranges(manifest))
    values = torch.from_numpy(np.concatenate((normalized, contexts), axis=1))
    sample_weights = _balanced_weights(manifest, windows["train"])
    train_loader = _loader(torch, values, windows["train"], shuffle=True, target_count=len(names), sample_weights=sample_weights)
    train_check = _loader(torch, values, windows["train"], shuffle=False, target_count=len(names))
    val_check = _loader(torch, values, windows["val"], shuffle=False, target_count=len(names))
    validation_kind = _validation_kind(manifest, windows)
    labels = {"independent-films": "独立电影验证", "within-film": "片内时段验证，尚不能证明跨电影泛化",
              "none": "无验证片段，仅显示训练集内部检查"}
    report(labels[validation_kind])
    if sample_weights is not None:
        report("多部训练片按影片均衡抽样，避免长片压过短片；验证仍使用全部留出窗口")
    train_baseline, train_baseline_mse = _evaluate(torch, model, train_check, target_device, persistence=True)
    baseline, baseline_mse = _evaluate(torch, model, val_check, target_device, persistence=True)
    counts = {"train_windows": len(windows["train"]), "val_windows": len(windows["val"]),
              "movie_count": len({s["film_id"] for s in manifest["sequences"]}), "sample_count": len(features),
              "feature_count": len(names), "validation_kind": validation_kind,
              "profile_context_count": context_count, "profile_count": manifest.get("profile_count", 0),
              "profile_conditioned": bool(context_count),
              "train_samples": sum(end - start for start, end in selected["train"]),
              "val_samples": sum(end - start for start, end in selected["val"])}
    metrics = {"kind": "movie-temporal", **counts, "total_epochs": epochs, "epochs": [],
               "baseline_loss": baseline, "baseline_mse": baseline_mse,
               "train_baseline_loss": train_baseline, "train_baseline_mse": train_baseline_mse,
               "warm_start": warm_start, "warm_start_reason": warm_start_reason, "patience": patience,
               "initial_learning_rate": initial_learning_rate, "restart": bool(restart),
               "sampling_strategy": "film-balanced" if sample_weights is not None else "uniform-windows",
               "profile_context": context_spec,
               "loss_definition": "平均标准化特征 Smooth L1，数值越低越好；不代表动画质量"}
    optimizer = torch.optim.AdamW(model.parameters(), lr=initial_learning_rate, weight_decay=.0001)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, mode="min", factor=.5, patience=max(1, patience//2), threshold=1e-6,
        threshold_mode="abs", min_lr=min(1e-5, initial_learning_rate / 100))

    def save_candidate(epoch):
        checkpoint = {"format": FORMAT, "version": VERSION, "feature_names": names,
                      "mean": mean.tolist(), "scale": scale.tolist(), "hidden_size": HIDDEN,
                      "context_steps": CONTEXT, "sample_fps": float(sample_fps), "extraction": extraction,
                      "training_ranges": provenance,
                      "profile_context": context_spec,
                      "state_dict": {key: value.detach().cpu() for key, value in model.state_dict().items()},
                      "epoch": epoch}
        temporary = run / "movie_temporal.pt.tmp"
        torch.save(checkpoint, temporary)
        temporary.replace(run / "movie_temporal.pt")

    initial_train, initial_train_mse = _evaluate(torch, model, train_check, target_device)
    initial_val, initial_val_mse = _evaluate(torch, model, val_check, target_device)
    best = {"epoch": 0, "train_loss": initial_train, "val_loss": initial_val,
            "train_mse": initial_train_mse, "val_mse": initial_val_mse}
    best_loss, best_epoch = initial_val if initial_val is not None else initial_train, 0
    best_origin = "previous-model" if warm_start else "persistence-baseline"
    metrics["initial"] = best.copy()
    metrics["best_epoch"], metrics["best_loss"] = best_epoch, best_loss
    save_candidate(0)
    _write_json(run / "movie_metrics.json", metrics)
    stalled, early_stopped = 0, False
    for epoch in range(1, epochs + 1):
        learning_rate = optimizer.param_groups[0]["lr"]
        model.train()
        for batch, lengths, target in train_loader:
            batch, target = batch.to(target_device), target.to(target_device)
            optimizer.zero_grad(set_to_none=True)
            prediction = model(batch, lengths)
            loss = torch.nn.functional.smooth_l1_loss(prediction, target)
            if not torch.isfinite(loss):
                raise ValueError("时序训练出现非有限损失，已停止本轮")
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.)
            optimizer.step()
        train_loss, train_mse = _evaluate(torch, model, train_check, target_device)
        val_loss, val_mse = _evaluate(torch, model, val_check, target_device)
        row = {"epoch": epoch, "train_loss": train_loss, "val_loss": val_loss,
               "train_mse": train_mse, "val_mse": val_mse, "learning_rate": learning_rate}
        score = val_loss if val_loss is not None else train_loss
        if not math.isfinite(score):
            raise ValueError("模型评估出现非有限损失，已停止本轮")
        if score < best_loss - 1e-6:
            best_loss, best_epoch = score, epoch
            best, best_origin, stalled = row.copy(), "trained", 0
            save_candidate(epoch)
        else:
            stalled += 1
        scheduler.step(score)
        metrics["epochs"].append(row)
        metrics["latest"] = row
        metrics["best_epoch"] = best_epoch
        metrics["best_loss"] = best_loss
        metrics["best_origin"] = best_origin
        metrics["improvement_percent"] = _improvement(best["val_loss"], baseline)
        metrics["actual_epochs"] = epoch
        _write_json(run / "movie_metrics.json", metrics)
        checked = f"验证损失 {val_loss:.5f}" if val_loss is not None else "无独立验证指标"
        report(f"电影时序训练 · 第 {epoch}/{epochs} 轮 · 训练损失 {train_loss:.5f} · {checked} · 学习率 {learning_rate:g}")
        if stalled >= patience:
            early_stopped = epoch < epochs
            if early_stopped:
                report(f"连续 {patience} 轮未改善，提前停止并保留最佳模型")
            break
    actual_epochs = len(metrics["epochs"])
    details = {"warm_start": warm_start, "warm_start_reason": warm_start_reason,
               "sampling_strategy": metrics["sampling_strategy"],
               "requested_epochs": epochs, "actual_epochs": actual_epochs, "early_stopped": early_stopped,
               "best_origin": best_origin, "improvement_percent": _improvement(best["val_loss"], baseline),
               "train_improvement_percent": _improvement(best["train_loss"], train_baseline)}
    metrics.update(details)
    _write_json(run / "movie_metrics.json", metrics)
    description = {"format": FORMAT, "version": VERSION, "kind": "movie-temporal", "architecture": "GRU-residual",
                   "feature_names": names, "context_steps": CONTEXT, "sample_fps": float(sample_fps),
                   "extraction": extraction,
                   "profile_context": context_spec, "profile_context_note": manifest.get("profile_context", {}).get("note", "") if context_spec else "",
                   "profile_input": {"api": "predict_next(model_path, history, steps=1, profile=profile, history_times_ms=times)",
                                     "fields": ["status=confirmed", "cast[{role,actor,aliases}]", "synopsis", "events[{start_ms,end_ms,text,roles}]"],
                                     "timing": "有时间段事件时必须提供与历史观测逐行对应的毫秒时间轴；递推不注入未来事件",
                                     "missing": "未提供资料时按缺失上下文预测，效果可能与训练时不同"} if context_spec else None,
                   "initial_learning_rate": initial_learning_rate, "patience": patience, "restart": bool(restart),
                   "weights": "movie_temporal.pt", "metrics": "movie_metrics.json", "best_epoch": best_epoch,
                   "objective": "依据连续电影观测预测下一个采样时刻的特征", "limitations": LIMITATIONS,
                   "aecpp_direct_import": False, **details, **counts}
    _write_json(run / "movie_model.json", description)
    completed = {"kind": "movie-temporal", "snapshot": fingerprint, "model": "movie_temporal.pt",
                 "manifest": "movie_model.json", "metrics": "movie_metrics.json", "epochs": actual_epochs,
                 "best_epoch": best_epoch, "train_loss": best["train_loss"], "val_loss": best["val_loss"],
                 "baseline_loss": baseline, "sample_fps": float(sample_fps), **details, **counts}
    completed.update(initial_learning_rate=initial_learning_rate, patience=patience, restart=bool(restart))
    evaluate_run(run, report)
    _write_json(run / "completed.json", completed)
    report("训练指标仅衡量观测特征预测，不代表剧情理解或动画制作质量")
    report(f"训练完成 · 电影时序模型 · {run}")
    return run / "completed.json"


FEATURE_LABELS = {
    "luma": "画面亮度", "contrast": "画面对比度", "saturation": "色彩饱和度",
    "red": "红色分量", "green": "绿色分量", "blue": "蓝色分量",
    "quadrant_luma_0": "左上亮度", "quadrant_luma_1": "右上亮度",
    "quadrant_luma_2": "左下亮度", "quadrant_luma_3": "右下亮度",
    "motion": "画面变化强度", "cut_score": "候选切点分数", "motion_x": "水平光流",
    "motion_y": "垂直光流", "audio_rms": "声音平均能量", "audio_peak": "声音峰值",
    "audio_available": "声音输入可用", "subtitle_active": "字幕出现状态",
    "subtitle_chars": "字幕字符数", "subtitle_available": "字幕输入可用",
    "subtitle_density": "同时显示的字幕数量", "confirmed_role_count": "已确认角色数量",
    "annotation_available": "人工标注可用", "people_count": "检出人物数量",
    "people_area": "人物画面占比", "people_x": "人物水平位置", "people_y": "人物垂直位置",
    "people_available": "人物检测可用", "script_role_count": "剧本建议角色数量",
    "script_available": "剧本匹配可用", "edge_density": "画面边缘密度",
    "frame_interval": "采样时间间隔",
}
GROUP_LABELS = {"visual": "画面", "motion": "运动与候选切点", "audio": "声音",
                "subtitles": "字幕", "people": "人物与角色", "availability": "输入可用性",
                "other": "其他观测"}
FEATURE_AVAILABILITY = {"audio_rms": "audio_available", "audio_peak": "audio_available",
                        "people_count": "people_available", "people_area": "people_available",
                        "people_x": "people_available", "people_y": "people_available",
                        "confirmed_role_count": "annotation_available", "script_role_count": "script_available",
                        "subtitle_active": "subtitle_available", "subtitle_chars": "subtitle_available",
                        "subtitle_density": "subtitle_available"}


def _feature_group(name):
    if name.endswith("available"):
        return "availability"
    if name in {"motion", "cut_score", "motion_x", "motion_y"}:
        return "motion"
    if name.startswith("audio_"):
        return "audio"
    if name.startswith("subtitle_"):
        return "subtitles"
    if name.startswith("people_") or name in {"confirmed_role_count", "script_role_count"}:
        return "people"
    if name in {"luma", "contrast", "saturation", "red", "green", "blue", "edge_density"} or name.startswith("quadrant_"):
        return "visual"
    return "other"


def evaluate_run(run, report=lambda _: None):
    """Evaluate the saved candidate without changing its weights or training data."""
    import torch

    run = Path(run)
    torch.set_num_threads(min(4, torch.get_num_threads()))
    manifest, features, _, windows = _corpus(run)
    names = manifest["feature_names"]
    context_spec, contexts = _contexts(run, manifest, len(features))
    saved = torch.load(run / "movie_temporal.pt", map_location="cpu", weights_only=True)
    if (saved.get("format") != FORMAT or saved.get("version") != VERSION
            or saved.get("feature_names") != names or saved.get("context_steps") != CONTEXT
            or saved.get("profile_context") != context_spec):
        raise ValueError("模型与这一批电影特征不兼容，无法评估")
    if "training_ranges" in saved and _overlaps(_merge_ranges(saved["training_ranges"]), _training_ranges(manifest, "val")):
        raise ValueError("模型历史训练包含当前验证时段，不能生成有效验证报告")
    mean, scale = _scaling(saved, len(names))
    normalized = (features - mean) / scale
    if not np.isfinite(normalized).all():
        raise ValueError("评估输入超出有效数值范围")
    model = _network(torch, len(names), saved["hidden_size"], contexts.shape[1])
    model.load_state_dict(saved["state_dict"])
    model.eval()
    split = "val" if windows["val"] else "train"
    loader = _loader(torch, torch.from_numpy(np.concatenate((normalized, contexts), axis=1)), windows[split],
                     shuffle=False, target_count=len(names))
    model_sum = np.zeros(len(names), dtype=np.float64)
    baseline_sum = np.zeros(len(names), dtype=np.float64)
    observed_model = np.zeros(len(names), dtype=np.float64)
    observed_baseline = np.zeros(len(names), dtype=np.float64)
    observed_count = np.zeros(len(names), dtype=np.int64)
    count = 0
    report("正在评估已保存模型的分项预测误差…")
    with torch.inference_mode():
        for batch, lengths, target in loader:
            prediction = model(batch, lengths)
            if not torch.isfinite(prediction).all():
                raise ValueError("模型预测存在非有限数值，无法评估")
            previous = batch[torch.arange(len(batch)), lengths - 1, :len(names)]
            losses = torch.nn.functional.smooth_l1_loss(prediction, target, reduction="none").numpy()
            baselines = torch.nn.functional.smooth_l1_loss(previous, target, reduction="none").numpy()
            model_sum += losses.sum(0)
            baseline_sum += baselines.sum(0)
            raw = target.numpy() * scale + mean
            for index, name in enumerate(names):
                mask_name = FEATURE_AVAILABILITY.get(name)
                available = raw[:, names.index(mask_name)] > .5 if mask_name in names else np.ones(len(batch), dtype=bool)
                observed_model[index] += losses[available, index].sum()
                observed_baseline[index] += baselines[available, index].sum()
                observed_count[index] += int(available.sum())
            count += len(batch)
    model_losses, baseline_losses = model_sum / count, baseline_sum / count
    details = []
    for index, name in enumerate(names):
        observed = int(observed_count[index])
        model_loss = float(observed_model[index] / observed) if observed else None
        base_loss = float(observed_baseline[index] / observed) if observed else None
        details.append({"name": name, "label": FEATURE_LABELS.get(name, name), "group": _feature_group(name),
                        "model_loss": model_loss, "baseline_loss": base_loss,
                        "improvement_percent": _improvement(model_loss, base_loss), "sample_count": observed,
                        "total_samples": count, "availability_feature": FEATURE_AVAILABILITY.get(name),
                        "all_samples_model_loss": float(model_losses[index]),
                        "all_samples_baseline_loss": float(baseline_losses[index])})
    groups = []
    for group, label in GROUP_LABELS.items():
        rows = [row for row in details if row["group"] == group]
        if rows:
            observed = sum(row["sample_count"] for row in rows)
            model_loss = sum((row["model_loss"] or 0) * row["sample_count"] for row in rows) / observed if observed else None
            baseline_loss = sum((row["baseline_loss"] or 0) * row["sample_count"] for row in rows) / observed if observed else None
            groups.append({"name": group, "label": label, "feature_count": len(rows), "model_loss": model_loss,
                           "baseline_loss": baseline_loss, "improvement_percent": _improvement(model_loss, baseline_loss),
                           "sample_count": observed, "total_samples": count * len(rows)})
    validation_kind = _validation_kind(manifest, windows)
    warnings = ["指标只衡量观测特征预测误差，不代表剧情、动作或动画创作能力。"]
    if context_spec:
        from movie_context import NOTE
        warnings.append(NOTE)
    if validation_kind == "within-film":
        warnings.append("验证使用同一影片的留出时段，不能证明跨电影泛化。")
    elif validation_kind == "none":
        warnings.append("没有验证片段，本报告使用训练数据，仅供内部检查。")
    if split == "val":
        warnings.append("此验证集参与最佳轮次选择，最终效果仍需独立测试素材评估。")
    warnings.append("输入可用性和固定采样间隔单独列出，恒定值的零基线不计算改善百分比。")
    warnings.append("总损失沿用训练的全部特征口径；分项和分类只统计对应输入可用的时刻，无有效样本显示为空。")
    model_loss, baseline_loss = float(model_losses.mean()), float(baseline_losses.mean())
    evaluation = {"version": 1, "kind": "movie-temporal-evaluation", "model_epoch": saved.get("epoch"),
                  "validation_kind": validation_kind, "split": split, "sample_count": count,
                  "profile_conditioned": bool(context_spec), "profile_context_count": contexts.shape[1],
                  "model_loss": model_loss, "baseline_loss": baseline_loss,
                  "improvement_percent": _improvement(model_loss, baseline_loss),
                  "finite_predictions": True, "features": details, "groups": groups, "warnings": warnings}
    _write_json(run / "movie_evaluation.json", evaluation)
    improvement = evaluation["improvement_percent"]
    result = f" · 相比基线改善 {improvement:.2f}%" if improvement is not None else " · 基线为零，不计算改善比例"
    report(f"模型分项评估完成 · {count} 个窗口{result}")
    return evaluation


def predict_next(model_path, history, steps=1, *, profile=None, history_times_ms=None):
    """Return future numeric feature rows in checkpoint feature_names order.

    History must be sampled at the checkpoint's sample_fps and use its exact
    feature order. Multi-step output feeds predictions back into the model.
    """
    import torch

    if type(steps) is not int or not 1 <= steps <= 120:
        raise ValueError("预测步数须在 1–120 之间")
    path = Path(model_path)
    saved = torch.load(path / "movie_temporal.pt" if path.is_dir() else path,
                       map_location="cpu", weights_only=True)
    if saved.get("format") != FORMAT or saved.get("version") != VERSION:
        raise ValueError("不是受支持的电影时序模型")
    names = saved["feature_names"]
    values = np.asarray(history, dtype=np.float32)
    if values.ndim != 2 or values.shape[1] != len(names) or not len(values) or not np.isfinite(values).all():
        raise ValueError("预测需要按模型特征顺序提供至少一个有效观测时刻")
    mean, scale = _scaling(saved, len(names))
    context = saved["context_steps"]
    if type(context) is not int or not 1 <= context <= 1024:
        raise ValueError("电影模型上下文长度无效")
    context_spec = _context_spec(saved)
    context_count = len(context_spec["names"]) if context_spec else 0
    if context_count:
        from movie_context import encode_profile
        if history_times_ms is None:
            if profile and profile.get("events"):
                raise ValueError("使用时间段剧情资料预测时，须提供 history_times_ms 对应观测时间")
            history_times_ms = np.arange(len(values)) * 1000 / saved["sample_fps"]
        times = np.asarray(history_times_ms)
        if times.shape != (len(values),) or not np.isfinite(times).all() or (np.diff(times) <= 0).any():
            raise ValueError("资料条件时间轴须与历史观测逐行对应且递增")
        conditions = encode_profile(profile, times)[-context:]
    else:
        conditions = np.zeros((min(context, len(values)), 0), dtype=np.float32)
    model = _network(torch, len(names), saved["hidden_size"], context_count)
    model.load_state_dict(saved["state_dict"])
    model.eval()
    normalized = (values[-context:] - mean) / scale
    if not np.isfinite(normalized).all():
        raise ValueError("输入观测超出模型的有效数值范围")
    output = []
    with torch.inference_mode():
        for _ in range(steps):
            batch = torch.from_numpy(np.concatenate((normalized, conditions), axis=1)[None])
            result = model(batch, torch.tensor([len(normalized)]))[0].numpy()
            future = result * scale + mean
            if not np.isfinite(future).all():
                raise ValueError("模型预测超出有效数值范围")
            output.append(future.tolist())
            normalized = np.concatenate((normalized, result[None]), axis=0)[-context:]
            if context_count:
                # Do not reveal future event annotations during recursive rollout.
                next_condition = conditions[-1].copy()
                next_condition[64:96] = 0
                next_condition[-1] = 0
                conditions = np.concatenate((conditions, next_condition[None]), axis=0)[-context:]
            else:
                conditions = np.zeros((len(normalized), 0), dtype=np.float32)
    return {"feature_names": names, "sample_fps": saved["sample_fps"], "values": output,
            "kind": "numeric-observation-forecast", "profile_conditioned": bool(context_count)}


def export_bundle(run, target):
    """Export model, portable metadata and metrics, excluding source movies."""
    run, target = Path(run), Path(target)
    completed = json.loads((run / "completed.json").read_text(encoding="utf-8"))
    if completed.get("kind") != "movie-temporal":
        raise ValueError("请选择已完成的电影时序训练批次")
    files = ["movie_temporal.pt", "movie_model.json", "movie_metrics.json", "completed.json"]
    if any(not (run / name).is_file() for name in files):
        raise ValueError("电影时序模型产物不完整")
    if (run / "movie_evaluation.json").is_file():
        files.append("movie_evaluation.json")
    protected = files + ["movie_corpus.json", "movie_corpus.npz", "movie_sources.json"]
    if target.resolve() in {(run / name).resolve() for name in protected}:
        raise ValueError("导出文件不能覆盖训练产物")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    profiles = []
    source_path = run / "movie_sources.json"
    if source_path.is_file():
        sources = json.loads(source_path.read_text(encoding="utf-8"))
        for movie in sources.get("movies", []):
            if not isinstance(movie, dict):
                continue
            profile = movie.get("profile")
            if not isinstance(profile, dict) or profile.get("status") != "confirmed":
                continue
            # Explicit allowlist excludes source paths, local IDs, research
            # diagnostics, screenshots, dialogue snapshots, and cache metadata.
            portable = {key: profile[key] for key in ("version", "title", "year", "cast", "synopsis", "events", "sources", "allow_missing")
                        if key in profile}
            portable["status"] = "confirmed"
            profiles.append({"film_id": movie.get("digest"), "title": profile.get("title") or movie.get("name"),
                             "profile": portable})
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in files:
                archive.write(run / name, name)
            archive.writestr("movie_profiles.json", json.dumps({"version": 1, "profiles": profiles,
                              "input_api": "predict_next(model_path, history, steps=1, profile=profile, history_times_ms=times)",
                              "notes": "仅包含本次训练快照已确认的资料；有时间段事件时必须提供历史观测毫秒时间轴。"
                                       "资料是词面条件输入，未提供时按缺失处理；无原片、截图或本地文件路径。"},
                             ensure_ascii=False, indent=2))
            archive.writestr("bundle.json", json.dumps({"format": "yingge-movie-temporal-bundle", "version": VERSION,
                                                        "manifest": "movie_model.json", "aecpp_direct_import": False,
                                                        "profiles": "movie_profiles.json",
                                                        "limitations": LIMITATIONS}, ensure_ascii=False, indent=2))
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target
