"""Deterministic casting and matching of existing AECPP resources.

Nothing is generated or written here. Every non-empty selection is a catalogue
ID, except an invalid explicit user choice which remains visible as a blocking
issue. Empty selections mean unassigned in this automatic route; the separate
strict/manual build_binding_plan contract is unchanged.
"""
import copy
import re

from aecpp_resources import build_binding_plan, clean_bindings
from story_pipeline import validate_storyboard


ACTION_SYNONYMS = {
    "idle": ("stand", "standing", "rest", "站立", "待机", "自然站立", "静止"),
    "talk": ("speak", "speaking", "dialogue", "说话", "讲话", "对白"),
    "walk": ("walking", "行走", "走路", "步行"),
    "run": ("running", "跑步", "奔跑", "快跑"),
    "jump": ("jumping", "跳跃", "跳起", "蹦跳"),
    "fall": ("fall_down", "falling", "跌倒", "摔倒", "倒地"),
    "shake": ("shiver", "tremble", "shaking", "颤抖", "发抖", "瑟瑟发抖"),
}
ACTION_SIMILAR = {
    "idle": (), "talk": ("talk_explain", "talk_shrug", "talk_lean", "sit_talk"),
    "walk": ("stroll", "march", "sneak", "hop_walk", "limp"),
    "run": ("walk", "stroll", "march"), "jump": ("hop_walk", "raise_hand"),
    "fall": ("hit_react", "stagger", "sit_down", "bow"),
    "shake": ("shiver", "tremble", "surprised", "shake_head"),
}
EMOTION_SYNONYMS = {
    "neutral": ("normal", "自然", "平静", "普通", "默认"),
    "happy": ("smile", "开心", "高兴", "快乐", "微笑"),
    "sad": ("悲伤", "伤心", "难过"), "angry": ("愤怒", "生气", "发怒"),
    "shocked": ("惊呆", "震惊", "惊愕"),
}
EMOTION_SIMILAR = {
    "neutral": ("talk", "thinking"), "happy": ("laugh", "excited", "celebratory", "大笑", "兴奋"),
    "sad": ("cry", "hurt", "哭泣"), "angry": ("furious", "annoyed", "暴怒", "不耐烦"),
    "shocked": ("surprised", "stunned", "fearful", "惊讶", "呆住"),
}
PLACE_WORDS = (
    ("教室", "课堂", "课室", "classroom"), ("学校", "校园", "school"),
    ("卧室", "寝室", "睡房", "bedroom"), ("客厅", "起居室", "livingroom"),
    ("厨房", "灶房", "kitchen"), ("医院", "诊所", "诊疗所", "hospital"),
    ("街道", "街头", "马路", "大街", "路口", "道路", "street"),
    ("森林", "树林", "竹林", "林间", "forest"), ("山顶", "山巅", "山峰", "悬崖", "崖边", "mountain"),
    ("河边", "河岸", "河畔", "河流", "river"), ("海边", "海滨", "沙滩", "海岸", "beach"),
    ("公园", "park"), ("办公室", "办公楼", "公司", "office"),
    ("集市", "市场", "菜场", "market"), ("村庄", "乡村", "村落", "village"),
)
INDOOR = ("室内", "屋内", "房间", "教室", "卧室", "寝室", "客厅", "厨房", "书房", "办公室", "屋里", "indoor")
OUTDOOR = ("室外", "屋外", "街", "路口", "道路", "公园", "山", "河岸", "田野", "树林", "森林", "门口", "outdoor")
DAY = ("白天", "日间", "清晨", "早晨", "上午", "中午", "下午", "阳光", "daytime")
NIGHT = ("夜", "深更", "月光", "月色", "night")


def _normal(value):
    return re.sub(r"[^\w\u3400-\u9fff]", "", str(value).casefold()).replace("_", "")


def _words(value):
    text = _normal(value)
    return {text[i:i+2] for i in range(max(0, len(text)-1))}


def _asset_text(item):
    return " ".join([item.get("name", ""), item.get("relative_path", ""), item.get("category", ""),
                     *item.get("semantic_labels", [])])


def _find_named(items, terms):
    for term in terms:
        exact = next((item for item in items if item["id"] == term), None)
        if exact:
            return exact
        normalized = _normal(term)
        exact = next((item for item in items if normalized in
                      (_normal(item["id"]), _normal(item.get("name", "")))), None)
        if exact:
            return exact
    return None


def _pick_motion(label, items, *, expression=False):
    synonyms = EMOTION_SYNONYMS if expression else ACTION_SYNONYMS
    similar = EMOTION_SIMILAR if expression else ACTION_SIMILAR
    exact = next((item for item in items if item["id"] == label), None)
    if exact:
        return exact, "exact", False, "已有素材标识与编排标签一致"
    item = _find_named(items, (label, *synonyms.get(label, ())))
    if item:
        return item, "synonym", False, "已有素材名称或标识与编排语义对应"
    item = _find_named(items, similar.get(label, ()))
    if item:
        return item, "similar", True, "没有对应素材，采用现有相近表现；效果与原编排有差异"
    neutral = "neutral" if expression else "idle"
    item = _find_named(items, (neutral, *synonyms.get(neutral, ())))
    if item:
        return item, "fallback", True, "缺少对应或相近素材，保留现有自然表情" if expression else "缺少对应或相近动作，保留现有站立动作"
    if items:
        # A rig may only have a saved motion with an opaque/custom name. Retain
        # that real animation and disclose it, never invent an idle animation.
        item = sorted(items, key=lambda row: (not row.get("basic"), not row.get("loop"), row["id"]))[0]
        return item, "fallback", True, "连自然表情也不存在，使用已有表情" if expression else "连站立动作也不存在，使用该角色已有动作"
    return None, "missing", False, "素材库没有可用表情" if expression else "该角色没有可用动作"


def _identity_score(name, actor):
    name, asset_name = _normal(name), _normal(actor.get("name", ""))
    if name == asset_name:
        return 200
    labels = [_normal(value) for value in actor.get("semantic_labels", [])]
    if name in labels:
        return 160
    # Single-character names are deliberately not substring matches.
    if len(name) >= 2 and (name in asset_name or asset_name and len(asset_name) >= 2 and asset_name in name):
        return 50
    return 0


def _character_score(character, actor, shots):
    needed = {shot["action"] for shot in shots} | {"idle"}
    quality = {"exact": 12, "synonym": 11, "similar": 5, "fallback": 0, "missing": -30}
    coverage = sum(quality[_pick_motion(label, actor["actions"])[1]] for label in needed)
    context = " ".join(shot.get("direction", "")+" "+shot["text"] for shot in shots)
    semantic = min(20, len(_words(character["name"]+" "+context) & _words(_asset_text(actor))))
    face = 8 if "正面" in _asset_text(actor) else -8 if "背面" in _asset_text(actor) else 0
    return _identity_score(character["name"], actor)+coverage+semantic+face


def _traits(text):
    text = text.casefold()
    groups = {index for index, words in enumerate(PLACE_WORDS) if any(word in text for word in words)}
    indoors, outdoors = any(word in text for word in INDOOR), any(word in text for word in OUTDOOR)
    day, night = any(word in text for word in DAY), any(word in text for word in NIGHT)
    return groups, (1 if indoors and not outdoors else -1 if outdoors and not indoors else 0), (1 if day and not night else -1 if night and not day else 0)


def _background_score(scene, context, item):
    resource = _asset_text(item)
    explicit_scene = scene not in ("默认场景", "场景", "未指定")
    scene_words = _words(scene) if explicit_scene else set()
    direct = len(scene_words & _words(resource))
    context_hits = min(6, len(_words(context) & _words(resource)))
    places, inside, daytime = _traits(scene)
    context_places, context_inside, context_daytime = _traits(context)
    asset_places, asset_inside, asset_daytime = _traits(resource)
    place_matches = len((places or context_places) & asset_places)
    spatial = inside or context_inside
    time = daytime or context_daytime
    score = direct*8+context_hits+place_matches*10
    score += (4 if spatial == asset_inside else -6) if spatial and asset_inside else 0
    score += (4 if time == asset_daytime else -6) if time and asset_daytime else 0
    # A mention in dialogue can guide an unnamed scene. If the user explicitly
    # requests another place, dialogue alone must not be reported as a match.
    matched = direct > 0 or bool(places & asset_places) or (not explicit_scene and (context_hits > 0 or place_matches > 0))
    return score, matched


def _background_mismatch(scene, context, item):
    _, inside, daytime = _traits(scene)
    _, context_inside, context_daytime = _traits(context)
    _, asset_inside, asset_daytime = _traits(_asset_text(item))
    differences = []
    if (inside or context_inside) and asset_inside and (inside or context_inside) != asset_inside:
        differences.append("室内外条件")
    if (daytime or context_daytime) and asset_daytime and (daytime or context_daytime) != asset_daytime:
        differences.append("昼夜条件")
    return differences


def _reuse_named_bindings(config, board, warnings):
    previous = config.get("character_names")
    if previous is not None:
        originals = copy.deepcopy(config)
        for group in ("characters", "actions", "emotions"):
            config[group] = {}
        for character in board["characters"]:
            key, name = character["id"], character["name"]
            matches = [old for old, old_name in previous.items() if old_name == name]
            source = key if previous.get(key) == name else matches[0] if len(matches) == 1 else None
            if source is not None:
                for group in ("characters", "actions", "emotions"):
                    if source in originals[group]:
                        config[group][key] = originals[group][source]
            elif key in originals["characters"]:
                warnings.append(f"角色「{name}」与上次编号 {key} 的人物不同，已重新选角，避免复用旧人物绑定。")
    config["character_names"] = {row["id"]: row["name"] for row in board["characters"]}


def auto_bind_resources(board, catalog, bindings=None):
    """Return {bindings, plan, matches, warnings}, without mutating any input.

    Optional bindings.character_names is a version-1 {character_id: exact_name}
    identity map. It supports stable named roles after IDs change and prevents
    accidental reuse of old numbered IDs. Legacy bindings without it apply to
    this story. Non-empty manual IDs take precedence, including invalid ones.
    matches records kind, subject, requested, asset_id, asset_name, method,
    substituted and reason. Substitutions are real catalogue assets and are
    separately reported in warnings; the storyboard's original intent remains.
    """
    board = validate_storyboard(board)
    if not isinstance(catalog, dict) or catalog.get("version") != 1:
        raise ValueError("请先扫描 AECPP 素材")
    config = clean_bindings(bindings or {})
    config["resource_root"] = catalog["root"]
    matches, warnings = [], []
    _reuse_named_bindings(config, board, warnings)
    actors = sorted((item for item in catalog["actors"] if item.get("valid")), key=lambda row: row["id"])
    backgrounds = sorted(catalog["backgrounds"], key=lambda row: row["id"])
    expressions = sorted(catalog["expressions"], key=lambda row: row["id"])
    actor_lookup = {row["id"]: row for row in actors}
    shots = {row["id"]: [shot for shot in board["shots"] if (shot.get("actor") or shot.get("speaker")) == row["id"]]
             for row in board["characters"]}

    def record(kind, subject, requested, item, method, substituted, reason):
        matches.append({"kind": kind, "subject": subject, "requested": requested,
                        "asset_id": item["id"] if item else "", "asset_name": item.get("name", item["id"]) if item else "",
                        "method": method, "substituted": substituted, "reason": reason})
        if substituted:
            warnings.append(f"{subject} · {requested} → {item.get('name', item['id'])}（{item['id']}）：{reason}。")

    # Reserve explicit choices before automatic casting. Strong name matches
    # cast first so a generic role does not consume a later role's named actor.
    used = {value for value in config["characters"].values() if value in actor_lookup}
    ordered = sorted(board["characters"], key=lambda row: (-max((_identity_score(row["name"], actor) for actor in actors), default=0), -len(row["name"])))
    for character in ordered:
        key, name = character["id"], character["name"]
        choice = config["characters"].get(key, "")
        if choice:
            actor = actor_lookup.get(choice)
            record("character", name, name, actor, "manual", False,
                   "沿用明确选择的角色素材" if actor else f"已选角色素材不存在或没有动作：{choice}；请重新选择")
            if not actor:
                warnings.append(f"角色「{name}」原有绑定不可用：{choice}，已保留并等待修正。")
        else:
            pool = [actor for actor in actors if actor["id"] not in used] or actors
            actor = max(pool, key=lambda row: _character_score(character, row, shots[key]), default=None)
            config["characters"][key] = actor["id"] if actor else ""
            if actor:
                reused = actor["id"] in used
                method = "identity" if _identity_score(name, actor) else "capability"
                record("character", name, name, actor, method, False, "根据人物名称、已标注特征、动作能力和正面优先选择；全片保持同一角色")
                if reused:
                    warnings.append(f"可用角色素材不足，角色「{name}」复用了「{actor['name']}」，可在素材绑定中更换。")
                used.add(actor["id"])
        if not actor:
            continue
        for group, needed, items in (("actions", {shot["action"] for shot in shots[key]} | {"idle"}, actor["actions"]),
                                     ("emotions", {shot["emotion"] for shot in shots[key]} | {"neutral"}, expressions)):
            mapping = config[group].setdefault(key, {})
            lookup = {item["id"]: item for item in items}
            for label in sorted(needed):
                if mapping.get(label):
                    selected = lookup.get(mapping[label])
                    synonyms = ACTION_SYNONYMS if group == "actions" else EMOTION_SYNONYMS
                    substituted = bool(selected and not _find_named([selected], (label, *synonyms.get(label, ()))))
                    reason = ("沿用明确选择的替代素材，实际表现与原编排标签不同" if substituted else "沿用明确选择的已有素材") if selected else f"原绑定 {mapping[label]} 已不可用；请重新选择"
                    record("action" if group == "actions" else "expression", name, label, selected, "manual", substituted, reason)
                    if not selected:
                        warnings.append(f"角色「{name}」的 {label} 原绑定不可用：{mapping[label]}，已保留并等待修正。")
                else:
                    selected, method, substituted, reason = _pick_motion(label, items, expression=group == "emotions")
                    mapping[label] = selected["id"] if selected else ""
                    record("action" if group == "actions" else "expression", name, label, selected, method, substituted, reason)
    for scene in dict.fromkeys(shot["scene"] for shot in board["shots"]):
        context = " ".join(shot["text"]+" "+shot.get("direction", "") for shot in board["shots"] if shot["scene"] == scene)
        choice = config["backgrounds"].get(scene, "")
        if choice:
            item = next((row for row in backgrounds if row["id"] == choice), None)
            mismatches = _background_mismatch(scene, context, item) if item else []
            substituted = bool(item and (mismatches or not _background_score(scene, context, item)[1]))
            reason = ("沿用已选背景作为替代，名称或已标注场景条件与编排不完全对应" if substituted else "沿用明确选择的已有背景") if item else f"原背景 {choice} 已不可用；请重新选择"
            if mismatches:
                reason += "；"+"、".join(mismatches)+"不符"
            record("background", scene, scene, item, "manual", substituted, reason)
            if not item:
                warnings.append(f"场景「{scene}」的原背景不可用：{choice}，已保留并等待修正。")
            continue
        item = max(backgrounds, key=lambda row: _background_score(scene, context, row)[0], default=None)
        config["backgrounds"][scene] = item["id"] if item else ""
        matched = item is not None and _background_score(scene, context, item)[1]
        mismatch = _background_mismatch(scene, context, item) if item else []
        reason = "根据场景名称、上下文、室内外和昼夜标签匹配已有背景" if matched else "没有匹配的背景，暂用素材库已有背景，请按需更换" if item else "素材库没有可用背景"
        if mismatch:
            reason += "；现有背景的"+"、".join(mismatch)+"与编排不符，保留场景内容作为替代"
        record("background", scene, scene, item, "context" if matched else "fallback" if item else "missing", bool(item and (not matched or mismatch)), reason)
    plan = build_binding_plan(board, catalog, config)
    shot_matches, start = [], 0.
    for shot, selected in zip(board["shots"],plan["shots"]):
        actor = selected.get("actor") or {}
        fields = {}
        character_name = next((c["name"] for c in board["characters"] if c["id"]==selected["character_id"]), "")
        for kind, label, item in (("action",shot["action"],selected.get("action")),
                                  ("expression",shot["emotion"],selected.get("expression"))):
            decision = next((m for m in matches if m["kind"]==kind and m["subject"]==character_name and m["requested"]==label),{})
            fields[kind] = {"requested":label, "asset_id":(item or {}).get("id", ""),
                            "method":decision.get("method", "missing"), "substituted":decision.get("substituted",False)}
        shot_matches.append({"shot_id":shot["id"], "character_id":selected["character_id"],
            "actor_asset_id":actor.get("asset_id", ""), "background_asset_id":(selected.get("background") or {}).get("asset_id", ""),
            "start":round(start,3), "duration":shot["duration"], "camera":shot["camera"],
            **fields, "visual_reference":copy.deepcopy(shot.get("visual_reference",{}))})
        start += shot["duration"]
    return {"bindings": plan["config"], "plan": plan, "matches": matches, "shot_matches":shot_matches,
            "warnings": list(dict.fromkeys(warnings))}
