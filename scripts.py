"""Import dialogue and suggest speakers by matching it to timed subtitles."""
from difflib import SequenceMatcher
import os
from pathlib import Path
import re
import unicodedata

from dataset import connect
from subtitles import decode_text


CLOCK = r"\d{1,3}:\d{2}:\d{2}(?:[.,]\d{1,3})?"
TIMED = re.compile(rf"^\[\s*({CLOCK})\s*(?:-->|[–-])\s*({CLOCK})\s*\]\s*(.*)$")
SPEECH = re.compile(r"^([^:：]{1,60})[:：]\s*(\S.*)$")
CLAUSES = re.compile(r"[。！？!?；;，,…\n]+")


def normalized(text):
    return "".join(c for c in unicodedata.normalize("NFKC",text).casefold() if c.isalnum())


def clock_ms(value):
    hours,minutes,seconds = value.replace(",",".").split(":")
    seconds,_,fraction = seconds.partition(".")
    if int(minutes)>=60 or int(seconds)>=60:
        raise ValueError(f"剧本时间码无效 · {value}")
    return (int(hours)*3600+int(minutes)*60+int(seconds))*1000+int(fraction.ljust(3,"0"))


def parse_script(content, fountain=False):
    role = ""
    for number, raw in enumerate(content.splitlines(),1):
        line = raw.strip()
        if not line:
            role = ""
            continue
        start = end = None
        timing = TIMED.match(line)
        if timing:
            start,end = map(clock_ms,timing.group(1,2))
            if end<=start:
                raise ValueError(f"剧本第 {number} 行结束时间须晚于开始时间")
            line = timing[3]
        elif re.match(r"^\[\s*\d+:\d+:\d+",line):
            raise ValueError(f"剧本第 {number} 行时间码格式无效")
        if fountain and (line.startswith(("#","=","[[","!",">","~")) or
                         line.upper().startswith(("INT.","EXT.","INT/EXT.","I/E.","EST.")) or
                         line.upper().endswith(" TO:")):
            role = ""
            continue
        if fountain and role and not timing:
            if not line.startswith(("(","（")) and normalized(line):
                yield number,role,line,start,end
        elif (not fountain or timing) and (match := SPEECH.match(line)):
            speaker,text = (v.strip() for v in match.groups())
            if speaker and normalized(text):
                yield number,speaker,text,start,end
        elif fountain and (line.startswith("@") or (line==line.upper() and any(c.isascii() and c.isalpha() for c in line))):
            role = re.sub(r"\s*\(.*\)$","",line.lstrip("@").rstrip("^ ")).strip()


def import_script(root, movie, path):
    path = Path(path).resolve()
    if path.suffix.lower() not in (".txt",".fountain"):
        raise ValueError("请选择 TXT 或 Fountain 剧本")
    entries = list(parse_script(decode_text(path.read_bytes()),path.suffix.lower()==".fountain"))
    if not entries:
        raise ValueError("未找到剧本台词。TXT 请每行使用「角色：台词」，或导入 Fountain 剧本。")
    source = os.path.normcase(str(path))
    with connect(root) as db:
        db.execute("""INSERT INTO scripts(movie,label,source) VALUES(?,?,?)
                      ON CONFLICT(movie,source) DO UPDATE SET label=excluded.label""",(movie,path.name,source))
        script = db.execute("SELECT id FROM scripts WHERE movie=? AND source=?",(movie,source)).fetchone()[0]
        db.execute("DELETE FROM script_lines WHERE script=?",(script,))
        db.executemany("INSERT INTO script_lines VALUES(?,?,?,?,?,?)",((script,*entry) for entry in entries))
    return len(entries)


def unique_speaker(rows):
    timed = [row for row in rows if row["start_ms"] is not None]
    rows = timed or rows
    return rows[0] if rows and all(row["role"]==rows[0]["role"] for row in rows) else None


def matching_line(text, lines):
    if not text:
        return None
    exact = [row for row,key in lines if key==text]
    if exact:
        return unique_speaker(exact)
    if len(text)<6:
        return None
    contained = [row for row,key in lines if text in key]
    if contained:
        return unique_speaker(contained)
    long_line = len(text)>=10
    threshold = .74 if long_line else .86
    ranked = []
    for row,key in lines:
        if min(len(key),len(text))/max(len(key),len(text))<(.7 if long_line else .8):
            continue
        match = SequenceMatcher(None,text,key,autojunk=False)
        if match.quick_ratio()>=threshold and (score := match.ratio())>=threshold:
            anchor = match.find_longest_match().size
            ranked.append((score,row,anchor))
    if not ranked:
        return None
    score,best,anchor = max(ranked,key=lambda item:item[0])
    if score<.82 and anchor<6:
        return None
    margin = .12 if score<.82 else .06
    if any(row["role"]!=best["role"] and score-other<margin for other,row,_ in ranked):
        return None
    return best


def candidates(root, movie, ms, captions):
    start = min([ms]+[c.get("start_ms",ms) for c in captions])
    end = max([ms+1]+[c.get("end_ms",ms+1) for c in captions])
    with connect(root) as db:
        rows = [dict(row) for row in db.execute("""SELECT s.id AS script_id,s.label,l.line_number,
                        l.role,l.text,l.start_ms,l.end_ms FROM script_lines l JOIN scripts s ON l.script=s.id
                        WHERE s.movie=? AND (l.start_ms IS NULL OR (l.start_ms<? AND l.end_ms>?))
                        ORDER BY s.id,l.line_number""",(movie,end,start))]
    result = [{**row,"line":row["text"]} for row in rows
              if row["start_ms"] is not None and row["start_ms"]<=ms<row["end_ms"]]
    lines = []
    if captions:
        for row in rows:
            keys = dict.fromkeys(normalized(value) for value in (row["text"],*CLAUSES.split(row["text"])))
            lines.extend((row,key) for key in keys if key)
    for caption in captions:
        left,right = caption.get("start_ms",ms),caption.get("end_ms",ms+1)
        eligible = [(row,key) for row,key in lines if row["start_ms"] is None or (row["start_ms"]<right and row["end_ms"]>left)]
        row = matching_line(normalized(caption["text"]),eligible)
        if row is None and caption.get("full_text"):
            row = matching_line(normalized(caption["full_text"]),eligible)
        if row:
            caption = {k:v for k,v in caption.items() if k!="full_text"}
            result.append({**row,"line":caption["text"],"subtitle":caption})
    return result
