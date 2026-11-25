"""Sample people through a continuous clip. Roles are explicitly assigned per film."""
import io
import json
import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps
from scipy.optimize import linear_sum_assignment

from dataset import connect, register_movie
from media import open_movie


def appearance(crop):
    hsv = cv2.cvtColor(np.asarray(crop.resize((40,64))), cv2.COLOR_RGB2HSV)
    # Retain upper/lower costume colours and brightness for black/white clothing.
    parts = []
    for half in np.array_split(hsv,2):
        parts.append(cv2.calcHist([half],[0,1],None,[12,4],[0,180,0,256]).ravel())
        parts.append(cv2.calcHist([half],[2],None,[8],[0,256]).ravel())
    hist = np.sqrt(np.concatenate(parts))
    return hist / max(float(np.linalg.norm(hist)),1e-8)


def overlap(a,b):
    x,y = max(a[0],b[0]),max(a[1],b[1])
    u,v = min(a[2],b[2]),min(a[3],b[3])
    common = max(0,u-x)*max(0,v-y)
    return common/max(1e-8,(a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-common)


class Track:
    def __init__(self,shot):
        self.shot = shot
        self.points = []
        self.best_size = 0
        self.first = self.best = self.last = None

    def observe(self, ms, detection):
        box,feature,crop = detection
        self.feature = feature if not self.points else .75*self.feature + .25*feature
        self.points.append([ms,*box])
        self.last = ImageOps.pad(crop,(70,88),color="#263239")
        if self.first is None:
            self.first = self.last
        if crop.width*crop.height > self.best_size:
            self.best_size = crop.width*crop.height
            self.best = self.last

    def thumbnail(self):
        strip = Image.new("RGB",(210,88))
        for i,image in enumerate((self.first,self.best,self.last)):
            strip.paste(image,(70*i,0))
        buf = io.BytesIO()
        strip.save(buf,"JPEG",quality=85)
        return buf.getvalue()


def match(tracks,detections,ms,shot):
    live = [t for t in tracks if t.shot==shot and ms-t.points[-1][0]<=650]
    costs = np.full((len(live),len(detections)),3.)
    for i,t in enumerate(live):
        for j,(box,feature,_) in enumerate(detections):
            iou = overlap(t.points[-1][1:],box)
            sim = float(t.feature @ feature)
            if iou>=.15 and sim>=.55:
                costs[i,j] = 1 - (.65*iou+.35*sim)
    assigned = {}
    if costs.size:
        for i,j in zip(*linear_sum_assignment(costs)):
            if costs[i,j]<1:
                assigned[j] = live[i]
    for j,det in enumerate(detections):
        track = assigned.get(j)
        if track is None:
            track = Track(shot)
            tracks.append(track)
        track.observe(ms,det)


def analyse_clip(root,source,start,length,weights,progress):
    from ultralytics import YOLO
    if not math.isfinite(start) or start<0 or not 0<length<=120:
        raise ValueError("起点须为非负秒数；每段时长可设为 0–120 秒（大于零）。")
    source = Path(source)
    with open_movie(source,progress) as movie:
        if start >= movie.duration:
            raise ValueError(f"起点超出影片时长（{movie.duration:.2f} 秒）")
        film = register_movie(root,source,"unset",progress)
        end = min(start+length,movie.duration)
        key = (film["id"],round(start*1000),round(end*1000))
        with connect(root) as db:
            if db.execute("SELECT 1 FROM clips WHERE movie=? AND start_ms=? AND end_ms=?",key).fetchone():
                return "该片段已分析，可在「角色确认」中继续"
        progress("正在加载人物检测权重…")
        model = YOLO(weights)
        ids = [k for k,v in model.names.items() if v in ("人物","人","person","行人")]
        if model.task!="detect" or not ids:
            raise ValueError("权重需含人物检测类别。可选择客户端的 yolo11n.pt。")
        tracks,shot,last_view,last_ms = [],1,None,-1
        times = (start+i/4 for i in range(math.ceil((end-start)*4)))
        for seconds,image in movie.samples(times):
            ms = round(seconds*1000)
            if ms==last_ms or seconds>=end:
                continue
            last_ms = ms
            progress(f"跟踪人物 · {seconds:.2f} / {end:.2f} 秒")
            view = np.asarray(image.resize((48,32)),dtype=np.float32)
            if last_view is not None and np.abs(view-last_view).mean()>35:
                shot += 1
            last_view = view
            result = model.predict(image, classes=ids, conf=.3, verbose=False)[0]
            detections = []
            for raw in result.boxes.xyxyn.tolist():
                box = [min(1.,max(0.,x)) for x in raw]
                x,y,u,v = [round(value*dim) for value,dim in zip(box,image.size*2)]
                if x>=u or y>=v:
                    continue
                crop = image.crop((x,y,u,v))
                detections.append((box,appearance(crop),crop))
            match(tracks,detections,ms,shot)
    if not tracks:
        return "该时段未检出人物。请选择有人出现的时段或其他检测权重。"
    progress("正在保存轨迹及代表图…")
    with connect(root) as db:
        cur = db.execute("INSERT OR IGNORE INTO clips(movie,start_ms,end_ms) VALUES(?,?,?)",key)
        if cur.rowcount:
            db.executemany("INSERT INTO tracks(clip,shot,observations,preview,appearance) VALUES(?,?,?,?,?)",
                           [(cur.lastrowid,t.shot,json.dumps(t.points),t.thumbnail(),json.dumps(t.feature.tolist()))
                            for t in tracks])
    return f"片段分析完成 · 发现 {len(tracks)} 条人物轨迹，可在「角色确认」中分组"


def films_with_tracks(root):
    with connect(root) as db:
        return list(db.execute("SELECT DISTINCT m.id,m.name FROM movies m JOIN clips c ON c.movie=m.id ORDER BY m.id"))


def film_tracks(root,movie):
    with connect(root) as db:
        rows = db.execute("SELECT t.* FROM tracks t JOIN clips c ON c.id=t.clip WHERE c.movie=? ORDER BY c.start_ms,t.id",(movie,))
        return [{**dict(r),"points":json.loads(r["observations"]),"feature":json.loads(r["appearance"])} for r in rows]


def assign_role(root,movie,ids,role):
    role = role.strip()
    if len(role)>64 or any(ord(c)<32 for c in role):
        raise ValueError("角色名最多 64 个可显示字符")
    with connect(root) as db:
        for track in ids:
            cur = db.execute("UPDATE tracks SET role=? WHERE id=? AND clip IN (SELECT id FROM clips WHERE movie=?)",
                             (role,track,movie))
            if not cur.rowcount:
                raise ValueError("轨迹不属于当前电影，请重新打开角色确认")
