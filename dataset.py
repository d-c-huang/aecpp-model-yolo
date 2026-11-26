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
        CREATE TABLE IF NOT EXISTS movie_cast(movie INTEGER NOT NULL REFERENCES movies(id),
            name TEXT NOT NULL,position INTEGER NOT NULL,PRIMARY KEY(movie,name));
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
        CREATE TABLE IF NOT EXISTS subtitle_corrections(track INTEGER NOT NULL REFERENCES subtitle_tracks(id),
            start_ms INTEGER NOT NULL,end_ms INTEGER NOT NULL,original_text TEXT NOT NULL,text TEXT NOT NULL,
            PRIMARY KEY(track,start_ms,end_ms,original_text));
        CREATE VIEW IF NOT EXISTS effective_subtitle_cues AS
            SELECT c.track,c.start_ms,c.end_ms,c.text AS original_text,COALESCE(r.text,c.text) AS text
            FROM subtitle_cues c LEFT JOIN subtitle_corrections r
            ON r.track=c.track AND r.start_ms=c.start_ms AND r.end_ms=c.end_ms AND r.original_text=c.text;
        CREATE TABLE IF NOT EXISTS scripts(id INTEGER PRIMARY KEY,movie INTEGER NOT NULL REFERENCES movies(id),
            label TEXT NOT NULL,source TEXT NOT NULL,UNIQUE(movie,source));
        CREATE TABLE IF NOT EXISTS script_lines(script INTEGER NOT NULL REFERENCES scripts(id),
            line_number INTEGER NOT NULL,role TEXT NOT NULL,text TEXT NOT NULL,start_ms INTEGER,end_ms INTEGER,
            PRIMARY KEY(script,line_number));
        """)
        db.execute("INSERT OR IGNORE INTO classes VALUES(0,'人物')")
        columns = {row["name"] for row in db.execute("PRAGMA table_info(frames)")}
        if "content_hash" not in columns:
            db.execute("ALTER TABLE frames ADD COLUMN content_hash TEXT")
        if "layer_ms" not in columns:
            db.execute("ALTER TABLE frames ADD COLUMN layer_ms INTEGER NOT NULL DEFAULT 1000")
        if "voices" not in columns:
            db.execute("ALTER TABLE frames ADD COLUMN voices TEXT NOT NULL DEFAULT '[]'")
        if "batch_id" not in columns:
            db.execute("ALTER TABLE frames ADD COLUMN batch_id TEXT NOT NULL DEFAULT ''")
            # Earlier releases stored empty machine results as drafts, blocking a retry.
            db.execute("UPDATE frames SET state='new' WHERE state='predicted' AND boxes='[]' AND voices='[]'")
        if "deleted" not in columns:
            db.execute("ALTER TABLE frames ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")
        movie_columns = {row["name"] for row in db.execute("PRAGMA table_info(movies)")}
        for name, kind in (("source_path","TEXT"),("source_size","INTEGER"),("source_mtime","INTEGER")):
            if name not in movie_columns:
                db.execute(f"ALTER TABLE movies ADD COLUMN {name} {kind}")
        db.execute("CREATE INDEX IF NOT EXISTS frame_picture ON frames(movie,content_hash)")
        db.execute("CREATE INDEX IF NOT EXISTS frame_time ON frames(movie,ms)")
        db.execute("CREATE INDEX IF NOT EXISTS frame_batch ON frames(movie,batch_id,state,split)")


def classes(root):
    with connect(root) as db:
        return [row[0] for row in db.execute("SELECT name FROM classes ORDER BY id")]


def add_class(root, name):
    name = name.strip()
    if not name or len(name) > 64 or any(ord(c) < 32 for c in name):
        raise ValueError("类别名称需为 1–64 个可显示字符")
    with connect(root) as db:
        db.execute("INSERT OR IGNORE INTO classes(name) VALUES(?)", (name,))


def frames(root, movie=None, *, deleted=False):
    query = "SELECT f.*, m.name AS film FROM frames f JOIN movies m ON f.movie=m.id WHERE f.deleted=?"
    params = (int(deleted),)
    if movie is not None:
        query += " AND f.movie=?"
        params += (movie,)
    with connect(root) as db:
        return [dict(row) for row in db.execute(query+" ORDER BY f.id",params)]


def frame_batches(root, movie):
    with connect(root) as db:
        return [row[0] for row in db.execute("SELECT batch_id FROM frames WHERE movie=? GROUP BY batch_id ORDER BY MIN(id)",(movie,))]


def set_frames_deleted(root, movie, frame_ids, *, deleted):
    flag = int(deleted)
    with connect(root) as db:
        return db.executemany("UPDATE frames SET deleted=? WHERE movie=? AND id=? AND deleted!=?",
                              ((flag,movie,frame,flag) for frame in frame_ids)).rowcount


def prediction_frames(root, movie, batch_id, *, frame_id=None, allow_validation=False):
    if allow_validation and frame_id is None:
        raise ValueError("验证集辅助识别须指定一张截图")
    with connect(root) as db:
        return db.execute("""SELECT id,path FROM frames WHERE movie=? AND batch_id=?
                            AND state='new' AND (split='train' OR (? AND split='val'))
                            AND deleted=0 AND (? IS NULL OR id=?)
                            ORDER BY id""",(movie,batch_id,allow_validation,frame_id,frame_id)).fetchall()


def movies(root):
    with connect(root) as db:
        return [dict(row) for row in db.execute("SELECT * FROM movies ORDER BY id")]


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
        query = f"UPDATE frames SET {fields} WHERE id=? AND deleted=0"
        if only_new:
            query += " AND state='new'"
        return db.execute(query, (*values,frame_id)).rowcount


def register_movie(root, source, mode, progress):
    # Keep the old content identity, including after moving a file or upgrading a project.
    source = Path(source).resolve()
    stat = source.stat()
    h = hashlib.sha256()
    last_percent = -1
    with source.open("rb") as stream:
        while block := stream.read(8 * 1024 * 1024):
            h.update(block)
            percent = stream.tell()*100//max(1,stat.st_size)
            if percent != last_percent:
                progress(f"核对电影来源 · {percent}%")
                last_percent = percent
    after = source.stat()
    if (after.st_size,after.st_mtime_ns) != (stat.st_size,stat.st_mtime_ns):
        raise ValueError("核对时电影文件发生变化，请重新导入")
    with connect(root) as db:
        digest = h.hexdigest()
        db.execute("INSERT OR IGNORE INTO movies(digest,name,mode) VALUES(?,?,?)", (digest,source.name,mode))
        db.execute("""UPDATE movies SET source_path=?,source_size=?,source_mtime=?,
                      mode=CASE WHEN mode='unset' THEN ? ELSE mode END WHERE digest=?""",
                   (str(source),stat.st_size,stat.st_mtime_ns,mode,digest))
        return db.execute("SELECT * FROM movies WHERE digest=?", (digest,)).fetchone()


def import_movie(root, source, mode, progress):
    source = Path(source)
    with open_movie(source,progress) as video:
        movie = register_movie(root,source,mode,progress)
        return movie["id"],load_subtitles(root,movie,source,video.origin,progress)


def generate_screenshots(root, movie, count, progress, *, step_ms=1000, start=0., end=None, at=None, next_batch=False):
    root = Path(root)
    if at is None and not 1 <= count <= 10000:
        raise ValueError("截图数量须在 1–10000 之间")
    if not movie["source_path"]:
        raise ValueError("此旧工程尚未记录电影路径，请用「导入电影」定位原片")
    source = Path(movie["source_path"])
    try:
        stat = source.stat()
    except OSError as exc:
        raise ValueError("找不到原片，请用「导入电影」重新定位") from exc
    if (stat.st_size,stat.st_mtime_ns) != (movie["source_size"],movie["source_mtime"]):
        raise ValueError("电影源文件已发生变化，请用「导入电影」重新核对")
    progress("正在打开视频轨…")
    batch_id = uuid.uuid4().hex
    with open_movie(source,progress) as video:
        if movie["mode"]=="unset":
            with connect(root) as db:
                db.execute("UPDATE movies SET mode='time' WHERE id=? AND mode='unset'",(movie["id"],))
                movie = db.execute("SELECT * FROM movies WHERE id=?",(movie["id"],)).fetchone()
        if at is not None:
            if not 0<=at<video.duration:
                raise ValueError("截图时间须位于影片内")
            fill_legacy_hashes(root,movie["id"],progress)
            sec,image = next(video.samples([at]))
            frame,created = store_frame(root,movie,video,round(at*1000),round(sec*1000),image,0,batch_id=batch_id)
            return int(created),frame
        inserted,first = extract_frames(root,video,count,movie,progress,step_ms,start,end,batch_id=batch_id)
        if next_batch and inserted<count:
            added,first_extra = scan_remaining_frames(root,video,movie,count-inserted,progress,start,end,batch_id=batch_id)
            return inserted+added,first or first_extra
        return inserted,first


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
        return extract_frames(root,video,count,movie,progress,step_ms,start,end,batch_id=uuid.uuid4().hex)[0]


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


def store_frame(root, movie, video, target, ms, image, step_ms, *, batch_id, known_content=None):
    buf = io.BytesIO()
    image.save(buf,format="JPEG",quality=94)
    data = buf.getvalue()
    digest = hashlib.sha256(data).hexdigest()
    if known_content is not None and digest in known_content:
        return known_content[digest],False
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
            duplicate = db.execute("""SELECT id,deleted FROM frames WHERE movie=? AND (ms=? OR content_hash=? OR slot=?)
                                       ORDER BY ms=? DESC,id LIMIT 1""",
                                   (movie["id"],ms,digest if step_ms else None,-target-1,ms)).fetchone()
            if duplicate:
                return None if duplicate["deleted"] else duplicate["id"],False
            (root/path).write_bytes(data)
            frame = db.execute("""INSERT INTO frames(movie,slot,ms,path,split,content_hash,layer_ms,batch_id)
                               VALUES(?,?,?,?,?,?,?,?)""",
                               (movie["id"],-target-1,ms,path.as_posix(),split,digest,step_ms,batch_id)).lastrowid
    except BaseException:
        (root/path).unlink(missing_ok=True)
        raise
    if known_content is not None:
        known_content[digest] = frame
    return frame,True


def extract_frames(root, video, count, movie, progress, step_ms, start, end, *, batch_id):
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
    first = None
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
            frame,created = store_frame(root,movie,video,target,ms,image,step_ms,batch_id=batch_id)
            if created:
                inserted += 1
                first = first or frame
    return inserted,first


def scan_remaining_frames(root, video, movie, count, progress, start, end, *, batch_id):
    end = video.duration if end is None else min(end,video.duration)
    with connect(root) as db:
        existing = db.execute("SELECT id,ms,content_hash FROM frames WHERE movie=?",(movie["id"],)).fetchall()
    used_times = {row["ms"] for row in existing}
    known_content = {row["content_hash"]:row["id"] for row in existing}
    interval = max(1,round(video.interval*1000))
    inserted = 0
    first = None
    for i,(seconds,frame) in enumerate(video.frames_between(start,end)):
        if i%30==0:
            progress(f"逐帧查找新画面 · 已补 {inserted}/{count} · {seconds:.2f} 秒")
        ms = round(seconds*1000)
        if ms in used_times:
            continue
        frame_id,created = store_frame(root,movie,video,ms,ms,video.frame_image(frame),interval,
                                       batch_id=batch_id,known_content=known_content)
        if created:
            used_times.add(ms)
            inserted += 1
            first = first or frame_id
            if inserted==count:
                break
    return inserted,first


def cast_names(root,movie):
    with connect(root) as db:
        return [row[0] for row in db.execute("SELECT name FROM movie_cast WHERE movie=? ORDER BY position",(movie,))]


def save_cast(root,movie,content):
    names = {}
    for number,line in enumerate(content.splitlines(),1):
        name = line.strip()
        if not name:
            continue
        if len(name)>64 or any(ord(c)<32 for c in name):
            raise ValueError(f"演员表第 {number} 行需为 1–64 个可显示字符")
        names.setdefault(name,number)
    with connect(root) as db:
        db.execute("DELETE FROM movie_cast WHERE movie=?",(movie,))
        db.executemany("INSERT INTO movie_cast(movie,name,position) VALUES(?,?,?)",
                       ((movie,name,position) for name,position in names.items()))
    return len(names)


def role_names(root,movie):
    cast = cast_names(root,movie)
    with connect(root) as db:
        names = {r[0] for r in db.execute("SELECT DISTINCT role FROM tracks t JOIN clips c ON c.id=t.clip WHERE movie=? AND role!=''",(movie,))}
        for row in db.execute("SELECT boxes FROM frames WHERE movie=? AND deleted=0 AND boxes!='[]'",(movie,)):
            names.update(b[5]["role"] for b in json.loads(row[0]) if len(b)>5 and b[5]["role"])
        for row in db.execute("SELECT voices FROM frames WHERE movie=? AND deleted=0 AND voices!='[]'",(movie,)):
            names.update(v["role"] for v in json.loads(row[0]) if v["role"])
        names.update(r[0] for r in db.execute("""SELECT DISTINCT l.role FROM script_lines l
                     JOIN scripts s ON l.script=s.id WHERE s.movie=?""",(movie,)))
    return [*cast,*sorted(names.difference(cast))]


def export_samples(root, target, progress):
    """Publish a self-contained, ordered image/text dataset after a consistent DB read."""
    root,target = Path(root),Path(target)
    tmp = target.with_name(uuid.uuid4().hex+".tmp")
    try:
        with connect(root) as db, zipfile.ZipFile(tmp,"x",allowZip64=True) as archive:
            db.execute("BEGIN")
            names = [r[0] for r in db.execute("SELECT name FROM classes ORDER BY id")]
            rows = db.execute("""SELECT f.*,m.name AS movie_name,m.digest FROM frames f
                JOIN movies m ON m.id=f.movie WHERE state='reviewed' AND f.deleted=0 ORDER BY m.digest,ms,f.id""")
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
                    if note.get("script_refs"):
                        objects[-1]["script_refs"] = note["script_refs"]
                record = {"version":1,"film_id":row["digest"],"film_name":row["movie_name"],"time_ms":row["ms"],
                          "sample_interval_ms":row["layer_ms"],"split":row["split"],"image":row["path"],"objects":objects}
                record["frame_dialogue"] = [
                    {"source":v["kind"],"character":v["role"] or None,"dialogue":v["lines"],
                     "subtitle_refs":v.get("subtitle_refs",[]),**({"script_refs":v["script_refs"]}
                     if v.get("script_refs") else {})} for v in json.loads(row["voices"])]
                archive.write(root/row["path"],row["path"])
                manifest.append(json.dumps(record,ensure_ascii=False))
                total += 1
            if not total:
                raise ValueError("请先确认至少一张图片，再导出电影学习素材")
            archive.writestr("samples.jsonl","\n".join(manifest)+"\n",compress_type=zipfile.ZIP_DEFLATED)
            captions = db.execute("""SELECT m.digest AS film_id,t.source,t.label,c.start_ms,c.end_ms,c.text
                FROM effective_subtitle_cues c JOIN subtitle_tracks t ON t.id=c.track JOIN movies m ON m.id=t.movie
                ORDER BY m.digest,t.source,c.start_ms,c.end_ms,c.text""")
            with archive.open("subtitles.jsonl","w",force_zip64=True) as out:
                for i,caption in enumerate(captions):
                    if i % 500 == 0:
                        progress("导出原片字幕时间轴…")
                    out.write((json.dumps(dict(caption),ensure_ascii=False)+"\n").encode("utf-8"))
            lines = db.execute("""SELECT m.digest AS film_id,s.id AS script_id,s.label,l.line_number,l.role,l.text,l.start_ms,l.end_ms
                FROM script_lines l JOIN scripts s ON l.script=s.id JOIN movies m ON s.movie=m.id
                ORDER BY m.digest,s.label,s.id,l.line_number""")
            with archive.open("scripts.jsonl","w",force_zip64=True) as out:
                for i,line in enumerate(lines):
                    if i % 500 == 0:
                        progress("导出电影剧本…")
                    out.write((json.dumps(dict(line),ensure_ascii=False)+"\n").encode("utf-8"))
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
