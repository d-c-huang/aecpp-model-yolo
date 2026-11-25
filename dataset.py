"""Movie sampling, frame annotations and portable film-learning datasets."""
import hashlib
import io
import json
import math
from pathlib import Path
import random
import sqlite3
import uuid
import zipfile
from contextlib import contextmanager

from media import open_movie

LAYERS = {"全片粗采 · 5 秒":5000,"标准 · 1 秒":1000,"局部精采 · 0.25 秒":250}
PERSON = {"人物","人","行人","person"}


@contextmanager
def connect(root):
    db = sqlite3.connect(Path(root) / "annotations.sqlite", timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "frames").mkdir(exist_ok=True)
    with connect(root) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS classes(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
        CREATE TABLE IF NOT EXISTS movies(id INTEGER PRIMARY KEY, digest TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL, mode TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS frames(id INTEGER PRIMARY KEY, movie INTEGER NOT NULL REFERENCES movies(id),
            slot INTEGER NOT NULL, ms INTEGER NOT NULL, path TEXT UNIQUE NOT NULL, split TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'new', boxes TEXT NOT NULL DEFAULT '[]', UNIQUE(movie,slot));
        CREATE TABLE IF NOT EXISTS clips(id INTEGER PRIMARY KEY, movie INTEGER NOT NULL REFERENCES movies(id),
            start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, UNIQUE(movie,start_ms,end_ms));
        CREATE TABLE IF NOT EXISTS tracks(id INTEGER PRIMARY KEY, clip INTEGER NOT NULL REFERENCES clips(id),
            shot INTEGER NOT NULL, observations TEXT NOT NULL, preview BLOB NOT NULL,
            appearance TEXT NOT NULL, role TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS subtitle_tracks(id INTEGER PRIMARY KEY, movie INTEGER NOT NULL REFERENCES movies(id),
            source TEXT NOT NULL,label TEXT NOT NULL,fingerprint TEXT NOT NULL,UNIQUE(movie,source));
        CREATE TABLE IF NOT EXISTS subtitle_cues(track INTEGER NOT NULL REFERENCES subtitle_tracks(id),
            start_ms INTEGER NOT NULL,end_ms INTEGER NOT NULL,text TEXT NOT NULL,
            PRIMARY KEY(track,start_ms,end_ms,text));
        """)
        db.execute("INSERT OR IGNORE INTO classes VALUES(0,'人物')")
        columns = {row["name"] for row in db.execute("PRAGMA table_info(frames)")}
        if "content_hash" not in columns:
            db.execute("ALTER TABLE frames ADD COLUMN content_hash TEXT")
        if "layer_ms" not in columns:
            db.execute("ALTER TABLE frames ADD COLUMN layer_ms INTEGER NOT NULL DEFAULT 1000")
        if "voices" not in columns:
            db.execute("ALTER TABLE frames ADD COLUMN voices TEXT NOT NULL DEFAULT '[]'")
        db.execute("CREATE INDEX IF NOT EXISTS frame_picture ON frames(movie,content_hash)")
        db.execute("CREATE INDEX IF NOT EXISTS frame_time ON frames(movie,ms)")


def classes(root):
    with connect(root) as db:
        return [row[0] for row in db.execute("SELECT name FROM classes ORDER BY id")]


def add_class(root, name):
    name = name.strip()
    if not name or len(name) > 64 or any(ord(c) < 32 for c in name):
        raise ValueError("类别名称需为 1–64 个可显示字符")
    with connect(root) as db:
        db.execute("INSERT OR IGNORE INTO classes(name) VALUES(?)", (name,))


def frames(root):
    with connect(root) as db:
        return [dict(row) for row in db.execute(
            "SELECT f.*, m.name AS film FROM frames f JOIN movies m ON f.movie=m.id ORDER BY f.id")]


def save_boxes(root, frame_id, boxes, state, only_new=False, *, voices=None):
    names = classes(root)
    if state not in ("draft", "reviewed", "predicted"):
        raise ValueError("无效的审核状态")
    for box in boxes:
        category, x1, y1, x2, y2 = box[:5]
        if not (isinstance(category, int) and 0 <= category < len(names)
                and 0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
            raise ValueError("标注框或类别无效")
        if len(box)>5:
            annotation = box[5]
            if (len(box)!=6 or not isinstance(annotation,dict) or
                    not isinstance(annotation.get("role"),str) or
                    not isinstance(annotation.get("lines"),list) or
                    not all(isinstance(t,str) and t.strip() for t in annotation["lines"])):
                raise ValueError("角色或台词标注无效")
            if annotation["lines"] and not annotation["role"].strip():
                raise ValueError("有台词的人物需要填写本片角色名")
            if names[category] not in PERSON:
                raise ValueError("请将角色和台词绑定到人物框")
    with connect(root) as db:
        values = [json.dumps(boxes),state]
        fields = "boxes=?,state=?"
        if voices is not None:
            for voice in voices:
                if (voice.get("kind") not in {"offscreen","narration","unknown"} or
                        not isinstance(voice.get("role"),str) or not isinstance(voice.get("lines"),list) or
                        not voice["lines"] or not all(isinstance(s,str) and s.strip() for s in voice["lines"])):
                    raise ValueError("每条画外发言需选择来源并填写台词")
            fields += ",voices=?"
            values.append(json.dumps(voices,ensure_ascii=False))
        query = f"UPDATE frames SET {fields} WHERE id=?"
        if only_new:
            query += " AND state='new'"
        return db.execute(query, (*values,frame_id)).rowcount


def register_movie(root, source, mode, progress):
    # Keep the old content identity, including after moving a file or upgrading a project.
    h = hashlib.sha256()
    size = source.stat().st_size
    last_percent = -1
    with source.open("rb") as stream:
        while block := stream.read(8 * 1024 * 1024):
            h.update(block)
            percent = stream.tell()*100//max(1,size)
            if percent != last_percent:
                progress(f"核对电影来源 · {percent}%")
                last_percent = percent
    with connect(root) as db:
        digest = h.hexdigest()
        db.execute("INSERT OR IGNORE INTO movies(digest,name,mode) VALUES(?,?,?)", (digest,source.name,mode))
        db.execute("UPDATE movies SET mode=? WHERE digest=? AND mode='unset'", (mode,digest))
        return db.execute("SELECT * FROM movies WHERE digest=?", (digest,)).fetchone()


def extract(root, source, count, mode, progress, *, step_ms=1000, start=0., end=None, subtitle_report=None):
    root, source = Path(root), Path(source)
    if not 1 <= count <= 10000 or mode not in ("time", "train", "val"):
        raise ValueError("抽帧设置无效")
    progress("正在打开视频轨…")
    with open_movie(source,progress) as video:
        movie = register_movie(root,source,mode,progress)
        report = load_subtitles(root,movie,source,video.origin,progress)
        if subtitle_report:
            subtitle_report(report)
        return extract_frames(root,video,count,movie,progress,step_ms,start,end)


def load_subtitles(root, movie, source, origin, progress):
    from subtitles import index_movie
    try:
        warnings = index_movie(root,movie["id"],source,origin,progress)
    except OSError as exc:
        warnings = [f"字幕读取失败：{exc}"]
    with connect(root) as db:
        count = db.execute("""SELECT count(*) FROM subtitle_cues c JOIN subtitle_tracks t ON c.track=t.id
                              WHERE t.movie=?""",(movie["id"],)).fetchone()[0]
    message = f"已索引 {count} 条字幕" if count else "未找到文字字幕，可在人物框中识别画面底部字幕"
    return " · ".join([message,*warnings])


def extract_subtitles(root, source, progress):
    source = Path(source)
    with open_movie(source,progress) as video:
        movie = register_movie(root,source,"unset",progress)
        return load_subtitles(root,movie,source,video.origin,progress)


def fill_legacy_hashes(root, movie, progress):
    with connect(root) as db:
        rows = db.execute("SELECT id,path FROM frames WHERE movie=? AND content_hash IS NULL",(movie,)).fetchall()
        for i,row in enumerate(rows,1):
            progress(f"索引已有画面 · {i}/{len(rows)}")
            digest = hashlib.sha256((root/row["path"]).read_bytes()).hexdigest()
            db.execute("UPDATE frames SET content_hash=? WHERE id=?", (digest,row["id"]))


def extract_frames(root, video, count, movie, progress, step_ms, start, end):
    end = video.duration if end is None else min(end,video.duration)
    if step_ms not in LAYERS.values() or not (0 <= start < end) or not math.isfinite(end):
        raise ValueError("请检查采样密度和起止秒数，起点须在影片内且早于终点")
    movie_id = movie["id"]
    fill_legacy_hashes(root,movie_id,progress)
    lo,hi = round(start*1000), math.ceil(end*1000)
    if hi<=lo:
        raise ValueError("所选时间段太短")
    with connect(root) as db:
        used = set()
        for row in db.execute("SELECT slot,ms FROM frames WHERE movie=?", (movie_id,)):
            target = -row["slot"]-1 if row["slot"]<0 else row["slot"]*1000
            for ms in (target,row["ms"]):
                if lo<=ms<hi:
                    used.add(ms//step_ms)
    available = [s for s in range(lo//step_ms,(hi-1)//step_ms+1) if s not in used]
    rng = random.SystemRandom()
    inserted = 0
    attempts = 0
    limit = min(len(available),max(16,count*4))
    # Spread every pass across the selected interval; try more slots after duplicate pictures.
    while available and inserted<count and attempts<limit:
        n = min(count-inserted,len(available),limit-attempts)
        picks = [rng.choice(available[i*len(available)//n:(i+1)*len(available)//n]) for i in range(n)]
        occupied = set(picks)
        available = [s for s in available if s not in occupied]
        requests = []
        for s in picks:
            left,right = max(lo,s*step_ms),min(hi,(s+1)*step_ms)
            # Keep a target away from the next slot's first decoded frame when possible.
            margin = min(int(video.interval*1000), (right-left)//4)
            requests.append(rng.randrange(left,right-margin))
        if attempts:
            video.open()
        for target,(sec,image) in zip(requests,video.samples(t/1000 for t in requests),strict=True):
            attempts += 1
            progress(f"分层抽帧 · 已存 {inserted}/{count} · {sec:.2f} 秒")
            ms = round(sec*1000)
            if not lo<=ms<hi:
                continue
            buf = io.BytesIO()
            image.save(buf,format="JPEG",quality=94)
            data = buf.getvalue()
            digest = hashlib.sha256(data).hexdigest()
            path = Path("frames")/(uuid.uuid4().hex+".jpg")
            split = movie["mode"]
            if split=="time":
                # Preserve the original whole-film train/validation boundary across all layers.
                seconds = max(1,int(video.duration))
                boundary = max(1,min(seconds-1,round(seconds*.8)))
                split = "train" if ms<boundary*1000 else "val"
            try:
                with connect(root) as db:
                    db.execute("BEGIN IMMEDIATE")
                    if db.execute("SELECT 1 FROM frames WHERE movie=? AND (ms=? OR content_hash=? OR slot=?) LIMIT 1",
                                  (movie_id,ms,digest,-target-1)).fetchone():
                        continue
                    (root/path).write_bytes(data)
                    db.execute("INSERT INTO frames(movie,slot,ms,path,split,content_hash,layer_ms) VALUES(?,?,?,?,?,?,?)",
                               (movie_id,-target-1,ms,path.as_posix(),split,digest,step_ms))
                inserted += 1
            except BaseException:
                (root/path).unlink(missing_ok=True)
                raise
    return inserted


def role_names(root,movie):
    with connect(root) as db:
        names = {r[0] for r in db.execute("SELECT DISTINCT role FROM tracks t JOIN clips c ON c.id=t.clip WHERE movie=? AND role!=''",(movie,))}
        for row in db.execute("SELECT boxes FROM frames WHERE movie=? AND boxes!='[]'",(movie,)):
            names.update(b[5]["role"] for b in json.loads(row[0]) if len(b)>5 and b[5]["role"])
        for row in db.execute("SELECT voices FROM frames WHERE movie=? AND voices!='[]'",(movie,)):
            names.update(v["role"] for v in json.loads(row[0]) if v["role"])
    return sorted(names)


def export_samples(root, target, progress):
    """Publish a self-contained, ordered image/text dataset after a consistent DB read."""
    root,target = Path(root),Path(target)
    tmp = target.with_name(uuid.uuid4().hex+".tmp")
    try:
        with connect(root) as db, zipfile.ZipFile(tmp,"x",allowZip64=True) as archive:
            db.execute("BEGIN")
            names = [r[0] for r in db.execute("SELECT name FROM classes ORDER BY id")]
            rows = db.execute("""SELECT f.*,m.name AS movie_name,m.digest FROM frames f
                JOIN movies m ON m.id=f.movie WHERE state='reviewed' ORDER BY m.digest,ms,f.id""")
            total = 0
            manifest = []
            for row in rows:
                progress(f"导出已确认画面 · {total+1}")
                objects = []
                for index,box in enumerate(json.loads(row["boxes"])):
                    note = box[5] if len(box)>5 else {}
                    objects.append({"object_id":index+1,"category":names[box[0]],"bbox_xyxy":box[1:5],
                                    "character":note.get("role") or None,"dialogue":note.get("lines",[])})
                    if note.get("subtitle_refs"):
                        objects[-1]["subtitle_refs"] = note["subtitle_refs"]
                record = {"version":1,"film_id":row["digest"],"film_name":row["movie_name"],"time_ms":row["ms"],
                          "sample_interval_ms":row["layer_ms"],"split":row["split"],"image":row["path"],"objects":objects}
                record["frame_dialogue"] = [
                    {"source":v["kind"],"character":v["role"] or None,"dialogue":v["lines"],
                     "subtitle_refs":v.get("subtitle_refs",[])} for v in json.loads(row["voices"])]
                archive.write(root/row["path"],row["path"])
                manifest.append(json.dumps(record,ensure_ascii=False))
                total += 1
            if not total:
                raise ValueError("请先确认至少一张图片，再导出电影学习素材")
            archive.writestr("samples.jsonl","\n".join(manifest)+"\n",compress_type=zipfile.ZIP_DEFLATED)
            captions = db.execute("""SELECT m.digest AS film_id,t.source,t.label,c.start_ms,c.end_ms,c.text
                FROM subtitle_cues c JOIN subtitle_tracks t ON t.id=c.track JOIN movies m ON m.id=t.movie
                ORDER BY m.digest,t.source,c.start_ms,c.end_ms,c.text""")
            with archive.open("subtitles.jsonl","w",force_zip64=True) as out:
                for i,caption in enumerate(captions):
                    if i % 500 == 0:
                        progress("导出原片字幕时间轴…")
                    out.write((json.dumps(dict(caption),ensure_ascii=False)+"\n").encode("utf-8"))
            tracks = db.execute("""SELECT m.digest,t.id,t.clip,t.shot,t.role,t.observations
                FROM tracks t JOIN clips c ON c.id=t.clip JOIN movies m ON m.id=c.movie
                WHERE t.role!='' ORDER BY m.digest,c.start_ms,t.id""")
            with archive.open("tracks.jsonl","w",force_zip64=True) as out:
                for track in tracks:
                    progress("导出已命名的人物轨迹…")
                    record = {"film_id":track["digest"],"track_id":track["id"],"clip_id":track["clip"],
                              "shot":track["shot"],"character":track["role"],
                              "points":json.loads(track["observations"])}
                    out.write((json.dumps(record,ensure_ascii=False)+"\n").encode("utf-8"))
        tmp.replace(target)
        return total
    finally:
        tmp.unlink(missing_ok=True)
