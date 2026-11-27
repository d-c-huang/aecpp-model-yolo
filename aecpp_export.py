"""Export editable, self-contained AECPP v6 projects from reviewed storyboards.

Contract: AECPP src/project/ProjectStorage.cpp and src/scene/Scene.hpp. Characters
are ordinary editable shape/text layers, not .actor skeletons. Animation uses
native transform keys; dialogue uses native subtitle cues. No movie forecast
checkpoint is presented as a story generator and no external assets are needed.
"""
import json
import math
import os
from pathlib import Path
import tempfile


ACTIONS = {"idle", "talk", "walk", "run", "jump", "fall", "shake"}
EMOTIONS = {"neutral", "happy", "angry", "sad", "shocked"}
CAMERAS = {"wide", "medium", "close"}
INK = "242633"
PALETTE = ["70B9CE", "EFA776", "A6BF8B", "C3A1CB", "D9BB68", "DF8E9C"]


def _color(value, alpha=1.):
    return dict(zip(("r", "g", "b", "a"),
                    [int(value[i:i+2], 16)/255 for i in (0, 2, 4)] + [alpha]))


def _vec(x, y):
    return {"x": round(x, 5), "y": round(y, 5)}


def _transform(x=0., y=0., rotation=0., sx=1., sy=1., opacity=1.):
    # ProjectStorage persists radians; engine/UI snapshots expose degrees.
    return {"position": _vec(x, y), "rotation": round(math.radians(rotation), 6),
            "scale": _vec(sx, sy), "opacity": opacity}


def _text(content, size=40, color=INK):
    return {"content": content, "fontFamily": "Microsoft YaHei", "fontSize": size,
            "alignment": 1, "bold": True, "color": _color(color),
            "outlineColor": _color("FFFFFF", 0), "outlineSize": 0,
            "shadowColor": _color("000000", 0), "shadowOffset": _vec(0, 0),
            "backgroundColor": _color("000000", 0)}


def _validate(board):
    if not isinstance(board, dict) or board.get("version") != 1:
        raise ValueError("请先生成有效的分镜草稿")
    characters, shots = board.get("characters"), board.get("shots")
    if not isinstance(characters, list) or len(characters) > 100:
        raise ValueError("角色列表无效，单个工程最多 100 位角色")
    names = {}
    for character in characters:
        if not isinstance(character, dict):
            raise ValueError("角色资料无效")
        identifier, name = character.get("id"), character.get("name")
        if (not isinstance(identifier, str) or not identifier or identifier in names
                or not isinstance(name, str) or not name.strip() or len(name) > 100):
            raise ValueError("角色标识重复或名称无效")
        names[identifier] = name
    if not isinstance(shots, list) or not 1 <= len(shots) <= 1000:
        raise ValueError("单个动画工程需要 1–1000 个镜头，请按章节生成")
    duration = 0.
    for shot in shots:
        if not isinstance(shot, dict):
            raise ValueError("镜头内容无效")
        seconds = shot.get("duration")
        if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
                or not math.isfinite(seconds) or not .25 <= seconds <= 120):
            raise ValueError("镜头时长须为 0.25–120 秒")
        if shot.get("speaker", "") and shot["speaker"] not in names:
            raise ValueError("镜头引用了未登记的角色，请先核对说话人")
        if shot.get("actor", "") and shot["actor"] not in names:
            raise ValueError("镜头动作引用了未登记的角色")
        for field, values in (("action", ACTIONS), ("emotion", EMOTIONS), ("camera", CAMERAS)):
            if shot.get(field) not in values:
                raise ValueError(f"镜头的 {field} 设置无效")
        if not isinstance(shot.get("text"), str) or len(shot["text"]) > 12000:
            raise ValueError("镜头台词无效或过长，请拆成多个镜头")
        if not isinstance(shot.get("scene", ""), str):
            raise ValueError("镜头场景名称无效")
        duration += seconds
    if duration > 3600:
        raise ValueError("AECPP 单个工程最长 60 分钟，请按章节分别导出")
    return names, shots, duration


def _subject(shot, names):
    if shot.get("actor"):
        return shot["actor"]
    if shot.get("speaker"):
        return shot["speaker"]
    # Narration keeps an empty subtitle speaker, but an explicit named subject
    # may own its action. Ambiguous or unnamed narration does not move everyone.
    mentioned = [identifier for identifier, name in names.items() if name in shot["text"]]
    return mentioned[0] if len(mentioned) == 1 else ""


def _setting(scene):
    value = scene.casefold()
    if any(word in value for word in ("客厅", "宿舍", "起居", "living")):
        return "living"
    if any(word in value for word in ("办公室", "教室", "会议", "office", "classroom")):
        return "office"
    if any(word in value for word in ("卧室", "房间", "室内", "厨房", "餐厅", "bedroom", "room", "int.")):
        return "room"
    if any(word in value for word in ("街", "路", "公园", "山", "河", "海", "户外", "室外", "ext.", "outdoor")):
        return "outdoor"
    return "stage"


def build_project(board, report=lambda _: None):
    """Return the actual AECPP JSON document, without touching the filesystem."""
    names, shots, duration = _validate(board)
    layers, tracks, regions, subtitles = [], [], [], []
    key_number = 0

    def key(time, transform, hold=False):
        nonlocal key_number
        key_number += 1
        return {"id": f"key_{key_number}", "time": round(time, 5),
                "interpolation": 0 if hold else 1, "transform": transform}

    def layer(name, x, y, width, height, color, *, kind=0, text=None, outline=.045):
        identifier = f"layer_{len(layers)+1}"
        item = {"id": identifier, "name": name, "contentKind": 2 if text is not None else 0,
                "visible": True, "locked": False, "zOrder": len(layers),
                "transform": _transform(x, y),
                "shape": {"kind": kind, "size": _vec(width, height), "color": _color(color),
                          "outlineColor": _color(INK, 1 if outline else 0), "outlineWidth": outline}}
        if text is not None:
            item["text"] = _text(text)
        layers.append(item)
        return item

    def track(item):
        result = {"targetLayerId": item["id"], "enabled": True, "keyframes": []}
        tracks.append(result)
        return result

    layer("背景 / 奶油纸", 0, 0, 22, 13, "F6F0DE", outline=0)
    layer("背景 / 地面", 0, -3.3, 22, 4.7, "DFE6D5", outline=0)
    layer("背景 / 地平线", 0, -.94, 22, .045, "A3AD96", outline=0)
    backgrounds = {}
    for setting in dict.fromkeys(_setting(shot.get("scene", "")) for shot in shots):
        first = len(layers)
        if setting == "outdoor":
            layer("室外 / 太阳", 7, 3.3, 1.2, 1.2, "F1D382", kind=1, outline=0)
            for x, y in ((-7, 3.1), (-6.5, 3.4), (-6, 3.1), (4.8, 3.3), (5.4, 3.5)):
                layer("室外 / 云", x, y, 1.5, .7, "FFFFFF", kind=1, outline=0)
        elif setting in {"living", "office", "room"}:
            layer("室内 / 墙脚线", 0, -.75, 22, .1, "C6BBA8", outline=0)
            layer("室内 / 窗框", 5.7, 1.9, 3.1, 2.65, "B4A68F", outline=0)
            layer("室内 / 窗玻璃", 5.7, 1.9, 2.82, 2.38, "C5DFE4", outline=0)
            layer("室内 / 窗中竖框", 5.7, 1.9, .1, 2.4, "F6F0DE", outline=0)
            layer("室内 / 窗中横框", 5.7, 1.9, 2.85, .1, "F6F0DE", outline=0)
            layer("室内 / 挂画框", -5.8, 2.3, 2.1, 1.5, "C2A882", outline=0)
            layer("室内 / 挂画", -5.8, 2.3, 1.85, 1.25, "DFB9A3", outline=0)
            layer("室内 / 画中山", -5.8, 2.2, 1.25, .8, "9BAF98", kind=2, outline=0)
            if setting == "living":
                layer("客厅 / 沙发靠背", -2.2, -.7, 6, 1.8, "B2BDA7", outline=.04)
                layer("客厅 / 沙发坐垫", -2.2, -1.65, 6, .55, "C4CDB9", outline=.04)
                for x in (-5.2, .8):
                    layer("客厅 / 沙发扶手", x, -1.25, .5, 1.4, "A4B195", outline=.04)
                for x in (-4.5, .1):
                    layer("客厅 / 沙发脚", x, -2.15, .23, .5, "8D8676", outline=0)
            elif setting == "office":
                layer("办公室 / 桌面", -3, -1.05, 5, .3, "C4A27F", outline=.035)
                for x in (-5.1, -.9):
                    layer("办公室 / 桌脚", x, -1.9, .2, 1.5, "9E8C76", outline=0)
                layer("办公室 / 电脑屏幕", -3.8, -.28, 1.3, 1, "667C82", outline=.035)
                layer("办公室 / 屏幕底座", -3.8, -.8, .75, .12, "667C82", outline=0)
            else:
                layer("房间 / 柜子", -5.7, -1.0, 2.2, 2, "C7AF8D", outline=.04)
                layer("房间 / 柜门分隔", -5.7, -1.0, .04, 1.95, "A18A6D", outline=0)
        backgrounds[setting] = [(item, track(item)) for item in layers[first:]]
        for item, _ in backgrounds[setting]:
            item["transform"]["opacity"] = 0.

    # Parts are reused across shots. Each has native keys and remains editable.
    puppets = {}
    specs = [("左腿", -.33, -1.45, .32, 1.2, INK, 0),
             ("右腿", .33, -1.45, .32, 1.2, INK, 0),
             ("左手", -.84, -.38, .32, 1.35, "FFF8E8", 0),
             ("右手", .84, -.38, .32, 1.35, "FFF8E8", 0),
             ("身体", 0, -.43, 1.36, 1.48, None, 0),
             ("头", 0, 1.03, 2.32, 2.12, "FFF8E8", 1),
             ("左眼", -.42, 1.24, .51, .55, "FFFFFF", 1),
             ("右眼", .42, 1.24, .51, .55, "FFFFFF", 1),
             ("左瞳孔", -.38, 1.21, .17, .23, INK, 1),
             ("右瞳孔", .46, 1.21, .17, .23, INK, 1),
             ("左眉", -.44, 1.69, .64, .10, INK, 0),
             ("右眉", .44, 1.69, .64, .10, INK, 0),
             ("嘴", 0, .58, .58, .19, INK, 1)]
    for index, (identifier, name) in enumerate(names.items()):
        parts = []
        for part, x, y, w, h, color, kind in specs:
            item = layer(f"{name} / {part}", x, y, w, h, color or PALETTE[index % len(PALETTE)], kind=kind)
            item["transform"]["opacity"] = 0.
            parts.append((part, x, y, item, track(item)))
        puppets[identifier] = parts

    def pose(part, x, y, anchor, scale, action, emotion, phase, progress, speaking):
        dx = dy = angle = rotation = 0.
        sy = 1.
        if action in {"walk", "run"}:
            dx = -1.4 + 2.8 * progress
            dy = abs(math.sin(phase)) * (.17 if action == "walk" else .3)
            if part in {"左腿", "右腿", "左手", "右手"}:
                rotation = math.sin(phase) * (24 if action == "walk" else 42) * (-1 if part.startswith("左") else 1)
        elif action == "jump":
            # Keep the exaggerated head below the scene heading even in close-up.
            dy = min(1.5, max(.2, 3.5/scale-2.09)) * max(0, math.sin(progress*math.pi))
            if part.endswith("手"):
                rotation = -55 if part.startswith("左") else 55
        elif action == "fall":
            angle = -85 * min(1., progress*2)
            dy = -.55 * min(1., progress*2)
        elif action == "shake":
            dx = math.sin(phase) * .18
            angle = math.sin(phase)*6
        else:
            dy = math.sin(phase)*.045
        if part in {"左眉", "右眉"}:
            rotation += {"angry": 22, "sad": -22, "shocked": 0, "happy": 12}.get(emotion, 0) * (-1 if part.startswith("左") else 1)
            if emotion == "shocked":
                y += .2
        if part.endswith("眼") and emotion == "happy":
            sy = .35
        if part.endswith("瞳孔") and emotion == "shocked":
            sy = .65
        if part == "嘴":
            if emotion == "shocked":
                sy = 3.
            elif speaking:
                sy = 1. + 1.3 * abs(math.sin(phase*1.7))
            elif emotion == "sad":
                sy = .45
        radians = math.radians(angle)
        px, py = x*math.cos(radians)-y*math.sin(radians), x*math.sin(radians)+y*math.cos(radians)
        return _transform(anchor + (px+dx)*scale, -.2+(py+dy)*scale,
                          rotation+angle, scale, sy*scale)

    start, previous_scene, previous_speaker = 0., None, ""
    for index, shot in enumerate(shots):
        report(f"编排 AECPP 镜头 · {index+1}/{len(shots)}")
        seconds, end = float(shot["duration"]), start+float(shot["duration"])
        speaker, scene = shot.get("speaker", ""), shot.get("scene", "")
        for setting, items in backgrounds.items():
            for item, animation in items:
                transform = {**item["transform"], "opacity": float(setting == _setting(scene))}
                animation["keyframes"].append(key(start, transform, True))
        subject = _subject(shot, names)
        selected = [subject] if subject else []
        if speaker and speaker not in selected:
            selected.append(speaker)
        if len(selected) < 2 and previous_scene == scene and previous_speaker and previous_speaker not in selected:
            selected.append(previous_speaker)
        if not selected:
            selected = list(names)[:2]
        scale = {"wide": .9, "medium": 1.15, "close": 1.5}[shot["camera"]]
        if shot["camera"] == "close" and subject:
            selected = [subject]
        active = set(selected)
        for identifier, parts in puppets.items():
            if identifier not in active:
                for _, x, y, item, animation in parts:
                    animation["keyframes"].append(key(start, _transform(x, y, opacity=0), True))
                continue
            anchor = 0 if len(selected) == 1 else (-3.25 if selected.index(identifier) == 0 else 3.25)
            is_speaker = identifier == speaker
            action = shot["action"] if identifier == subject else "idle"
            emotion = shot["emotion"] if identifier == subject else "neutral"
            samples = 16 if action in {"walk", "run", "shake", "talk"} else 8
            for sample in range(samples+1):
                progress = sample/samples
                # Last pose is just before the hard shot boundary, preventing blending.
                at = min(end-.0001, start+progress*seconds)
                phase = progress * math.pi * (8 if action == "run" else 4)
                for part, x, y, item, animation in parts:
                    animation["keyframes"].append(key(at, pose(part, x, y, anchor, scale, action,
                        emotion, phase, progress, is_speaker), sample == samples))
        title = layer(f"镜头 {index+1} / 场景标题", 0, 3.9, 14, .6, INK,
                      text=f"{index+1:02d}  {scene or board.get('title') or '故事'}")
        title["text"]["fontSize"] = 38
        title["transform"]["opacity"] = 0.
        heading = track(title)
        if start:
            heading["keyframes"].append(key(0, _transform(0, 3.9, opacity=0), True))
        heading["keyframes"].extend([key(start, _transform(0, 3.9), True),
                                     key(end, _transform(0, 3.9, opacity=0), True)])
        text = (names[speaker]+"：" if speaker else "") + shot["text"]
        # Native subtitle renderer wraps text to the camera width.
        style = _text(text, 48, "FFFFFF")
        style.update(outlineColor=_color(INK), outlineSize=3)
        subtitles.append({"id": f"subtitle_{index+1}", "locked": False,
                          "start": round(start, 5), "end": round(end-.0001, 5), "style": style})
        regions.append({"id": f"shot_{index+1}", "name": f"{index+1:02d} · {scene or '镜头'}",
                        "start": round(start, 5), "end": round(end, 5)})
        previous_scene, previous_speaker, start = scene, subject or (previous_speaker if previous_scene == scene else ""), end
    for parts in puppets.values():
        for _, x, y, item, animation in parts:
            animation["keyframes"].append(key(duration, _transform(x, y, opacity=0), True))
    return {"format": "aecpp-project", "version": 6,
            "scene": {"name": str(board.get("title") or "小说动画"),
                      "sceneBounds": {"left": -10, "right": 10, "bottom": -5.625, "top": 5.625},
                      "selectedLayerId": layers[0]["id"], "nextLayerId": len(layers)+1,
                      "selectedAudioId": "", "audioTracks": [],
                      "camera": {"transform": _transform(), "locked": False, "enabled": True,
                                 "keyframes": []},
                      "timelineRegions": regions, "timelineGroups": [], "subtitles": subtitles,
                      "transitions": [], "weather": [], "layers": layers,
                      "animation": {"name": str(board.get("title") or "小说动画"),
                                    "duration": round(duration, 5), "fps": 24, "tracks": tracks}}}


def export_project(board, target, report=lambda _: None):
    """Atomically publish a new .aecpp file; existing projects are never replaced."""
    target = Path(target).absolute()
    if target.suffix.lower() != ".aecpp":
        raise ValueError("请选择 .aecpp 工程文件")
    if target.exists() or target.is_symlink():
        raise ValueError("工程文件已存在，请选择新名称，以保留原来的手工修改")
    project = build_project(board, report)
    payload = json.dumps(project, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(payload) > 64*1024*1024:
        raise ValueError("工程超过 AECPP 的 64 MB 上限，请按章节分别导出")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".yingge-aecpp-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        report("正在保存可编辑动画工程…")
        # Windows rename is exclusive and also works on exFAT/FAT removable disks.
        # POSIX rename replaces targets, so use exclusive hard-link publication there.
        try:
            (os.rename if os.name == "nt" else os.link)(temporary, target)
        except FileExistsError:
            raise ValueError("工程文件已存在，请选择新名称") from None
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target


def to_script(board):
    """AECPP structured-script v1, for binding real .actor resources and voices.

Action/expression are suggestions, deliberately not unverified resource IDs.
The desktop script editor validates bindings before generating an actor scene.
"""
    names, shots, _ = _validate(board)
    if len(names) > 32 or len(shots) > 64:
        raise ValueError("AECPP 结构化剧本最多 32 个角色、64 个镜头，请按章节导出")
    action_names = dict(idle="待机", talk="说话", walk="走路", run="跑步", jump="跳跃", fall="摔倒", shake="夸张抖动")
    emotion_names = dict(neutral="平静", happy="开心", angry="愤怒", sad="难过", shocked="震惊")
    # Use fresh ASCII IDs accepted by the native script contract.
    ids = {identifier: f"character_{index+1}" for index, identifier in enumerate(names)}
    scenes = []
    previous_scene, previous_speaker = None, ""
    total_text = 0
    for index, shot in enumerate(shots):
        text = shot["text"]
        total_text += len(text)
        if len(text) > 4000 or total_text > 120000:
            raise ValueError("台词超出 AECPP 剧本限制，请拆分长台词或章节")
        speaker, location = shot.get("speaker", ""), shot.get("scene", "")
        subject = _subject(shot, names)
        cast = [subject] if subject else []
        if speaker and speaker not in cast:
            cast.append(speaker)
        if len(cast) < 2 and location == previous_scene and previous_speaker and previous_speaker not in cast:
            cast.append(previous_speaker)
        if not cast:
            cast = list(names)[:2]
        if shot["camera"] == "close" and subject and (not speaker or speaker == subject):
            cast = [subject]
        members = []
        for person_index, identifier in enumerate(cast):
            selected = identifier == subject
            x = 0 if len(cast) == 1 else -3 if person_index == 0 else 3
            member = {"characterId": ids[identifier], "x": x, "y": -.5,
                      "height": {"wide": 3.5, "medium": 4.5, "close": 6}[shot["camera"]],
                      "facing": "right" if person_index == 0 else "left",
                      "action": action_names[shot["action"]] if selected else "待机",
                      "expression": emotion_names[shot["emotion"]] if selected else "平静"}
            if selected and shot["action"] in {"walk", "run"}:
                member["movement"] = {"start": 0, "duration": shot["duration"], "toX": x+1.5,
                                      "toY": -.5, "easing": "linear", "autoFacing": True}
            members.append(member)
        scenes.append({"id": f"scene_{index+1}", "title": location[:200] or f"镜头 {index+1}",
                       "description": f"{action_names[shot['action']]}；{emotion_names[shot['emotion']]}",
                       "duration": shot["duration"], "background": {"color": "#F6F0DE"},
                       "characters": members,
                       "dialogue": [{"id": f"dialogue_{index+1}", "characterId": ids.get(speaker, ""),
                                     "text": text, "start": 0, "duration": shot["duration"]}]})
        previous_scene, previous_speaker = location, subject or (previous_speaker if previous_scene == location else "")
    return {"version": 1, "title": str(board.get("title") or "小说动画")[:200],
            "characters": [{"id": ids[identifier], "name": name} for identifier, name in names.items()],
            "scenes": scenes}


def export_script(board, target, report=lambda _: None):
    """Save AECPP's importable structured script without overwriting old edits."""
    target = Path(target).absolute()
    if target.suffix.lower() != ".json":
        raise ValueError("请选择 .json 剧本文件")
    document = to_script(board)
    if target.exists() or target.is_symlink():
        raise ValueError("剧本文件已存在，请选择新名称")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".yingge-script-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(document, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        report("正在保存 AECPP 结构化剧本…")
        try:
            (os.rename if os.name == "nt" else os.link)(temporary, target)
        except FileExistsError:
            raise ValueError("剧本文件已存在，请选择新名称") from None
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target
