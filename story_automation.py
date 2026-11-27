"""One-click text to editable AECPP, assembled exclusively from existing assets."""
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid

from aecpp_resources import (clean_bindings, load_bindings, save_bindings,
                             scan_resources, verify_binding_plan, _resource_root)
from aecpp_resource_export import export_resource_project
from story_pipeline import generate_project_storyboard, load_storyboard, reference_description


def _cancelled(stop):
    return bool(stop.is_set() if hasattr(stop, "is_set") else stop()) if stop is not None else False


def _check(stop):
    if _cancelled(stop):
        raise ValueError("已取消一键生成，原有草稿和工程保持不变")


def _choose_resources(root, explicit, config, catalog, warnings):
    if explicit and str(explicit).strip():
        selected = Path(explicit).expanduser().resolve()
        if not selected.is_dir():
            raise ValueError(f"素材目录不存在：{selected}；请在“素材与组装”选择 AECPP 素材目录")
        return _resource_root(selected)
    saved = config.get("resource_root")
    if saved:
        selected = Path(saved).expanduser().resolve()
        if selected.is_dir():
            return _resource_root(selected)
        warnings.append(f"已保存的素材目录不可用，正在查找本机 AECPP：{selected}")
    candidates = [catalog.get("root") if isinstance(catalog, dict) else None,
                  os.environ.get("AECPP_RESOURCE_ROOT"), root.parent/"AECPP", root/"AECPP",
                  Path(__file__).resolve().parent.parent/"AECPP"]
    seen = set()
    for candidate in candidates:
        if not candidate:
            continue
        selected = Path(candidate).expanduser().resolve()
        if selected in seen:
            continue
        seen.add(selected)
        if selected.is_dir():
            normalized = _resource_root(selected)
            if (normalized/"actors").is_dir() or (normalized/"scenes").is_dir():
                return normalized
    raise ValueError("未找到 AECPP 素材目录；请在“素材与组装”选择一次 AECPP 或 assets 目录，以后将自动复用")


def _previous_names(root, config, warnings):
    """Disambiguate legacy per-story numeric IDs before reusing saved mappings."""
    if "character_names" in config or not config.get("characters"):
        return config
    path = root/"creations"/"current_story.json"
    if path.is_file():
        try:
            board = load_storyboard(path)
            config["character_names"] = {row["id"]: row["name"] for row in board["characters"]}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            warnings.append(f"旧分镜身份信息无法读取，已重新自动匹配角色：{exc}")
            for key in ("characters", "actions", "emotions"):
                config[key] = {}
    return config


def _write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def generate_animation(root, text, title="我的沙雕动画", *, source_format="auto",
                       resource_root=None, bindings=None, catalog=None,
                       report=lambda message: None, stop=None,sound=None):
    sound = {'enabled':sound} if isinstance(sound,bool) else {'enabled':False} if sound is None else sound
    if not isinstance(sound,dict) or type(sound.get('enabled',False)) is not bool or type(sound.get('effects',True)) is not bool:
        raise ValueError('自动声音设置无效')
    with tempfile.TemporaryDirectory(prefix='aecpp-speech-') as folder:
        return _generate_animation(root,text,title,source_format=source_format,resource_root=resource_root,
            bindings=bindings,catalog=catalog,report=report,stop=stop,sound=sound,audio_folder=Path(folder))


def _generate_animation(root, text, title, *, source_format, resource_root, bindings, catalog, report, stop, sound, audio_folder):
    """Publish an independent run; failed/cancelled runs never replace a draft.

    Manual mappings are preferences, not overwritten by automatic substitutions.
    Cancellation is accepted until project publication, after which success wins.
    """
    from aecpp_auto_match import auto_bind_resources
    from cache_storage import storage_lease

    root = Path(root).resolve()
    warnings = []
    _check(stop)
    report("正在解析小说 / 剧本的人物、场景、对白与动作…")
    board = generate_project_storyboard(root, text, title, source_format=source_format)
    matched_movies = [shot["visual_reference"]["movie_id"] for shot in board["shots"]
                      if shot.get("visual_reference") and "duration" not in shot["visual_reference"].get("applied_fields",[])]
    if matched_movies:
        from video_visual_learning import prepare_matched_rhythm
        changed, rhythm_warnings=prepare_matched_rhythm(root,matched_movies,report=report,check=lambda:_check(stop))
        warnings.extend(rhythm_warnings)
        if changed:
            board=generate_project_storyboard(root,text,title,source_format=source_format)
    _check(stop)
    description = reference_description(board)
    if description:
        report(description)
    config = clean_bindings(copy.deepcopy(bindings)) if bindings is not None else load_bindings(root)
    config = _previous_names(root, config, warnings)
    selected = _choose_resources(root, resource_root, config, catalog, warnings)
    report(f"正在读取现有素材 · {selected}")
    reusable = isinstance(catalog, dict) and catalog.get("root") == str(selected)
    if not reusable:
        catalog = scan_resources(selected, report=report, stop=lambda: _cancelled(stop))
    else:
        catalog = copy.deepcopy(catalog)
    _check(stop)
    report("正在自动匹配角色、动作、表情和场景…")
    matched = auto_bind_resources(board, catalog, config)
    # Reuse in-memory scans only if their selected files still match. Re-scan once
    # on changes; no stale file or invented ID may enter the published project.
    if reusable:
        try:
            if not matched["plan"]["ready"]:
                raise ValueError("已有扫描未能满足本次分镜")
            verify_binding_plan(matched["plan"], catalog)
        except (OSError, ValueError, KeyError, TypeError):
            report("素材已变化或映射不完整，正在重新扫描并匹配…")
            catalog = scan_resources(selected, report=report, stop=lambda: _cancelled(stop))
            matched = auto_bind_resources(board, catalog, config)
    _check(stop)
    plan = matched["plan"]
    if not plan["ready"]:
        raise ValueError("素材尚不能完成组装，请在“素材与组装”核对：" + "；".join(plan["issues"])[:1800])
    audio_bundle=None
    if sound.get('enabled'):
        from story_audio import prepare_story_audio
        board,audio_bundle=prepare_story_audio(board,audio_folder,effects=sound.get('effects',True),
                                              report=report,check=lambda:_check(stop))
        matched=auto_bind_resources(board,catalog,config)
        plan=matched['plan']
    verify_binding_plan(plan, catalog)
    warnings.extend(matched["warnings"])
    warnings.extend(catalog.get("issues", []))
    if not board["characters"]:
        warnings.append("本段未识别到明确人物，已生成背景和旁白字幕；可在分镜中补充人物后重新组装")
    for summary_key in ("reference_summary", "visual_reference_summary"):
        if board.get(summary_key, {}).get("warning"):
            warnings.append(board[summary_key]["warning"])
    warnings = list(dict.fromkeys(warnings))
    for row in matched["matches"]:
        report(f"匹配 {row['kind']} · {row['subject']} · {row['requested']} → {row['asset_name']} · {row['reason']}")
    _check(stop)
    # Remember a verified directory even if a later copy is cancelled: this
    # avoids asking for it again on retry. Human mappings remain unchanged.
    # Resolve preference-save warnings before writing the per-run report.
    config["resource_root"] = catalog["root"]
    try:
        save_bindings(root, config)
    except (OSError, ValueError) as exc:
        warning = f"素材目录偏好未能保存，本次仍可生成：{exc}"
        warnings.append(warning)
        report(warning)
    _check(stop)
    # The generated basename is bounded and never includes user path components.
    safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(board["title"]))[:42].strip(" .") or "动画"
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S")
    animations = root/"creations"/"animations"
    run_dir = animations/f"{safe_title}-{stamp}-{uuid.uuid4().hex[:10]}"
    output = run_dir/"story.aecpp"
    report_path = run_dir/"matching-report.json"
    with storage_lease(root):
        # Reject symlink/junction output redirection before making or removing a run.
        if not animations.resolve().is_relative_to(root):
            raise ValueError("生成目录指向工程之外，请检查 creations/animations")
        animations.mkdir(parents=True, exist_ok=True)
        run_dir.mkdir()
        try:
            _write_json(run_dir/"storyboard.json", board)
            _write_json(run_dir/"bindings.json", matched["bindings"])
            _write_json(report_path, {"version": 1, "kind": "existing-resource-assembly",
                "output": str(output), "resource_root": catalog["root"],
                "catalog_fingerprint": catalog["fingerprint"], "source_hash": board.get("source_hash", ""),
                "matches": matched["matches"], "shot_matches":matched["shot_matches"],
                "visual_reference_summary":board.get("visual_reference_summary",{}),
                "reference_summary":board.get("reference_summary",{}), "warnings": warnings})
            if audio_bundle:
                report_data=json.loads(report_path.read_text(encoding='utf-8'))
                report_data['sound']=audio_bundle['summary']
                _write_json(report_path,report_data)
            _check(stop)
            output = export_resource_project(board, output, catalog=catalog,
                bindings=matched["bindings"], report=report, stop=lambda: _cancelled(stop),audio_bundle=audio_bundle)
        except BaseException:
            resolved = run_dir.resolve()
            if resolved.parent == animations.resolve() and resolved.is_relative_to(root):
                shutil.rmtree(resolved)
            raise
    report(f"一键生成完成 · {len(board['shots'])} 个镜头 · {output}")
    return {"board": board, "catalog": catalog, "bindings": matched["bindings"], "plan": plan,
            "output": output, "run_dir": run_dir, "warnings": warnings,
            "sound":audio_bundle['summary'] if audio_bundle else {},
            "matches": matched["matches"], "shot_matches":matched["shot_matches"], "report_path": report_path}
