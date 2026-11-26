"""Run prediction, clip tracking and training in a worker process."""
import argparse
import json
from pathlib import Path

from dataset import PERSON, classes, prediction_frames, save_boxes

BASE_WEIGHTS = Path(__file__).resolve().parent/"weights"/"yolo11n.pt"


def report(message):
    print("AECPP " + str(message), flush=True)


def detect_boxes(model, image, confidence, mapping):
    result = model.predict(image,conf=confidence,classes=list(mapping),verbose=False)[0]
    boxes = []
    for category,coords in zip(result.boxes.cls.tolist(),result.boxes.xyxyn.tolist()):
        x,y,u,v = [max(0.,min(1.,float(n))) for n in coords]
        if x<u and y<v:
            boxes.append([mapping[int(category)],x,y,u,v])
    return boxes


def predict(root, weights, confidence, *, movie, batch_id, frame_id=None, allow_validation=False):
    todo = prediction_frames(root,movie,batch_id,frame_id=frame_id,allow_validation=allow_validation)
    if not todo:
        raise ValueError("此截图已不在待识别列表中。" if frame_id is not None else "当前批次没有待辅助识别的训练图片。")
    from PIL import Image
    from ultralytics import YOLO
    model = YOLO(weights)
    names = classes(root)
    if model.task != "detect":
        raise ValueError("请选择目标检测模型权重")
    person = next((i for i,name in enumerate(names) if name in PERSON),None)
    aliases = {raw:names[person] for raw in PERSON if raw not in names} if person is not None else {}
    mapping = {i:names.index(label) for i, raw in model.names.items()
               if (label := aliases.get(raw,raw)) in names}
    if not mapping:
        raise ValueError("权重的类别与工程类别没有交集，请选择本工程权重或添加对应类别。")
    report("匹配类别 · " + "、".join(names[i] for i in sorted(set(mapping.values()))))
    can_fallback = person is not None and BASE_WEIGHTS.is_file() and Path(weights).resolve()!=BASE_WEIGHTS
    general = None
    count = 0
    empty = 0
    for i, row in enumerate(todo):
        used_general = False
        with Image.open(root / row["path"]) as image:
            rgb = image.convert("RGB")
            boxes = detect_boxes(model,rgb,confidence,mapping)
            if can_fallback and not any(names[box[0]] in PERSON for box in boxes):
                if general is None:
                    general = YOLO(str(BASE_WEIGHTS))
                people = detect_boxes(general,rgb,confidence,{idx:person for idx,label in general.names.items() if label=="person"})
                boxes.extend(people)
                used_general = bool(people)
        if boxes:
            count += save_boxes(root,row["id"],boxes,"predicted",only_new=True)
        else:
            empty += 1
        source = " · 通用权重补检人物" if used_general else ""
        report(f"预标注 {i+1}/{len(todo)} · 本图 {len(boxes)} 个框{source}")
    missing = f" · {empty} 张未检出目标，仍可再次辅助识别" if empty else ""
    report(f"已生成 {count} 张待复核草稿{missing}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("task", choices=["predict","track","train","train-movie"])
    p.add_argument("root", type=Path)
    p.add_argument("weights")
    p.add_argument("--confidence", type=float, default=.25)
    p.add_argument("--movie-id",type=int)
    p.add_argument("--batch-id",default=None)
    p.add_argument("--frame-id",type=int)
    p.add_argument("--allow-validation",action="store_true")
    p.add_argument("--source",type=Path)
    p.add_argument("--start",type=float,default=0)
    p.add_argument("--length",type=float,default=12)
    p.add_argument("--epochs",type=int)
    p.add_argument("--device",default="")
    p.add_argument("--run",type=Path)
    p.add_argument("--previous",type=Path)
    p.add_argument("--snapshot",type=Path)
    p.add_argument("--sample-fps",type=float,default=1.)
    p.add_argument("--patience",type=int,default=5)
    p.add_argument("--learning-rate",type=float,default=.001)
    p.add_argument("--restart",action="store_true")
    args = p.parse_args()
    if args.epochs is None:
        args.epochs = 30 if args.task == "train-movie" else 5
    try:
        if args.task=="track":
            from tracking import analyse_clip
            report(analyse_clip(args.root.resolve(),args.source,args.start,args.length,args.weights,report))
        elif args.task=="train-movie":
            from film_learning import new_run_path
            from movie_sequence_data import snapshot_sources
            from movie_sequence_model import train_movie
            sources = json.loads(args.snapshot.read_text(encoding="utf-8")) if args.snapshot else snapshot_sources(
                args.root,[args.movie_id] if args.movie_id is not None else None)
            train_movie(args.root.resolve(),sources,args.epochs,args.device,report,
                        run=args.run or new_run_path(args.root),sample_fps=args.sample_fps,
                        weights=args.weights or None,previous=args.previous,patience=args.patience,
                        learning_rate=args.learning_rate,restart=args.restart)
        elif args.task=="train":
            from film_learning import train
            if not 1<=args.epochs<=500:
                raise ValueError("训练轮数须在 1–500 之间")
            reviewed = json.loads(args.snapshot.read_text(encoding="utf-8")) if args.snapshot else None
            train(args.root.resolve(),args.weights,args.epochs,args.device,report,
                  run=args.run,previous=args.previous,reviewed=reviewed)
        else:
            if args.movie_id is None or args.batch_id is None:
                raise ValueError("辅助识别需要指定电影和截图批次。")
            predict(args.root.resolve(),args.weights,args.confidence,movie=args.movie_id,batch_id=args.batch_id,
                    frame_id=args.frame_id,allow_validation=args.allow_validation)
    except Exception as exc:
        report(str(exc))
        raise SystemExit(1)
