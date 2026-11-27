"""Read existing AECPP assets and bind story directions to actual resources.

Contracts: AECPP's .actor motions/boneContours, ActorExpressionLibrary.cpp,
ActorExpressionAssets.cpp, and ElectronEngine structured-script bindings.
No resource in the selected library is modified or generated here.
"""
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import uuid

from PIL import Image

SETTINGS = "aecpp_resource_bindings.json"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
PRESETS = list(zip(
    "neutral happy laugh surprised angry sad thinking shy cry talk weary smug skeptical confused shocked fearful panicked annoyed disgusted embarrassed pleading excited proud guilty sleepy bored wink cheeky lovestruck jealous relieved hopeful nervous furious rage_cry sarcastic evil_grin determined stunned sweating blissful pout yawn rolling_eyes speechless sick hurt celebratory".split(),
    "自然 开心 大笑 惊讶 生气 难过 思考 害羞 哭泣 说话 疲惫 得意 怀疑 疑惑 震惊 害怕 惊慌 不耐烦 嫌弃 尴尬 可怜巴巴 兴奋 自豪 心虚 困倦 无聊 眨眼 调皮 心动 嫉妒 松一口气 期待 紧张 暴怒 气哭 嘲讽 坏笑 坚定 呆住 汗颜 满足 撅嘴 打哈欠 翻白眼 无语 难受 疼痛 欢呼".split()))


def _text(value, label, limit=2000, *, empty=False):
    if not isinstance(value, str) or len(value) > limit or any(ord(char)<32 for char in value):
        raise ValueError(f"{label}无效")
    value = value.strip()
    if not value and not empty:
        raise ValueError(f"{label}不能为空")
    return value


def _read(path, maximum=32*1024*1024):
    if path.stat().st_size > maximum:
        raise ValueError("素材索引文件过大")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("素材索引不是有效 JSON") from exc


def _signature(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")).hexdigest()


def _hash(path):
    if not path.is_file() or path.stat().st_size > 256*1024*1024:
        raise ValueError("素材文件缺失或超过 256 MiB")
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024*1024):
            digest.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("读取时素材发生变化，请重新扫描")
    return digest.hexdigest()


def _inside(root, path):
    path = Path(path).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("素材路径超出所选资源目录")
    return path


def _relative(root, value):
    value = _text(value, "素材相对路径").replace("\\", "/")
    if ":" in value or PurePosixPath(value).is_absolute() or ".." in PurePosixPath(value).parts:
        raise ValueError("素材相对路径无效")
    return _inside(root, root/value)


def _image(root, path, *, identifier=None, name=None):
    path = _inside(root, path)
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        raise ValueError("不是支持的图片素材")
    digest = _hash(path)
    try:
        with Image.open(path) as image:
            width, height = image.size
            if not 0 < width*height <= 100_000_000:
                raise ValueError("素材图片尺寸过大或无效")
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError("素材图片无法读取") from exc
    relative = path.relative_to(root).as_posix()
    return {"id": identifier or relative, "name": name or path.stem, "path": str(path),
            "relative_path": relative, "sha256": digest, "width": width, "height": height}


def _semantic_labels(*documents):
    """Use authored metadata only; anonymous images are never visually guessed."""
    labels = []
    for document in documents:
        if not isinstance(document, dict):
            continue
        for field in ("tags", "labels", "keywords", "category", "description"):
            values = document.get(field, [])
            values = [values] if isinstance(values, str) else values
            if isinstance(values, list):
                for value in values[:32]:
                    if isinstance(value, str) and value.strip():
                        labels.append(" ".join(value.split())[:256])
    return list(dict.fromkeys(labels))[:64]


def _sidecar_labels(root, path):
    documents = []
    for candidate in (path.with_suffix(".json"), path.parent/"manifest.json"):
        try:
            candidate = _inside(root, candidate)
            if candidate.is_file():
                documents.append(_read(candidate, 256*1024))
        except (OSError, ValueError, TypeError):
            # Optional labels must not make an otherwise usable asset disappear.
            continue
    return _semantic_labels(*documents)


def _actor(root, path):
    path = _inside(root, path)
    raw = _read(path)
    if not isinstance(raw, dict) or raw.get("resourceKind") != "actor" or raw.get("version") != 1:
        raise ValueError("不是受支持的 AECPP actor 文档")
    skeleton = raw.get("skeleton")
    if not isinstance(skeleton, dict) or not isinstance(skeleton.get("bones"), list) or not skeleton["bones"]:
        raise ValueError("角色缺少骨骼")
    motions, contours = raw.get("motions", []), raw.get("boneContours", [])
    if not isinstance(motions, list) or len(motions) > 2000 or not isinstance(contours, list) or not contours:
        raise ValueError("角色动作或部件格式无效")
    actions, ids = [], set()
    for motion in motions:
        if not isinstance(motion, dict):
            raise ValueError("角色动作格式无效")
        key = _text(motion.get("id"), "动作标识", 256)
        if key in ids:
            raise ValueError("角色动作标识重复")
        ids.add(key)
        frames = motion.get("durationFrames", 60)
        if type(frames) is not int or not 1 <= frames <= 100000:
            raise ValueError("角色动作帧数无效")
        actions.append({"id": key, "name": _text(motion.get("name", key), "动作名", 256),
                        "loop": motion.get("loop", False) is True, "basic": motion.get("basic", False) is True,
                        "duration_frames": frames})
    dependencies = []
    for contour in contours:
        if not isinstance(contour, dict):
            raise ValueError("角色部件格式无效")
        image = contour.get("image", {})
        # Match ActorContourResource.cpp precedence, including legacy packs.
        image = image if isinstance(image, dict) else {}
        reference = contour.get("imagePath", image.get("path", image.get("relativePath")))
        if not reference:
            continue
        reference = _text(reference, "角色贴图引用")
        ref = Path(reference)
        if ref.is_absolute():
            selected = _inside(root, ref)
        else:
            selected = _relative(root, reference)
            if not selected.is_file():
                selected = _inside(root, path.parent/reference)
        item = _image(root, selected)
        dependencies.append({"reference": reference, **item})
    relative = path.relative_to(root).as_posix()
    issues = [] if actions else ["文件未保存动作，请在 AECPP 准备动作后另存并重新扫描"]
    return {"id": relative, "name": _text(raw.get("name", path.stem), "角色名", 256),
            "path": str(path), "relative_path": relative, "sha256": _hash(path),
            "actions": actions, "dependencies": dependencies, "valid": bool(actions), "issues": issues,
            "bone_count": len(skeleton["bones"]), "part_count": len(contours),
            "semantic_labels": list(dict.fromkeys(_semantic_labels(raw)+_sidecar_labels(root, path)))}


def _resource_root(path):
    selected = Path(_text(str(path), "资源目录")).expanduser().resolve()
    if not selected.is_dir():
        raise ValueError("请选择已有的 AECPP 素材目录")
    if (selected/"assets"/"actors").is_dir() or (selected/"assets"/"scenes").is_dir():
        return selected/"assets"
    if selected.name in {"actors", "scenes", "accessories"} and selected.parent.name == "assets":
        return selected.parent
    return selected


def scan_resources(path, report=lambda message: None, stop=lambda: False):
    """Read a library root, an AECPP checkout, or existing AECPP resource packs."""
    root = _resource_root(path)
    result = {"version": 1, "root": str(root), "actors": [], "backgrounds": [], "expressions": [], "issues": []}
    count = 0

    def check(message):
        nonlocal count
        if stop():
            raise InterruptedError("素材扫描已停止")
        count += 1
        if count > 20000:
            raise ValueError("素材数量超过单次扫描上限")
        report(message)

    actor_folder = root/"actors"
    for file in sorted(actor_folder.rglob("*.actor")) if actor_folder.is_dir() else []:
        check(f"读取已有角色与动作 · {file}")
        try:
            result["actors"].append(_actor(root, file))
        except (OSError, ValueError, TypeError) as exc:
            result["issues"].append(f"{file.name}：{exc}")
    scenes = root/"scenes"
    for file in sorted(scenes.rglob("*")) if scenes.is_dir() else []:
        if file.is_file() and file.suffix.lower() in IMAGE_SUFFIXES:
            check(f"读取已有背景 · {file}")
            try:
                result["backgrounds"].append({**_image(root, file), "semantic_labels": _sidecar_labels(root, file)})
            except (OSError, ValueError) as exc:
                result["issues"].append(f"{file.name}：{exc}")
    # Built-in IDs and row-major cells come directly from ActorExpressionLibrary.
    expression_root = root.parent/"resources"/"expressions"
    if root.name == "assets" and expression_root.is_dir():
        for sheet in range(4):
            file = expression_root/f"emotions-{sheet+1:02d}.png"
            check(f"读取现有内置表情图集 · {file}")
            try:
                atlas = _image(root.parent, file)
                for cell in range(12):
                    identifier, name = PRESETS[sheet*12+cell]
                    result["expressions"].append({**atlas, "id": identifier, "name": name, "builtin": True, "kind": "builtin",
                        "preset_id": identifier, "atlas_cell": cell, "atlas_columns": 4, "atlas_rows": 3,
                        "variants": [], "speaking_enabled": True})
            except (OSError, ValueError) as exc:
                result["issues"].append(f"{file.name}：{exc}")
    folder = root/"accessories"/"expressions"
    catalog_file = folder/"expression-catalog.json"
    if catalog_file.is_file():
        catalog = _read(catalog_file)
        if not isinstance(catalog, dict) or catalog.get("version") != 1 or not isinstance(catalog.get("assets"), list):
            raise ValueError("表情索引格式无效")
        for row in catalog["assets"]:
            if not isinstance(row, dict) or row.get("hidden") is True or row.get("builtIn") is True:
                continue
            check(f"读取已有表情 · {row.get('name', '')}")
            try:
                identifier = _text(row.get("id"), "表情标识", 256)
                item = _image(root, _relative(folder.resolve(), row.get("path", "")),
                              identifier=identifier, name=_text(row.get("name"), "表情名", 256))
                if row.get("sha256") and row["sha256"] != item["sha256"]:
                    raise ValueError("表情图片与索引内容摘要不符")
                variants = []
                for variant in row.get("variants", []):
                    role = variant.get("role")
                    if role not in {"base", "closed", "small", "open", "round", "wide"}:
                        raise ValueError("表情嘴型版本角色无效")
                    frame = _image(root, _relative(folder.resolve(), variant.get("path", "")))
                    if variant.get("sha256") != frame["sha256"]:
                        raise ValueError("表情嘴型图片与索引内容摘要不符")
                    variants.append({**frame, "role": role})
                    if role == "base":
                        item.update({key: frame[key] for key in ("path", "relative_path", "sha256", "width", "height")})
                result["expressions"].append({**item, "builtin": False, "kind": "custom", "preset_id": "", "variants": variants,
                    "speaking_enabled": row.get("speakingEnabled", True) is True,
                    "category": str(row.get("category", ""))[:256], "semantic_labels": _semantic_labels(row)})
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                result["issues"].append(f"表情 {row.get('name', '')}：{exc}")
    # Pack images are catalogued as backgrounds only when AECPP says background.
    # Role GIFs/Spine bundles are not falsely represented as .actor skeletons.
    pack_catalogs = list((root/"packs").glob("*/catalog.json")) if (root/"packs").is_dir() else []
    if (root/"catalog.json").is_file():
        pack_catalogs.append(root/"catalog.json")
    unsupported = 0
    for catalog_file in pack_catalogs:
        data = _read(catalog_file)
        for row in data.get("entries", []) if isinstance(data, dict) else []:
            if not isinstance(row, dict):
                continue
            if row.get("sourceKind") == "role":
                unsupported += 1
            if row.get("operation") != "background" or row.get("status") not in (None, "ready"):
                continue
            check(f"读取资源包背景 · {row.get('name', '')}")
            try:
                selected = _relative(catalog_file.parent.resolve(), row.get("path", ""))
                result["backgrounds"].append({**_image(root, selected, name=str(row.get("name") or selected.stem)),
                                              "semantic_labels": _semantic_labels(row)})
            except (OSError, ValueError) as exc:
                result["issues"].append(f"资源包背景：{exc}")
    if unsupported:
        result["issues"].append(f"另有 {unsupported} 项 GIF/Spine 等资源包角色，当前原生骨骼组装只绑定 .actor，请选择含 actors 的 AECPP/assets 目录。")
    if not any(row["valid"] for row in result["actors"]):
        result["issues"].append("未找到已保存可用动作的 .actor 角色")
    result["fingerprint"] = _signature(result)
    report(f"已读取 {len(result['actors'])} 个角色、{len(result['backgrounds'])} 个背景、{len(result['expressions'])} 个表情")
    return result


def clean_bindings(value):
    if not isinstance(value, dict) or value.get("version", 1) != 1:
        raise ValueError("素材绑定设置格式无效")
    clean = {"version": 1, "resource_root": _text(value.get("resource_root", ""), "素材目录", empty=True)}
    for group in ("characters", "backgrounds", "actions", "emotions"):
        mapping = value.get(group, {})
        if not isinstance(mapping, dict) or len(mapping) > 5000:
            raise ValueError("素材映射数量或格式无效")
        clean[group] = {}
        for key, item in mapping.items():
            key = _text(key, "绑定标识", 300)
            if group in {"actions", "emotions"}:
                if not isinstance(item, dict) or len(item) > 100:
                    raise ValueError("动作或表情映射格式无效")
                clean[group][key] = {_text(k, "编排标签", 100): _text(v, "素材标识", empty=True) for k, v in item.items()}
            else:
                clean[group][key] = _text(item, "素材标识", empty=True)
    # Optional identity metadata lets the automatic route reuse a named role
    # across stories without confusing the new story's character-1 with the old.
    if "character_names" in value:
        names = value["character_names"]
        if not isinstance(names, dict) or len(names) > 5000:
            raise ValueError("角色姓名映射格式无效")
        clean["character_names"] = {_text(key, "角色编号", 300): _text(name, "角色名", 80)
                                    for key, name in names.items()}
    return clean


def load_bindings(root):
    path = Path(root)/SETTINGS
    return clean_bindings(_read(path, 1024*1024)) if path.is_file() else clean_bindings({})


def save_bindings(root, value):
    from cache_storage import storage_lease
    clean = clean_bindings(value)
    root = Path(root).resolve()
    with storage_lease(root):
        path = root/SETTINGS
        temporary = path.with_name(path.name+"."+uuid.uuid4().hex+".tmp")
        try:
            temporary.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return clean


def _tokens(value):
    value = re.sub(r"[^\w\u3400-\u9fff]", "", value.casefold())
    return {value[i:i+2] for i in range(max(0, len(value)-1))}


def build_binding_plan(board, catalog, bindings=None):
    """Pure-data selection; scanning and export verification happen in workers."""
    from story_pipeline import validate_storyboard
    board = validate_storyboard(board)
    if not isinstance(catalog, dict) or catalog.get("version") != 1:
        raise ValueError("请先扫描 AECPP 素材")
    config = clean_bindings(bindings or {})
    config["resource_root"] = catalog["root"]
    actors = {item["id"]: item for item in catalog["actors"] if item.get("valid")}
    backgrounds = {item["id"]: item for item in catalog["backgrounds"]}
    expressions = {item["id"]: item for item in catalog["expressions"]}
    available = sorted(actors.values(), key=lambda row: (row["name"] != "沙雕正面", row["name"] == "沙雕背面", row["name"]))
    result = {"version": 1, "resource_root": catalog["root"], "catalog_fingerprint": catalog.get("fingerprint", ""),
              "config": config, "actors": [], "scenes": [], "shots": [], "issues": [], "ready": False}
    selected = {}
    for index, character in enumerate(board["characters"]):
        identifier = character["id"]
        choice = config["characters"].get(identifier)
        if choice is None:
            exact = next((item for item in available if item["name"] == character["name"]), None)
            chosen = exact or (available[index % len(available)] if available else None)
            choice = chosen["id"] if chosen else ""
            config["characters"][identifier] = choice
        actor = actors.get(choice)
        if not actor:
            result["issues"].append(f"角色「{character['name']}」尚未绑定可用 .actor 素材")
            continue
        row = {"character_id": identifier, "name": character["name"], "asset_id": actor["id"],
               **{key: actor[key] for key in ("path", "sha256", "dependencies")}, "actions": {}, "expressions": {}}
        action_lookup = {item["id"]: item for item in actor["actions"]}
        character_shots = [shot for shot in board["shots"] if (shot.get("actor") or shot.get("speaker")) == identifier]
        # Other visible characters stand idle, so their actual idle must exist too.
        needed_actions = {shot["action"] for shot in character_shots} | {"idle"}
        needed_emotions = {shot["emotion"] for shot in character_shots} | {"neutral"}
        for group, needed, lookup, destination in (("actions", needed_actions, action_lookup, row["actions"]),
                                                    ("emotions", needed_emotions, expressions, row["expressions"])):
            mapping = config[group].setdefault(identifier, {})
            for label in sorted(needed):
                if label not in mapping:
                    mapping[label] = label if label in lookup else ""
                item = lookup.get(mapping[label])
                if item is None:
                    result["issues"].append(f"角色「{character['name']}」缺少{'动作' if group=='actions' else '表情'}映射：{label}")
                else:
                    destination[label] = item
        selected[identifier] = row
        result["actors"].append(row)
    selected_scenes = {}
    for name in dict.fromkeys(shot["scene"] for shot in board["shots"]):
        choice = config["backgrounds"].get(name)
        if choice is None:
            candidates = []
            words = _tokens(name)
            for item in backgrounds.values():
                hits = len(words & _tokens(item["name"]))
                if hits:
                    candidates.append((hits, item))
            choice = max(candidates, key=lambda row: row[0])[1]["id"] if candidates else ""
            config["backgrounds"][name] = choice
        item = backgrounds.get(choice)
        if item is None:
            result["issues"].append(f"场景「{name}」尚未绑定已有背景")
            continue
        row = {**item, "name": name, "asset_id": item["id"]}
        selected_scenes[name] = row
        result["scenes"].append(row)
    for shot in board["shots"]:
        character_id = shot.get("actor") or shot.get("speaker") or ""
        actor = selected.get(character_id)
        result["shots"].append({"shot_id": shot["id"], "character_id": character_id, "actor": actor,
            "action": actor["actions"].get(shot["action"]) if actor else None,
            "expression": actor["expressions"].get(shot["emotion"]) if actor else None,
            "background": selected_scenes.get(shot["scene"])})
    result["ready"] = not result["issues"]
    return result


def verify_binding_plan(plan, catalog):
    """Reject changed or foreign paths immediately before copying/exporting."""
    if not plan.get("ready") or plan.get("catalog_fingerprint") != catalog.get("fingerprint"):
        raise ValueError("素材绑定未完成或目录已更新，请重新核对")
    if _signature({key: value for key, value in catalog.items() if key != "fingerprint"}) != catalog.get("fingerprint"):
        raise ValueError("素材目录记录已被修改，请重新扫描")
    root = Path(catalog["root"]).resolve()
    permitted = set()
    for actor in catalog["actors"]:
        permitted.add((actor["path"], actor["sha256"]))
        permitted.update((item["path"], item["sha256"]) for item in actor["dependencies"])
    for item in catalog["backgrounds"] + catalog["expressions"]:
        permitted.add((item["path"], item["sha256"]))
        permitted.update((variant["path"], variant["sha256"]) for variant in item.get("variants", []))
    selected = []
    for actor in plan["actors"]:
        selected.append(actor)
        selected.extend(actor["dependencies"])
        for item in actor["expressions"].values():
            selected.append(item)
            selected.extend(item.get("variants", []))
    selected.extend(plan["scenes"])
    checked = set()
    for item in selected:
        key = (item["path"], item["sha256"])
        if key not in permitted:
            raise ValueError("素材绑定包含未扫描的来源")
        if key in checked:
            continue
        checked.add(key)
        path = Path(item["path"]).resolve()
        if not path.is_relative_to(root) and not (root.name == "assets" and path.is_relative_to(root.parent/"resources"/"expressions")):
            raise ValueError("素材来源超出所选资源目录")
        if _hash(path) != item["sha256"]:
            raise ValueError(f"素材已变化，请重新扫描：{path.name}")
    return True
