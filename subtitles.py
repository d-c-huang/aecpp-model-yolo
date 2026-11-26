"""Index timed text against the same video origin used by the frame sampler."""
from collections import defaultdict
import hashlib
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import uuid

import av
from PIL import Image

from dataset import connect


TEXT_CODECS = {"srt","subrip","text","ssa","ass","webvtt","mov_text"}
SIDECARS = {".srt":"srt", ".vtt":"webvtt", ".ass":"ass", ".ssa":"ass"}


def decode_text(data):
    if data.startswith((b"\xff\xfe",b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("gb18030")


def packet_text(packet):
    parts = []
    for subtitle in packet.decode():
        for rect in getattr(subtitle,"rects",(subtitle,)):
            raw = getattr(rect,"dialogue",None) or getattr(rect,"text",None)
            if not raw:
                continue
            value = raw.decode("utf-8") if isinstance(raw,bytes) else str(raw)
            value = re.sub(r"\{\\[^}]*\}", "", value).replace(r"\N","\n").replace(r"\h"," ")
            if value.strip():
                parts.append(value.strip())
    return "\n".join(parts)


def read_cues(container, streams, origin, progress):
    entries = defaultdict(list)
    last_report = -1
    for packet in container.demux(*streams):
        if packet.pts is None or not packet.size:
            continue
        start = round(1000*(packet.pts*packet.time_base-origin))
        second = start//1000
        if second >= last_report+30:
            progress(f"正在读取字幕 · {second} 秒")
            last_report = second
        end = start+round(packet.duration*packet.time_base*1000) if packet.duration else None
        text = packet_text(packet)
        # Empty packets can mark the time at which a previous caption disappears.
        entries[packet.stream.index].append((start,end,text))
    result = {}
    for stream in streams:
        cues = []
        ordered = sorted(entries[stream.index],key=lambda c:c[0])
        for i,(start,end,text) in enumerate(ordered):
            if end is None and i+1<len(ordered):
                end = ordered[i+1][0]
            start = max(0,start)
            if text and end is not None and end>start:
                cues.append((start,end,text))
        result[stream.index] = cues
    return result


def store_track(root, movie, source, label, fingerprint, cues):
    with connect(root) as db:
        db.execute("""INSERT INTO subtitle_tracks(movie,source,label,fingerprint) VALUES(?,?,?,?)
                      ON CONFLICT(movie,source) DO UPDATE SET label=excluded.label,fingerprint=excluded.fingerprint""",
                   (movie,source,label,fingerprint))
        track = db.execute("SELECT id FROM subtitle_tracks WHERE movie=? AND source=?",(movie,source)).fetchone()[0]
        db.execute("DELETE FROM subtitle_cues WHERE track=?",(track,))
        db.executemany("INSERT OR IGNORE INTO subtitle_cues(track,start_ms,end_ms,text) VALUES(?,?,?,?)",
                       ((track,*cue) for cue in cues))


def file_cues(path, data, progress):
    fmt = SIDECARS.get(path.suffix.lower())
    if not fmt:
        raise ValueError("请选择 SRT、VTT、ASS 或 SSA 字幕文件")
    with av.open(io.BytesIO(decode_text(data).encode("utf-8")),format=fmt) as sub:
        stream = sub.streams.subtitles[0]
        return read_cues(sub,[stream],0,progress)[stream.index]


def import_subtitles(root, movie, path, progress):
    path = Path(path)
    data = path.read_bytes()
    cues = file_cues(path,data,progress)
    if not cues:
        raise ValueError("字幕文件没有可用的文字或时间码")
    # Keep a manual track separate only when the movie index cannot update the same file.
    with connect(root) as db:
        source_path = db.execute("SELECT source_path FROM movies WHERE id=?",(movie,)).fetchone()[0]
    film = Path(source_path) if source_path else None
    is_sidecar = (film and path.resolve().parent==film.parent and
                  (path.stem.casefold()==film.stem.casefold() or
                   path.stem.casefold().startswith(film.stem.casefold()+".")))
    location = hashlib.sha256(os.path.normcase(str(path.resolve())).encode("utf-8")).hexdigest()
    key = "sidecar:"+path.name if is_sidecar else f"import:{location}"
    store_track(root,movie,key,path.name,hashlib.sha256(data).hexdigest(),cues)
    return len(cues)


def index_movie(root, movie, path, origin, progress):
    with connect(root) as db:
        cached = dict(db.execute("SELECT source,fingerprint FROM subtitle_tracks WHERE movie=?",(movie,)))
    warnings = []
    with av.open(str(path)) as container:
        streams = [s for s in container.streams.subtitles if s.codec_context and s.codec_context.name in TEXT_CODECS
                   and f"embedded:{s.index}" not in cached]
        if streams:
            progress("正在提取内嵌文字字幕…")
            try:
                cues = read_cues(container,streams,origin,progress)
            except (av.FFmpegError,UnicodeError) as exc:
                warnings.append(f"内嵌字幕读取失败：{exc}")
            else:
                for s in streams:
                    label = " · ".join(v for v in (f"内嵌轨 {s.index}",s.metadata.get("language"),s.metadata.get("title")) if v)
                    store_track(root,movie,f"embedded:{s.index}",label,"embedded",cues[s.index])
    prefix = path.stem.casefold()
    for file in sorted(path.parent.iterdir()):
        fmt = SIDECARS.get(file.suffix.lower())
        stem = file.stem.casefold()
        if not fmt or not (stem==prefix or stem.startswith(prefix+".")) or not file.is_file():
            continue
        try:
            data = file.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            key = "sidecar:"+file.name
            if cached.get(key)==digest:
                continue
            cues = file_cues(file,data,progress)
            store_track(root,movie,key,file.name,digest,cues)
        except (av.FFmpegError,OSError,UnicodeError,IndexError) as exc:
            warnings.append(f"{file.name} 读取失败：{exc}")
    return warnings


def candidates(root,movie,ms,*,nearby_ms=0):
    with connect(root) as db:
        rows = db.execute("""SELECT t.id AS track_id,t.source,t.label,c.start_ms,c.end_ms,c.text,c.original_text
                    FROM effective_subtitle_cues c
                    JOIN subtitle_tracks t ON c.track=t.id WHERE t.movie=? AND c.start_ms<=? AND c.end_ms>?
                    ORDER BY t.label,c.start_ms,c.end_ms,c.text,c.original_text""",(movie,ms+nearby_ms,ms-nearby_ms))
        return [dict(row) for row in rows]


def correct_cue(root,movie,cue,text):
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if not text:
        raise ValueError("请输入修正后的字幕")
    key = (cue["track_id"],cue["start_ms"],cue["end_ms"],cue["original_text"])
    with connect(root) as db:
        db.execute("BEGIN IMMEDIATE")
        exists = db.execute("""SELECT 1 FROM subtitle_cues c JOIN subtitle_tracks t ON t.id=c.track
                            WHERE c.track=? AND c.start_ms=? AND c.end_ms=? AND c.text=? AND t.movie=?""",(*key,movie))
        if exists.fetchone() is None:
            raise ValueError("原字幕已更新，请重新打开标注窗口")
        if text==cue["original_text"]:
            db.execute("DELETE FROM subtitle_corrections WHERE (track,start_ms,end_ms,original_text)=(?,?,?,?)",key)
        else:
            db.execute("""INSERT INTO subtitle_corrections VALUES(?,?,?,?,?)
                       ON CONFLICT(track,start_ms,end_ms,original_text) DO UPDATE SET text=excluded.text""",(*key,text))


def list_tracks(root):
    with connect(root) as db:
        return [dict(r) for r in db.execute("""SELECT t.id,m.name,t.label FROM subtitle_tracks t
                    JOIN movies m ON t.movie=m.id WHERE EXISTS(SELECT 1 FROM subtitle_cues WHERE track=t.id)
                    ORDER BY m.name,t.label""")]


def export_srt(root,track,path):
    def clock(ms):
        sec,fraction = divmod(ms,1000)
        minutes,seconds = divmod(sec,60)
        hours,minutes = divmod(minutes,60)
        return f"{hours:02}:{minutes:02}:{seconds:02},{fraction:03}"
    path = Path(path)
    tmp = path.with_name(uuid.uuid4().hex+".tmp")
    try:
        with connect(root) as db,tmp.open("x",encoding="utf-8-sig") as out:
            rows = db.execute("SELECT start_ms,end_ms,text FROM effective_subtitle_cues WHERE track=? ORDER BY start_ms,end_ms,text",(track,))
            for number,(start,end,text) in enumerate(rows,1):
                out.write(f"{number}\n{clock(start)} --> {clock(end)}\n{text}\n\n")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def ocr_frame(path):
    program = shutil.which("powershell.exe")
    if not program:
        raise ValueError("当前系统没有可用的 Windows OCR 环境")
    with tempfile.TemporaryDirectory() as directory:
        image_path = Path(directory)/"subtitle.png"
        with Image.open(path) as image:
            crop = image.convert("RGB").crop((0,int(image.height*.55),image.width,image.height))
            crop.thumbnail((2400,1800))
            crop.save(image_path)
        result = subprocess.run([program,"-NoProfile","-NonInteractive","-File",
            str(Path(__file__).with_name("frame_ocr.ps1")),str(image_path)],capture_output=True,
            creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0),timeout=45)
    if result.returncode:
        raise ValueError("Windows OCR 未能读取画面。请检查系统 OCR 语言包。"+
                         result.stderr.decode("utf-8","replace").strip()[:220])
    return [re.sub(r"(?<=[\u3400-\u9fff]) +(?=[\u3400-\u9fff，。！？：；、])","",line.strip())
            for line in result.stdout.decode("utf-8-sig").splitlines() if line.strip()]
