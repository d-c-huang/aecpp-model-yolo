"""Use existing detection weights for optional draft boxes and clip tracking."""
import argparse
from pathlib import Path

from dataset import classes, frames, save_boxes


def report(message):
    print("AECPP " + str(message), flush=True)




def predict(root, weights, confidence):
    from PIL import Image
    from ultralytics import YOLO
    todo = [row for row in frames(root) if row["state"] == "new" and row["split"] == "train"]
    if not todo:
        raise ValueError("没有待预标注的训练图片。验证图片请独立人工标注。")
    model = YOLO(weights)
    names = classes(root)
    if model.task != "detect":
        raise ValueError("请选择目标检测模型权重")
    aliases = {"person":"人物"} if "person" not in names else {}
    mapping = {i:names.index(label) for i, raw in model.names.items()
               if (label := aliases.get(raw,raw)) in names}
    if not mapping:
        raise ValueError("权重的类别与工程类别没有交集，请选择本工程权重或添加对应类别。")
    report("匹配类别 · " + "、".join(names[i] for i in sorted(set(mapping.values()))))
    count = 0
    for i, row in enumerate(todo):
        with Image.open(root / row["path"]) as image:
            result = model.predict(image.convert("RGB"), conf=confidence, classes=list(mapping), verbose=False)[0]
        boxes = []
        for category, coords in zip(result.boxes.cls.tolist(), result.boxes.xyxyn.tolist()):
            x,y,u,v = [max(0., min(1., float(n))) for n in coords]
            if x < u and y < v:
                boxes.append([mapping[int(category)], x,y,u,v])
        count += save_boxes(root, row["id"], boxes, "predicted", only_new=True)
        report(f"预标注 {i+1}/{len(todo)} · 本图 {len(boxes)} 个框")
    report(f"已生成 {count} 张待复核草稿")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("task", choices=["predict","track"])
    p.add_argument("root", type=Path)
    p.add_argument("weights")
    p.add_argument("--confidence", type=float, default=.25)
    p.add_argument("--source",type=Path)
    p.add_argument("--start",type=float,default=0)
    p.add_argument("--length",type=float,default=12)
    args = p.parse_args()
    try:
        if args.task=="track":
            from tracking import analyse_clip
            report(analyse_clip(args.root.resolve(),args.source,args.start,args.length,args.weights,report))
        else:
            predict(args.root.resolve(), args.weights, args.confidence)
    except Exception as exc:
        report(str(exc))
        raise SystemExit(1)
