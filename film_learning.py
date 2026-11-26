"""Train from a fixed snapshot of reviewed film annotations."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import uuid

from dataset import connect


def snapshot(root):
    with connect(root) as db:
        db.execute("BEGIN")
        names = [r[0] for r in db.execute("SELECT name FROM classes ORDER BY id")]
        rows = [dict(row) for row in db.execute("""SELECT f.id,f.path,f.split,f.boxes,f.voices,f.ms,
                     m.digest AS film_id,m.name AS film FROM frames f JOIN movies m ON m.id=f.movie
                     WHERE f.state='reviewed' AND f.deleted=0 ORDER BY f.id""")]
    return names,rows


def prepare_detection(root, run, names, rows, report):
    import yaml

    data = run/"dataset"
    has_validation = any(row["split"]=="val" for row in rows)
    for split in ("train","val") if has_validation else ("train",):
        for folder in ("images","labels"):
            (data/folder/split).mkdir(parents=True)
    for i,row in enumerate(rows,1):
        if i==1 or i%50==0:
            report(f"准备目标检测样本 · {i}/{len(rows)}")
        source = root/row["path"]
        name = str(row["id"])
        image = data/"images"/row["split"]/(name+source.suffix)
        try:
            os.link(source,image)
        except OSError:
            shutil.copyfile(source,image)
        labels = []
        for category,x,y,u,v,*_ in json.loads(row["boxes"]):
            labels.append(f"{category} {(x+u)/2:.8f} {(y+v)/2:.8f} {u-x:.8f} {v-y:.8f}")
        (data/"labels"/row["split"]/(name+".txt")).write_text("\n".join(labels),encoding="ascii")
    spec = {"path":str(data),"train":"images/train","val":"images/val" if has_validation else "images/train",
            "names":names}
    path = data/"data.yaml"
    path.write_text(yaml.safe_dump(spec,allow_unicode=True),encoding="utf-8")
    return path,has_validation


def train_detector(root, run, weights, names, rows, epochs, device, report):
    import torch
    from ultralytics import YOLO

    spec,has_validation = prepare_detection(root,run,names,rows,report)
    if not has_validation:
        report("本轮没有独立验证图片，检测训练将使用训练集检查运行状态")
    if not device:
        device = "0" if torch.cuda.is_available() else "cpu"
    if device=="cpu":
        torch.set_num_threads(min(4,torch.get_num_threads()))
    model = YOLO(weights)
    if model.task!="detect":
        raise ValueError("请选择 YOLO 目标检测权重")
    model.add_callback("on_train_epoch_end",lambda trainer: report(f"训练目标检测 · 第 {trainer.epoch+1}/{epochs} 轮"))
    model.train(data=str(spec),epochs=epochs,imgsz=320,batch=4,workers=0,device=device,
                project=str(run),name="fit",exist_ok=True,plots=False,amp=False,val=has_validation)
    if not (run/"fit/weights/best.pt").is_file():
        raise RuntimeError("检测训练未生成可用权重")
    return has_validation


def text_features(text):
    from scripts import normalized

    text = normalized(text)
    return Counter(text[i:i+size] for size in (1,2,3) for i in range(len(text)-size+1))


def weighted_vector(terms, idf):
    vector = {term:(1+math.log(count))*idf.get(term,1) for term,count in terms.items()}
    norm = math.sqrt(sum(value*value for value in vector.values())) or 1
    return {term:value/norm for term,value in vector.items()}


def train_context(root, run, rows, report):
    from PIL import Image
    from tracking import appearance

    entries,frequency = [],Counter()
    for i,row in enumerate(rows,1):
        if i==1 or i%50==0:
            report(f"学习人物和台词 · {i}/{len(rows)} 张训练截图")
        subjects = [("onscreen",b[1:5],b[5]) for b in json.loads(row["boxes"])
                    if len(b)>5 and (b[5]["role"] or b[5]["lines"])]
        subjects.extend((n["kind"],None,n) for n in json.loads(row["voices"]))
        if not subjects:
            continue
        with Image.open(root/row["path"]) as source:
            image = source.convert("RGB")
        scene = None
        for kind,coords,note in subjects:
            if coords:
                x,y,u,v = [value*size for value,size in zip(coords,image.size*2)]
                feature = appearance(image.crop((math.floor(x),math.floor(y),math.ceil(u),math.ceil(v)))).tolist()
            else:
                if scene is None:
                    scene = appearance(image).tolist()
                feature = scene
            terms = text_features(" ".join(note["lines"]))
            if terms:
                frequency.update(terms.keys())
            entries.append({"frame_id":row["id"],"film_id":row["film_id"],"time_ms":row["ms"],"kind":kind,
                            "role":note["role"],"lines":note["lines"],"visual":feature,"terms":terms})
    if entries:
        documents = sum(bool(entry["terms"]) for entry in entries)
        idf = {term:1+math.log((1+documents)/(1+count)) for term,count in frequency.items()}
        for entry in entries:
            entry["terms"] = weighted_vector(entry["terms"],idf)
        (run/"context.json").write_text(json.dumps({"version":1,"idf":idf,"entries":entries},ensure_ascii=False),encoding="utf-8")
    return len(entries)


def search_context(model, film_id, *, image=None, text="", kind="onscreen", limit=5):
    """Rank learned examples for a film, from a person crop or a full offscreen frame and/or text."""
    from tracking import appearance

    model = json.loads(Path(model).read_text(encoding="utf-8"))
    terms = weighted_vector(text_features(text),model["idf"])
    visual = appearance(image.convert("RGB")).tolist() if image is not None else []
    ranked = []
    for entry in model["entries"]:
        if entry["film_id"]!=film_id or entry["kind"]!=kind:
            continue
        scores = []
        if visual:
            scores.append(sum(a*b for a,b in zip(visual,entry["visual"])))
        if terms:
            scores.append(sum(value*entry["terms"].get(term,0) for term,value in terms.items()))
        if scores and (score := sum(scores)/len(scores))>0:
            ranked.append({**{k:v for k,v in entry.items() if k not in {"terms","visual"}},"score":score})
    return sorted(ranked,key=lambda r:r["score"],reverse=True)[:limit]


def new_run_path(root):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    return root/"runs"/(stamp+"-"+uuid.uuid4().hex[:8])


def train(root, weights, epochs, device, report, *, run=None, previous=None, reviewed=None):
    names,rows = snapshot(root) if reviewed is None else reviewed
    fingerprint = hashlib.sha256(json.dumps([names,rows],ensure_ascii=False,sort_keys=True).encode("utf-8")).hexdigest()
    if previous and previous.is_file():
        if json.loads(previous.read_text(encoding="utf-8")).get("snapshot")==fingerprint:
            report("上一轮已包含当前已确认标注，等待新的投喂")
            return
    training = [row for row in rows if row["split"]=="train"]
    if not training:
        raise ValueError("请先确认至少一张训练集截图，再开始训练")
    detects = any(json.loads(row["boxes"]) for row in training)
    run = run or new_run_path(root)
    run.mkdir(parents=True)
    report(f"训练快照 · {len(training)} 张训练图，{len(rows)-len(training)} 张验证图")
    context_count = train_context(root,run,training,report)
    if not detects and not context_count:
        raise ValueError("请先给训练集截图添加标注框或台词，并确认截图")
    holdout = train_detector(root,run,weights,names,rows,epochs,device,report) if detects else False
    metadata = {"snapshot":fingerprint,"train_images":len(training),"val_images":len(rows)-len(training),"context_entries":context_count,
                "detector":"fit/weights/best.pt" if detects else None,"detector_holdout":holdout,
                "context":"context.json" if context_count else None,"initial_weights":weights if detects else None}
    temp = run/"completed.tmp"
    temp.write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding="utf-8")
    temp.replace(run/"completed.json")
    outputs = "、".join(name for name,exists in (("目标检测",detects),("人物与台词检索",context_count)) if exists)
    report(f"训练完成 · {outputs} · {run}")
