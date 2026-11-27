"""Editable text storyboards and local learning from explicitly reviewed examples.

The initial director is a transparent set of text rules. Reviewed storyboards
teach a TF-IDF nearest-example director; it never rewrites dialogue or imports
another story's characters. No movie forecast is represented as a text model.
"""
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import unicodedata
import uuid


VERSION = 1
MAX_TEXT = 100000
MAX_SHOTS = 5000
MAX_CHARACTERS = 100
CHUNK_SIZE = 64
ACTIONS = ("idle", "talk", "walk", "run", "jump", "fall", "shake")
EMOTIONS = ("neutral", "happy", "angry", "sad", "shocked")
CAMERAS = ("wide", "medium", "close")
ACTION_LABELS = dict(zip(ACTIONS, ("待机", "说话", "走路", "奔跑", "跳跃", "摔倒", "抖动")))
EMOTION_LABELS = dict(zip(EMOTIONS, ("平静", "开心", "生气", "难过", "震惊")))
CAMERA_LABELS = dict(zip(CAMERAS, ("全景", "中景", "特写")))
NARRATORS = {"旁白", "叙述", "解说", "画外音", "NARRATOR", "VOICEOVER"}
QUOTES = re.compile(r'“([^”]*)”|「([^」]*)」|『([^』]*)』|"([^"\n]*)"')
SPEECH = re.compile(r"^([^：:\n]{1,30})[：:]\s*(.+)$")
SCENE = re.compile(r"^(?:(?:场景|场次|地点|背景)(?:\s*[\d一二三四五六七八九十]+)?|第[\d一二三四五六七八九十]+场)\s*[:：]\s*(.+)$"
                   r"|^(?:INT\.?/EXT\.?|INT\.|EXT\.|I/E\.|EST\.)\s+(.+)$", re.I)
CHAPTER = re.compile(r"^(?:第[\d一二三四五六七八九十百千万]+[章节幕场回].*|#{1,3}\s+.+)$")
SPEECH_VERB = r"(?:回答道|嘀咕道|说道|问道|喊道|叫道|答道|回答|嘀咕|说|问|喊|叫|道)"
ATTRIBUTION = re.compile(SPEECH_VERB + r"\s*[:：，,]*$")
SUBJECT_MODIFIERS = (r"(?:(?:笑着|笑了笑|哭着|生气地|愤怒地|惊讶地|轻声|大声|低声|无奈地|认真地|得意地|"
                     r"突然|立刻|连忙|急忙|慢慢地?|缓缓|飞快地?|高兴地|开心地|震惊得|吓得|开始|正在|猛地|忍不住|也|又))*")
NAME_TOKEN = r"(?:[A-Za-z][A-Za-z0-9_]{0,29}|[\u3400-\u9fff]{1,4}?(?:·[\u3400-\u9fff]{1,4})?)"
SUBJECT_VERBS = (SPEECH_VERB + r"|走|奔跑|狂奔|跑|跳|蹦|摔|跌|发抖|颤抖|瑟瑟发抖|"
                 r"看着|望着|瞪着|盯着|对|向|转头|回头|转过头|摇头|点头|抬头|抬起|举起|笑|哭|站|坐|在|来到|进入|回到|赶到|冲")
ACTION_SUBJECT = re.compile(r"^(" + NAME_TOKEN + r")" + SUBJECT_MODIFIERS + r"(?=(?:" + SUBJECT_VERBS + r"))")
AMBIGUOUS = {"他", "她", "它", "他们", "她们", "它们", "我", "你", "我们", "你们", "自己", "对方",
             "有人", "大家", "众人", "那人", "这人", "谁", "一个人物", "人物", "人群", "人们",
             "笑着", "哭着", "轻声", "大声", "低声", "突然", "连忙", "清晨", "随后", "于是", "现在", "今天", "明天",
             "风", "雨", "雨水", "阳光", "灯光", "镜头", "画面", "窗外", "树叶", "桌子", "椅子", "手机", "双手"}
PLACES = ("办公室", "会议室", "实验室", "卫生间", "地铁站", "电影院", "客厅", "卧室", "厨房", "餐厅", "教室",
          "学校", "操场", "公园", "街道", "马路", "大街", "小巷", "走廊", "医院", "商场", "超市", "饭店", "酒店",
          "车站", "机场", "公司", "房间", "门口", "楼下", "楼顶", "天台", "院子", "书房", "浴室", "宿舍",
          "村口", "树林", "山洞", "湖边", "河边", "工厂", "仓库", "家")
PLACE_PATTERN = "(?:" + "|".join(sorted(PLACES, key=len, reverse=True)) + ")"
TIME_PREFIX = re.compile(r"^(?:清晨|早晨|早上|上午|中午|下午|傍晚|晚上|深夜|次日|翌日|第二天|当天|随后|于是|接着|"
                         r"与此同时|过了一会儿|不一会儿|一会儿后|片刻后)\s*[，,]\s*")
LOCATION_PREFIX = re.compile(r"^在?(" + PLACE_PATTERN + r")(?:里|内|中|门口|外)?\s*[，,]\s*")
LOCATION_TRANSITION = re.compile(r"^(?:走进|走到|跑进|跑到|来到|进入|回到|赶到|冲进|冲到|奔进|站在|坐在|在)(?:了)?(" + PLACE_PATTERN + r")"
                                 r"(?=$|[，,。！？!?；;：:\s]|里|内|中|外|门|后|前|看|笑|说|发现|见|就|时|了)")


@dataclass(frozen=True)
class _StoryEntry:
    scene: str
    speaker: str
    text: str
    direction: str = ""
    actor: str = ""
    evidence: str = ""


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _text(value, name, maximum, *, empty=False):
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{name}须为不超过 {maximum} 字的有效文本")
    value = value.strip()
    if not empty and not value:
        raise ValueError(f"{name}不能为空")
    return value


def _metadata(value, name):
    """Keep bounded, inert provenance through edits and portable draft saves."""
    if not isinstance(value, dict):
        raise ValueError(f"{name}须为对象")
    pending, nodes = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > 6 or nodes > 1000:
            raise ValueError(f"{name}内容过多")
        if isinstance(item, dict):
            if any(not isinstance(key, str) or len(key) > 100 or "\x00" in key for key in item):
                raise ValueError(f"{name}字段无效")
            pending.extend((child, depth+1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth+1) for child in item)
        elif isinstance(item, str):
            if "\x00" in item:
                raise ValueError(f"{name}文本无效")
        elif item is not None and type(item) not in (int, float, bool):
            raise ValueError(f"{name}内容无效")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}内容无效") from exc
    if len(encoded.encode("utf-8")) > 16384:
        raise ValueError(f"{name}超过 16 KiB")
    return json.loads(encoded)


def validate_storyboard(board):
    """Validate before rendering, teaching, saving or exporting a storyboard."""
    if not isinstance(board, dict) or type(board.get("version")) is not int or board["version"] != VERSION:
        raise ValueError("不支持的分镜版本")
    result = {"version": VERSION, "title": _text(board.get("title"), "故事标题", 200)}
    characters, shots = board.get("characters"), board.get("shots")
    if not isinstance(characters, list) or len(characters) > MAX_CHARACTERS:
        raise ValueError(f"一个故事最多支持 {MAX_CHARACTERS} 个角色，请拆分章节")
    if not isinstance(shots, list) or not 1 <= len(shots) <= MAX_SHOTS:
        raise ValueError(f"分镜数量须为 1–{MAX_SHOTS}，请按章节生成")
    ids, names, clean_characters = set(), set(), []
    for row in characters:
        if not isinstance(row, dict):
            raise ValueError("角色格式无效")
        key, name = _text(row.get("id"), "角色编号", 80), _text(row.get("name"), "角色名", 80)
        if key in ids or name in names:
            raise ValueError("角色编号或角色名重复")
        ids.add(key)
        names.add(name)
        clean_characters.append({"id": key, "name": name})
    clean_shots, shot_ids = [], set()
    total = 0
    for row in shots:
        if not isinstance(row, dict):
            raise ValueError("分镜格式无效")
        key = _text(row.get("id"), "分镜编号", 80)
        if key in shot_ids:
            raise ValueError("分镜编号重复")
        shot_ids.add(key)
        speaker = _text(row.get("speaker", ""), "说话角色", 80, empty=True)
        if speaker and speaker not in ids:
            raise ValueError("分镜引用了不存在的角色")
        actor = _text(row.get("actor", ""), "动作角色", 80, empty=True)
        if actor and actor not in ids:
            raise ValueError("分镜动作引用了不存在的角色")
        duration = row.get("duration")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or not .5 <= duration <= 60:
            raise ValueError("每个分镜时长须为 0.5–60 秒")
        for field, allowed in (("action", ACTIONS), ("emotion", EMOTIONS), ("camera", CAMERAS)):
            if row.get(field) not in allowed:
                raise ValueError(f"不支持的分镜{field}：{row.get(field)}")
        text = _text(row.get("text"), "分镜台词或旁白", 2000)
        total += len(text)
        clean_shots.append({"id": key, "scene": _text(row.get("scene", "默认场景"), "场景", 300),
                            "speaker": speaker, "text": text, "action": row["action"], "emotion": row["emotion"],
                            "camera": row["camera"], "duration": round(float(duration), 3)})
        if "actor" in row:
            clean_shots[-1]["actor"] = actor
        if row.get("direction"):
            clean_shots[-1]["direction"] = _text(row["direction"], "动作说明", 500)
        if "source_start_ms" in row or "source_end_ms" in row:
            start, end = row.get("source_start_ms"), row.get("source_end_ms")
            if (type(start) is not int or type(end) is not int
                    or not 0 <= start < end <= 10**12):
                raise ValueError("原片时间范围无效")
            clean_shots[-1].update(source_start_ms=start, source_end_ms=end)
        if "source_kind" in row:
            clean_shots[-1]["source_kind"] = _text(row["source_kind"], "参考来源类型", 50)
        if "role_evidence" in row:
            clean_shots[-1]["role_evidence"] = _text(row["role_evidence"], "角色证据", 500, empty=True)
        if "visual_reference" in row:
            clean_shots[-1]["visual_reference"] = _metadata(row["visual_reference"], "视觉参考依据")
    if total > MAX_TEXT:
        raise ValueError(f"单次故事内容最多 {MAX_TEXT} 字，请拆分章节")
    result.update(characters=clean_characters, shots=clean_shots)
    if "source_text" in board:
        result["source_text"] = _text(board["source_text"], "原文", MAX_TEXT)
    for field in ("source_format", "source_hash"):
        if field in board:
            result[field] = _text(board[field], field, 100)
    for field in ("provenance", "reference_summary", "visual_reference_summary"):
        if field in board:
            result[field] = _metadata(board[field], field)
    return result


def _chunks(text):
    """Keep every non-whitespace character while making readable subtitle shots."""
    remaining = text.strip()
    while remaining:
        end = min(len(remaining), CHUNK_SIZE)
        if end < len(remaining):
            boundary = [i+1 for i, char in enumerate(remaining[:end]) if char in "。！？!?；;，,、 "]
            if boundary and boundary[-1] >= end // 3:
                end = boundary[-1]
        part, remaining = remaining[:end].strip(), remaining[end:].strip()
        if part:
            yield part


def _speaker_attribution(text):
    # Only the unfinished sentence adjacent to this quote can introduce it.
    # A completed post-quote tag (小明说。) must not leak into the next quote.
    tail = re.split(r"[。！？!?；;\n]", text.strip())[-1].strip("：:，, ")
    clauses = [part.strip() for part in re.split(r"[，,]", tail) if part.strip()]
    if not clauses or not (tag := ATTRIBUTION.search(clauses[-1])):
        return ""
    role = _action_subject(clauses[-1])
    if role:
        return role
    # 小明走进客厅，笑着说： has one explicit subject in the same sentence.
    # 他/她/有人说： is not an omitted subject and is deliberately unresolved.
    if not re.fullmatch(SUBJECT_MODIFIERS, clauses[-1][:tag.start()].strip()):
        return ""
    for clause in reversed(clauses[:-1]):
        role = _action_subject(clause)
        if role:
            return role
        if not re.fullmatch(SUBJECT_MODIFIERS, clause):
            break
    return ""


def _without_context(text):
    value = text.strip().lstrip("，, ")
    while (match := TIME_PREFIX.match(value)) or (match := LOCATION_PREFIX.match(value)):
        value = value[match.end():]
    return value


def _valid_prose_name(name):
    # A bounded grammatical cue is evidence of a subject, not general NER.
    # Reject pronouns, collective/descriptive phrases and common setting words.
    return (name not in AMBIGUOUS and name not in PLACES and not re.fullmatch(SUBJECT_MODIFIERS, name)
            and not any(word in name for word in ("和", "与", "以及", "的", "不", "没", "打算", "准备", "计划", "想要", "一边"))
            and not re.search(r"(?:想|要|会|能|将|正|已|才|还|再|就|都|却|忽然|终于|刚刚)$", name)
            and not name.startswith(("他", "她", "它", "我", "你", "一个", "一位", "某个", "某位", "这个", "那个")))


def _subject_match(text):
    if re.match(SUBJECT_MODIFIERS, text).end():
        return None
    match = ACTION_SUBJECT.match(text)
    return match if match and _valid_prose_name(match[1]) else None


def _action_subject(text):
    match = _subject_match(_without_context(text))
    if match:
        return match[1]
    return ""


def _narrative_scene(text):
    """Accept a local setting or an actual arrival, never a mentioned destination."""
    value = text.strip()
    while match := TIME_PREFIX.match(value):
        value = value[match.end():]
    if match := LOCATION_PREFIX.match(value):
        return match[1]
    subject = _subject_match(value)
    if subject:
        # The transition must immediately follow the named subject/modifiers.
        # 打算去/没有走进/想起厨房 cannot silently change the current setting.
        if match := LOCATION_TRANSITION.match(value[subject.end():]):
            return match[1]
    return ""


def _narrative_parts(text):
    # Keep punctuation while separating named actions performed by different
    # people; otherwise one person's jump can be applied to another actor.
    parts = []
    for sentence in re.split(r"(?<=[。！？!?；;])", text):
        start = 0
        for comma in re.finditer(r"[，,]", sentence):
            if _action_subject(sentence[comma.end():]) and _action_subject(sentence[start:comma.end()]):
                parts.append(sentence[start:comma.end()])
                start = comma.end()
        if sentence[start:].strip():
            parts.append(sentence[start:])
    return parts


def _rule_direction(text, speaking):
    action = "talk" if speaking else "idle"
    for key, words in (("fall", ("摔", "跌倒", "倒地", "滑倒")), ("run", ("跑", "狂奔", "追赶")),
                       ("jump", ("跳", "蹦")), ("shake", ("发抖", "颤抖", "瑟瑟", "震惊", "吓")),
                       ("walk", ("走", "踱步", "散步"))):
        if any(word in text for word in words):
            action = key
            break
    emotion = "neutral"
    for key, words in (("angry", ("生气", "愤怒", "怒", "可恶", "气死")),
                       ("sad", ("哭", "难过", "伤心", "悲伤", "呜呜")),
                       ("shocked", ("震惊", "惊呆", "什么", "居然", "竟然", "吓", "？！", "!?")),
                       ("happy", ("哈哈", "笑", "开心", "高兴", "太棒", "嘿嘿"))):
        if any(word in text for word in words):
            emotion = key
            break
    camera = "close" if emotion in {"angry", "sad", "shocked"} else "medium" if speaking else "wide"
    if action in {"walk", "run", "jump", "fall"}:
        camera = "wide"
    return {"action": action, "emotion": emotion, "camera": camera,
            "duration": round(min(20., max(2., len(text)/4.5 + .8)), 2)}


def _entries(content, source_format):
    scene, fountain_role, direction = "默认场景", "", ""

    def narration(segment):
        nonlocal scene
        for part in _narrative_parts(segment):
            scene = _narrative_scene(part) or scene
            subject = _action_subject(part)
            evidence = ("原文动作主语：" + part.strip())[:500] if subject else ""
            yield _StoryEntry(scene, "", part, actor=subject, evidence=evidence)

    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            fountain_role, direction = "", ""
            continue
        heading = SCENE.match(line)
        if heading or CHAPTER.match(line):
            scene = next((group for group in heading.groups() if group), "") if heading else line.lstrip("# ")
            if len(scene) > 300:
                raise ValueError("场景标题超过 300 字，请缩短标题")
            fountain_role, direction = "", ""
            continue
        if source_format == "fountain":
            if line.startswith("@") or (not fountain_role and re.fullmatch(r"[A-Z][A-Z0-9 ._'()-]{0,50}", line) and not line.endswith(" TO:")):
                fountain_role = re.sub(r"\s*\([^)]*\)$", "", line.lstrip("@").rstrip("^ ")).strip()
                continue
            if fountain_role:
                if re.fullmatch(r"[（(].+[）)]", line):
                    direction = line[1:-1]
                    continue
                role = "" if fountain_role.upper() in NARRATORS else fountain_role
                yield _StoryEntry(scene, role, line, direction, role,
                                  ("剧本角色提示：" + fountain_role)[:500] if role else "")
                continue
        match = SPEECH.match(line)
        if match and ATTRIBUTION.search(match[1]) and not QUOTES.search(line):
            role = _speaker_attribution(match[1])
            if role:
                yield from narration(line[:match.start(2)])
                yield _StoryEntry(scene, role, match[2], actor=role,
                                  evidence=("原文冒号前归属：" + match[1])[:500])
                continue
        if match and source_format != "novel":
            role, spoken = match[1].strip(), match[2].strip()
            # Prose attribution such as 小明说： must not become a character named 小明说.
            prose_attribution = bool(ATTRIBUTION.search(role))
            if prose_attribution or re.search(r"[。！？!?，,；;“”\"]", role):
                role = ""
            if role:
                evidence = "剧本角色标记：" + role
                stage = re.search(r"[（(]([^）)]*)[）)]$", role)
                stage = stage[1] if stage else ""
                role = re.sub(r"[（(][^）)]*[）)]$", "", role).strip()
                if (spoken.startswith("“") and spoken.endswith("”")) or (spoken.startswith('"') and spoken.endswith('"')):
                    spoken = spoken[1:-1]
                role = "" if role.upper() in NARRATORS else role
                # Explicit narrator lines may still contain named actions.
                if not role and not stage:
                    yield from narration(spoken)
                else:
                    yield _StoryEntry(scene, role, spoken, stage, role, evidence[:500] if role else "")
                continue
        cursor = 0
        for quoted in QUOTES.finditer(line):
            before = line[cursor:quoted.start()]
            if before.strip():
                yield from narration(before)
            role = _speaker_attribution(before)
            evidence = ("原文引号前归属：" + before.strip()[-480:]) if role else ""
            if not role:
                # A following attribution may identify this quote; do not use a
                # role from a different paragraph or an unresolved pronoun.
                after = line[quoted.end():].lstrip("，, ")
                phrase = re.split(r"[。！？!?，,；;]", after, maxsplit=1)[0]
                role = _speaker_attribution(phrase)
                if role:
                    evidence = ("原文引号后归属：" + phrase.strip())[:500]
            spoken = next(group for group in quoted.groups() if group is not None)
            if spoken.strip():
                yield _StoryEntry(scene, role, spoken, actor=role, evidence=evidence)
            cursor = quoted.end()
        if line[cursor:].strip():
            yield from narration(line[cursor:])


def generate_storyboard(text, title="新故事", *, source_format="auto", model=None, reference=None, visual_library=None):
    text = _text(text, "小说或剧本", MAX_TEXT)
    if source_format not in {"auto", "novel", "script", "fountain"}:
        raise ValueError("请选择自动识别、小说、剧本或 Fountain 格式")
    title = _text(title, "故事标题", 200)
    characters, shots, roles = [], [], {}
    reference_count, reference_warning, reference_enabled = 0, "", reference is not None
    visual_count = 0
    entries = list(_entries(text, source_format))
    for entry in entries:
        role = entry.speaker or entry.actor
        if role and role not in roles:
            identifier = f"character-{len(roles)+1}"
            roles[role] = identifier
            characters.append({"id": identifier, "name": role})
    for entry in entries:
        scene, role, text_part, direction = entry.scene, entry.speaker, entry.text, entry.direction
        for part in _chunks(text_part):
            actor = roles.get(role or entry.actor, "")
            shot = {"id": f"shot-{len(shots)+1}", "scene": scene, "speaker": roles.get(role, ""), "text": part,
                    "actor": actor, **_rule_direction((direction + " " + part).strip(), bool(role))}
            if entry.evidence:
                shot["role_evidence"] = entry.evidence
            if direction:
                shot["direction"] = direction
            reference_duration = None
            if reference_enabled:
                try:
                    from auto_learning import predict_reference_duration
                    reference_duration = predict_reference_duration(reference, part)
                    if reference_duration is not None:
                        if (isinstance(reference_duration, bool) or not isinstance(reference_duration, (int, float))
                                or not math.isfinite(reference_duration) or not .5 <= reference_duration <= 60):
                            raise ValueError("参考时长超出范围")
                        shot["duration"] = round(float(reference_duration), 3)
                except (ImportError, OSError, ValueError, KeyError, TypeError) as exc:
                    reference_enabled = False
                    reference_duration = None
                    reference_warning = f"自动时长参考不可用，已使用基础时长：{exc}"[:500]
            # Explicit stage directions are the author's instructions and take
            # priority over learned preferences for similar dialogue.
            learned = None
            if model and not direction:
                learned = _predict_direction(model, part, bool(role))
                if learned:
                    shot.update(learned)
                    reference_duration = None
            # Unreviewed visual examples supply a draft only when a reviewed
            # example has not matched. Never change text, speakers or actors.
            if visual_library and not direction and not learned:
                from video_visual_learning import predict_visual_direction
                visual = predict_visual_direction(visual_library, part)
                if visual:
                    suggestion, applied, changes = visual["direction"], [], {}
                    authored_action = shot["action"] not in {"idle", "talk"}
                    authored_emotion = shot["emotion"] != "neutral"
                    camera_terms = (("close", ("特写", "近景")), ("medium", ("中景",)), ("wide", ("全景", "远景")))
                    explicit_camera = next((value for value, words in camera_terms if any(word in part for word in words)), None)
                    if explicit_camera:
                        shot["camera"] = explicit_camera
                    for field, locked in (("action", authored_action), ("emotion", authored_emotion),
                                          ("camera", bool(explicit_camera) or authored_action)):
                        if field in suggestion and not locked:
                            changes[field] = {"before":shot[field], "after":suggestion[field]}
                            shot[field] = suggestion[field]
                            applied.append(field)
                    if "rhythm_seconds" in suggestion:
                        # Candidate cuts are approximate. Preserve reading time,
                        # use only a small bounded nudge, and retain provenance.
                        base = shot["duration"]
                        observed = suggestion["rhythm_seconds"]
                        shot["duration"] = round(min(60., max(.5, min(base*1.2, max(base*.8, observed)))), 3)
                        changes["duration"] = {"before":base, "after":shot["duration"], "observed_seconds":observed}
                        applied.append("duration")
                        reference_duration = None
                    if applied:
                        shot["visual_reference"] = {**visual["provenance"], "applied_fields": applied, "changes":changes}
                        visual_count += 1
            reference_count += reference_duration is not None
            shots.append(shot)
            if len(shots) > MAX_SHOTS:
                raise ValueError(f"分镜超过 {MAX_SHOTS} 个，请按章节生成")
    board = {"version": VERSION, "title": title, "characters": characters, "shots": shots,
             "source_text": text, "source_format": source_format, "source_hash": _hash(text)}
    if reference is not None:
        def count(key):
            value = reference.get(key, 0) if isinstance(reference, dict) else 0
            return value if type(value) is int and value >= 0 else 0
        validation = reference.get("validation") if isinstance(reference, dict) else None
        board["reference_summary"] = {"example_count": count("example_count"), "movie_count": count("movie_count"),
            "applied_shots": reference_count, "validation_kind": str((validation or {}).get("kind", "none")) if isinstance(validation, dict) else "none",
            "usable": reference.get("usable", True) is True if isinstance(reference, dict) else False,
            "reason": str(reference.get("note", ""))[:500] if isinstance(reference, dict) else "",
            "note": "仅自动参考字幕显示时长，不代表动作或剧情理解；已确认的分镜偏好优先。",
            "warning": reference_warning}
    if visual_library is not None:
        board["visual_reference_summary"] = {"example_count": visual_library.get("example_count", 0),
            "movie_count": visual_library.get("movie_count", 0), "applied_shots": visual_count,
            "review_status": "unreviewed", "validation_kind": "none",
            "note": "按相似上下文检索视觉观察；动作、表情和景别为待审核建议，候选切镜仅小幅调整节奏。人工确认偏好与明确动作优先。",
            "warning": visual_library.get("warning", "")}
    return validate_storyboard(board)


def generate_project_storyboard(root, text, title="新故事", *, source_format="auto"):
    """Load both independent learning sources; optional reference failure is recoverable."""
    reference, warning, visual_library = None, "", None
    if root is not None:
        try:
            from auto_learning import load_reference
            reference = load_reference(root)
        except (ImportError, OSError, ValueError, KeyError, TypeError) as exc:
            warning = f"自动时长参考未能读取，已使用基础时长：{exc}"[:500]
        try:
            from video_visual_learning import load_visual_library
            visual_library = load_visual_library(root)
        except (ImportError, OSError, ValueError, KeyError, TypeError) as exc:
            visual_library = {"examples": [], "example_count": 0, "movie_count": 0,
                              "warning": f"视觉参考未能读取，已使用基础编排：{exc}"[:500]}
    board = generate_storyboard(text, title, source_format=source_format,
                               model=load_style(root) if root is not None else None, reference=reference,
                               visual_library=visual_library)
    if warning:
        board["reference_summary"] = {"example_count": 0, "movie_count": 0, "applied_shots": 0,
                                      "validation_kind": "none", "warning": warning}
    return board


def reference_description(board):
    """User-facing scope, kept identical in the UI and command-line output."""
    timing = _timing_reference_description(board)
    visual = board.get("visual_reference_summary") or {}
    if visual.get("warning"):
        return " ".join(value for value in (timing, visual["warning"]) if value)
    if not visual or not visual.get("example_count"):
        return timing
    message = (f"视觉参考：{visual.get('movie_count', 0)} 部影片、{visual.get('example_count', 0)} 条观察，"
               f"用于 {visual.get('applied_shots', 0)} 个镜头的动作、表情或景别建议；候选切镜仅小幅调整节奏。"
               "尚未人工审核或独立验证，可在分镜中修正。")
    return " ".join(value for value in (timing, message) if value)


def _timing_reference_description(board):
    summary = board.get("reference_summary") or {}
    if summary.get("warning"):
        return summary["warning"]
    if not summary:
        return ""
    if summary.get("usable") is False:
        return "自动时长参考暂未启用：" + (summary.get("reason") or "有效样例不足或留出效果未优于基础时长。")
    checked = "尚无独立影片验证" if summary.get("validation_kind") in (None, "none") else "见自动学习任务中的留出评估"
    return (f"自动字幕时长参考：{summary.get('movie_count', 0)} 部影片、{summary.get('example_count', 0)} 条样例，"
            f"用于 {summary.get('applied_shots', 0)} 个镜头；{checked}。仅参考显示节奏，不代表动作理解。")


def save_storyboard(board, path):
    _atomic_json(path, validate_storyboard(board))
    return Path(path)


def load_storyboard(path):
    try:
        return validate_storyboard(json.loads(Path(path).read_text(encoding="utf-8-sig")))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ValueError("分镜文件不是有效的 UTF-8 JSON") from exc


def _terms(text):
    text = "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())
    return Counter(text[i:i+size] for size in (1, 2, 3) for i in range(len(text)-size+1))


def _vector(text, idf):
    values = {term: (1+math.log(count))*idf.get(term, 1.) for term, count in _terms(text).items()}
    norm = math.sqrt(sum(value*value for value in values.values())) or 1.
    return {term: value/norm for term, value in values.items()}


def _fit(examples):
    frequency = Counter()
    for row in examples:
        frequency.update(_terms(row["text"]).keys())
    idf = {term: 1.+math.log((1+len(examples))/(1+count)) for term, count in frequency.items()}
    return {"format": "yingge-story-director", "version": VERSION, "idf": idf,
            "examples": [{**row, "vector": _vector(row["text"], idf)} for row in examples], "threshold": .6}


def _predict_direction(model, text, speaking=False):
    if model.get("format") != "yingge-story-director" or model.get("version") != VERSION:
        raise ValueError("分镜学习模型格式不兼容")
    vector = _vector(text, model["idf"])
    candidates = []
    for row in model["examples"]:
        if bool(row.get("speaking")) != speaking:
            continue
        score = sum(value*row["vector"].get(term, 0) for term, value in vector.items())
        candidates.append((score, row))
    if not candidates:
        return None
    score, row = max(candidates, key=lambda item: item[0])
    if score < model.get("threshold", .6):
        return None
    # Durations follow approximate reading pace without overwriting the new text.
    duration = min(60., max(.5, row["duration"]*max(1, len(text))/max(1, len(row["text"]))))
    return {**{key: row[key] for key in ("action", "emotion", "camera")}, "duration": round(duration, 3)}


def _validate_model(value):
    if not isinstance(value, dict) or value.get("format") != "yingge-story-director" or value.get("version") != VERSION:
        raise ValueError("分镜学习模型格式不兼容")
    examples = value.get("examples")
    if not isinstance(examples, list) or not examples or len(examples) > 50000:
        raise ValueError("分镜学习样例无效")
    # Rebuild vector values rather than trusting arbitrary persisted numeric data.
    clean = []
    for row in examples:
        if not isinstance(row, dict):
            raise ValueError("分镜学习样例格式无效")
        shot = {key: row.get(key) for key in ("text", "action", "emotion", "camera", "duration")}
        validate_storyboard({"version": 1, "title": "校验", "characters": [],
                            "shots": [{"id": "1", "scene": "校验", "speaker": "", **shot}]})
        story = _text(row.get("story"), "故事编号", 100)
        clean.append({"key": _text(row.get("key"), "样例编号", 100),
                      "story": story, "unit": _text(row.get("unit", story), "修订单元", 100),
                      "speaking": bool(row.get("speaking")), **shot})
    fitted = _fit(clean)
    for key in ("validation", "trained_at", "example_count", "story_count"):
        if key in value:
            fitted[key] = value[key]
    return fitted


def load_style(root):
    path = Path(root)/"story_learning"/"director.json"
    if not path.exists():
        return None
    try:
        return _validate_model(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, UnicodeError, TypeError, KeyError) as exc:
        raise ValueError("分镜学习模型损坏，请备份后重新学习") from exc


def train_style(root, boards, report=lambda _: None):
    """Learn only the boards explicitly supplied by the user's review action.

    A single atomic model contains the reviewed corpus as well as vectors. New
    reviews replace the same story/shot; examples from other stories accumulate.
    Validation groups whole stories, never neighbouring shots from one story.
    """
    if not isinstance(boards, (list, tuple)) or not boards:
        raise ValueError("请先确认至少一个分镜故事")
    previous = load_style(root)
    examples = {row["key"]: {key: value for key, value in row.items() if key != "vector"}
                for row in previous["examples"]} if previous else {}
    for board in boards:
        board = validate_storyboard(board)
        story = _hash(board.get("source_text") or {"title": board["title"], "texts": [row["text"] for row in board["shots"]]})
        unit = story
        provenance = board.get("provenance") or {}
        if provenance.get("kind") == "automatic-video-reference" and provenance.get("source_hash"):
            # All reviewed parts of one film stay together at evaluation time.
            # A later review replaces only that part, not the other film parts.
            source_hash = _text(provenance["source_hash"], "原片标识", 200)
            part = provenance.get("part", 1)
            if type(part) is not int or not 1 <= part <= 100000:
                raise ValueError("自动参考分段编号无效")
            story = _hash(["video-reference", source_hash])
            unit = _hash([story, part])
        # Re-reviewing a story replaces removed shots too, avoiding stale examples.
        examples = {key: row for key, row in examples.items() if row.get("unit", row["story"]) != unit}
        for shot in board["shots"]:
            key = _hash([unit, shot["id"]])
            examples[key] = {"key": key, "story": story, "unit": unit, "speaking": bool(shot["speaker"]), **{field: shot[field]
                              for field in ("text", "action", "emotion", "camera", "duration")}}
    if len(examples) > 50000:
        raise ValueError("分镜样例超过 50000 条，请拆分学习工程")
    rows = list(examples.values())
    stories = sorted({row["story"] for row in rows})
    report(f"学习已确认分镜 · {len(stories)} 个故事 · {len(rows)} 条样例")
    validation = {"kind": "none", "sample_count": 0, "accuracy": None, "baseline_accuracy": None,
                  "note": "只有一个故事，未进行独立故事验证；样例记忆不代表对新小说的理解。"}
    if len(stories) > 1:
        # Deterministic whole-story holdout keeps large collections affordable.
        held = set(stories[::5] if len(stories) >= 5 else stories[-1:])
        training = [row for row in rows if row["story"] not in held]
        test = [row for row in rows if row["story"] in held]
        candidate = _fit(training)
        correct, baseline_correct, matched = Counter(), Counter(), 0
        for row in test:
            prediction = _predict_direction(candidate, row["text"], bool(row.get("speaking")))
            matched += prediction is not None
            baseline = _rule_direction(row["text"], row.get("speaking", False))
            prediction = prediction or baseline
            for field in ("action", "emotion", "camera"):
                correct[field] += prediction[field] == row[field]
                baseline_correct[field] += baseline[field] == row[field]
        validation = {"kind": "held-out-stories", "sample_count": len(test),
                      "train_stories": len(stories)-len(held), "test_stories": len(held),
                      "matched_examples": matched,
                      "accuracy": {field: correct[field]/len(test) for field in ("action", "emotion", "camera")},
                      "baseline_accuracy": {field: baseline_correct[field]/len(test) for field in ("action", "emotion", "camera")},
                      "note": "按完整故事留出，评估样例导演与规则回退的标签一致率；最终模型使用全部确认样例。"}
    model = _fit(rows)
    model.update(example_count=len(rows), story_count=len(stories), validation=validation,
                 trained_at=datetime.now(timezone.utc).isoformat())
    path = Path(root)/"story_learning"/"director.json"
    _atomic_json(path, model)
    report(f"分镜学习完成 · {len(rows)} 条样例；新故事会借鉴相似台词的动作、表情与镜头")
    return {key: model[key] for key in ("format", "version", "example_count", "story_count", "validation", "trained_at")} | {"model_path": str(path)}
