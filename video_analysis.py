"""Built-in transcription, grounded content analysis and editable references."""
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

import dataset
from media import open_movie
from movie_sequence_data import snapshot_sources, _check_source


DEFAULTS = {"enabled":True,"asr_enabled":True,"asr_model":"tiny","asr_model_path":"",
            "language":"zh","semantic_enabled":False,"provider":"ollama",
            "base_url":"http://127.0.0.1:11434","model":"","api_key_env":"VIDEO_ANALYSIS_API_KEY",
            "keyframes":True,"visual_enabled":False,"visual_model":"","visual_max_clips":12,
            "analysis_cache":True,"ollama_keep_alive":300}
SETTINGS = "video_analysis_settings.json"


def clean_settings(value):
    if not isinstance(value,dict):
        raise ValueError("视频理解设置格式无效")
    result = {**DEFAULTS,**{key:item for key,item in value.items() if key in DEFAULTS}}
    for key in ("enabled","asr_enabled","semantic_enabled","keyframes","visual_enabled","analysis_cache"):
        if type(result[key]) is not bool:
            raise ValueError("请选择有效的视频理解选项")
    for key in ("asr_model_path","language","base_url","model","visual_model","api_key_env"):
        if not isinstance(result[key],str) or len(result[key])>2000 or any(ord(c)<32 for c in result[key]):
            raise ValueError("视频理解设置包含无效文本")
        result[key] = result[key].strip()
    if type(result["visual_max_clips"]) is not int or not 1<=result["visual_max_clips"]<=60:
        raise ValueError("画面分析片段数须为 1–60 的整数")
    if type(result['ollama_keep_alive']) is not int or not 0<=result['ollama_keep_alive']<=3600:
        raise ValueError('Ollama 保留时间须为 0–3600 秒')
    if result["asr_model"] not in {"tiny","base","small"}:
        raise ValueError("请选择 tiny、base 或 small 语音模型")
    if result["provider"] not in {"ollama","openai-compatible"}:
        raise ValueError("请选择本地 Ollama 或兼容接口")
    if result["api_key_env"] and (not result["api_key_env"].isascii() or not result["api_key_env"].replace("_", "a").isalnum()):
        raise ValueError("API Key 环境变量名无效")
    # Credentials belong to this process (or an environment variable), never a project archive.
    from urllib.parse import urlsplit
    address = urlsplit(result["base_url"])
    if (address.scheme not in {"http","https"} or not address.hostname or address.username or address.password
            or address.query or address.fragment):
        raise ValueError("模型服务地址须为不含密码、查询参数的 HTTP/HTTPS 地址")
    if address.scheme=="http" and address.hostname not in {"localhost","127.0.0.1","::1"}:
        raise ValueError("远程模型服务请使用 HTTPS；本地服务可使用 HTTP")
    return result


def load_settings(root):
    path = Path(root)/SETTINGS
    if not path.exists():
        return dict(DEFAULTS)
    if path.stat().st_size>16384:
        raise ValueError("视频理解设置文件过大")
    return clean_settings(json.loads(path.read_text(encoding="utf-8")))


def write_json(path,value):
    path = Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary = path.with_name(uuid.uuid4().hex+".tmp")
    try:
        temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def save_settings(root,value):
    from cache_storage import storage_lease
    value = clean_settings(value)
    with storage_lease(root):
        write_json(Path(root)/SETTINGS,value)
    return value


def model_path(settings):
    return Path(settings["asr_model_path"]).expanduser().resolve() if settings["asr_model_path"] else Path(__file__).resolve().parent/"weights"/"asr"/settings["asr_model"]


def _signature(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode("utf-8")).hexdigest()


def commit_when_idle(action,check,report):
    """A concurrent download must not turn successful model inference into failure."""
    from cache_storage import StorageBusy
    deadline,notified = time.monotonic()+600,False
    while True:
        check()
        try:
            return action()
        except StorageBusy:
            if time.monotonic()>=deadline:
                raise ValueError("等待工程写入超过 10 分钟，请完成下载或缓存整理后重试学习")
            if not notified:
                report("waiting-storage","分析结果已生成，正在等待下载或缓存整理释放写入锁；可暂停本次学习")
                notified = True
            time.sleep(.2)


def analyse_video(root,movie_id,report=lambda phase,message:None,check=lambda:None,*,api_key_provider=lambda:""):
    """Run in the existing cancellable auto-learning worker; no separate service."""
    from auto_learning import analyse_movie, _timed_text
    root = Path(root).resolve()
    started=time.monotonic();performance={}
    settings = load_settings(root)
    if not settings["enabled"]:
        return analyse_movie(root,movie_id,report,check)
    check()
    snapshot = snapshot_sources(root,[movie_id])
    movie = snapshot["movies"][0]
    source = _check_source(movie)
    with open_movie(source,lambda message:check()) as video:
        duration = round(video.duration*1000)
    cues = _timed_text(movie,duration)
    stages, issues = [], []
    asr_config_hash = _signature({"path":str(model_path(settings)),"model":settings["asr_model"],"language":settings["language"]})
    asr_record = (root/"video_analysis"/"transcripts"/f"{movie_id}-latest.json").resolve()
    if not asr_record.is_relative_to(root):
        raise ValueError("转写目录不在当前工程内")
    previous_asr = {}
    if asr_record.is_file() and asr_record.stat().st_size<=16384:
        try:
            previous_asr = json.loads(asr_record.read_text(encoding="utf-8"))
            if not isinstance(previous_asr,dict):
                previous_asr = {}
        except (ValueError,OSError):
            previous_asr = {}
    other_movie = {**movie,"subtitles":[row for row in movie["subtitles"] if row.get("source")!="local-asr"]}
    has_other_text = bool(_timed_text(other_movie,duration))
    retranslate = (bool(cues) and not has_other_text and any(row.get("source")=="local-asr" for row in movie["subtitles"])
                   and previous_asr.get("settings_hash")!=asr_config_hash)

    rhythm = None
    def relay(event):
        check()
        message = str(event.get("message",event.get("phase","正在处理")))
        if event.get("url"):
            message += " · "+str(event["url"])
        if event.get('frames') and callable(getattr(report,'visual',None)):
            from learning_telemetry import visual_event
            details=visual_event(root,event,rhythm)
            report.visual(str(event.get('phase','visual-understanding')),message,details)
        else:
            report(str(event.get("phase","analysing")),message)

    def stopped():
        check()
        return False

    cache_dir=None;source_digest=''
    if settings['analysis_cache'] and settings['provider']=='ollama':
        from video_analysis_cache import file_hash
        cache_dir=(root/'video_analysis/inference_cache').resolve()
        if not cache_dir.is_relative_to(root):raise ValueError('推理缓存目录不在当前工程内')
        report('analysis-cache','正在核对原片摘要，以安全复用已完成的分析片段…')
        source_digest=file_hash(source,stopped)
        if source_digest!=movie['digest']:raise ValueError('原片摘要已变化，请重新导入核对')

    if cues and not retranslate:
        report("transcript-ready",f"复用已有 {len(cues)} 条带时间码字幕或剧本 · {source}")
        stages.append({"stage":"transcription","status":"reused","segments":len(cues)})
    if (not cues or retranslate) and settings["asr_enabled"]:
        try:
            from video_transcription import transcribe_movie
            report("transcribing",f"开始本地语音转写 · {settings['asr_model']} · CPU int8 · {model_path(settings)}")
            transcript = transcribe_movie(root,movie_id,model_path=model_path(settings),language=settings["language"] or None,
                                          report=relay,stop=stopped)
            check()
            _check_source(movie)
            # Reuse the strict timed-text validator; this is our own ASR output,
            # not an analysis summary or a fabricated transcript.
            from video_helper_import import _read_transcript
            directory = (root/"video_analysis"/"transcripts").resolve()
            if not directory.is_relative_to(root):
                raise ValueError("转写目录不在当前工程内")
            path = directory/f"{movie_id}-{_signature(transcript)[:24]}.json"
            write_json(path,transcript)
            if transcript.get("segments"):
                raw,validated,_,_ = _read_transcript(path)
                import subtitles
                from cache_storage import storage_lease
                def store_transcript():
                    with storage_lease(root):
                        check()
                        _check_source(movie)
                        subtitles.store_track(root,movie_id,"local-asr",f"本地语音转写 · {settings['asr_model']}",
                                              hashlib.sha256(raw).hexdigest(),validated)
                        write_json(asr_record,{"settings_hash":asr_config_hash,"source_digest":movie["digest"],
                                               "path":path.relative_to(root).as_posix()})
                commit_when_idle(store_transcript,check,report)
                stages.append({"stage":"transcription","status":"completed","segments":len(validated),
                               "path":path.relative_to(root).as_posix(),"model":settings["asr_model"]})
            else:
                issues.append("语音模型未识别到有效台词，可补充字幕或更换语音模型")
        except Exception as exc:
            check()
            issues.append("语音转写未完成："+str(exc))
            report("transcription-unavailable",issues[-1])

    analysis = analyse_movie(root,movie_id,report,check,cache_dir=cache_dir,source_hash=source_digest)
    performance['scan_cache_hit']=analysis.get('scan_cache_hit',False)
    observations = analysis["observations"]
    semantic_status, note_path, frame_count = "disabled", "", 0
    if settings["semantic_enabled"]:
        semantic_status = "needs_input"
        if not observations:
            issues.append("内容分析需要可用的字幕或语音转写")
        elif not settings["model"]:
            semantic_status = "needs_setup"
            issues.append("请在视频理解设置中选择内容分析模型")
        else:
            try:
                from video_semantics import analyse_transcript, extract_keyframes
                from video_analysis_notes import save_reference, to_storyboard
                transcript = {"version":1,"unit":"ms","provider":"indexed-timed-text","durationMs":analysis["duration_ms"],
                              "segments":[{"startMs":row["start_ms"],"endMs":row["end_ms"],"text":row["text"]} for row in observations]}
                api_key = api_key_provider() or (os.environ.get(settings["api_key_env"],"") if settings["api_key_env"] else "")
                report("understanding",f"正在分析剧情、冲突、笑点与镜头建议 · {settings['model']} · {settings['base_url']}")
                result = analyse_transcript(transcript,model=settings["model"],provider=settings["provider"],
                                            base_url=settings["base_url"],api_key=api_key,report=relay,stop=stopped,
                                            cache_dir=cache_dir,source_hash=source_digest,keep_alive=settings['ollama_keep_alive'])
                performance['text']=result.get('analysis',{}).get('performance',{})
                check()
                _check_source(movie)
                if settings["keyframes"]:
                    directory = (root/"video_analysis"/"keyframes"/f"{movie_id}-{_signature(result)[:16]}").resolve()
                    if not directory.is_relative_to(root):
                        raise ValueError("关键帧目录不在当前工程内")
                    frames = extract_keyframes(source,result["contentBlocks"],directory,report=relay,stop=stopped)
                    frame_count = len(frames)
                    for frame in frames:
                        path = Path(frame["path"]).resolve()
                        if not path.is_relative_to(root):
                            raise ValueError("关键帧不在当前工程内")
                        for block in result["contentBlocks"]:
                            if block["blockId"]==frame["blockId"] and block["highlights"]:
                                highlight = next((row for row in block["highlights"] if row["highlightId"]==frame.get("highlightId")),block["highlights"][0])
                                highlight.setdefault("keyframes",[]).append({"path":path.relative_to(root).as_posix(),
                                    "timeMs":frame["timeMs"],"caption":frame.get("caption","")})
                check()
                _check_source(movie)
                def store_note():
                    check()
                    _check_source(movie)
                    return save_reference(root,movie_id,result,source={"kind":"built-in-video-analysis","model":settings["model"],
                        "provider":settings["provider"],"url":settings["base_url"],"film_hash":movie["digest"],
                        "transcript_hash":_signature(transcript)},report=lambda message:report("saving-analysis",message))
                note = commit_when_idle(store_note,check,report)
                note_path = note["path"]
                semantic_status = "completed"
                stages.append({"stage":"understanding","status":"completed","model":settings["model"],"path":note_path})
                try:
                    # A long film can exceed the editor's per-story size limit.
                    # Each bounded content block remains independently editable.
                    for block in result["contentBlocks"]:
                        check()
                        analysis["boards"].append(to_storyboard(note,block_ids=[block["blockId"]]))
                except Exception as exc:
                    check()
                    issues.append("内容笔记已保存，部分摘要分镜需手动选择内容段生成："+str(exc))
            except Exception as exc:
                check()
                semantic_status = "failed"
                issues.append("内容分析未完成："+str(exc))
                report("understanding-failed",issues[-1])
    visual_status, visual_path, visual_count, visual_frames = "disabled", "", 0, 0
    if settings["visual_enabled"]:
        visual_status = "needs_setup"
        if not settings["visual_model"]:
            issues.append("请在视频理解设置中选择视觉模型，或点击一键准备视觉模型")
        else:
            try:
                from video_visual import analyse_visual
                from video_visual_learning import save_visual_reference
                from video_rhythm import analyse_rhythm
                rhythm = None
                try:
                    rhythm = analyse_rhythm(source,duration_ms=analysis["duration_ms"],report=report,check=check,
                        cache_dir=cache_dir,source_hash=source_digest)
                    performance['rhythm_cache_hit']=rhythm.pop('cache_hit',False)
                except Exception as exc:
                    check()
                    report("shot-rhythm-unavailable","全片切镜节奏暂不可用，保留局部参考："+str(exc))
                # Unique output directory keeps old references intact until the
                # entire new batch has been validated and atomically published.
                directory = (root/"video_analysis"/"visual_frames"/f"{movie_id}-{uuid.uuid4().hex}").resolve()
                if not directory.is_relative_to(root):
                    raise ValueError("画面采样目录不在当前工程内")
                api_key = api_key_provider() or (os.environ.get(settings["api_key_env"],"") if settings["api_key_env"] else "")
                report("visual-understanding",f"正在学习连续画面的人物动作、表情与景别 · {settings['visual_model']} · {source}")
                result = analyse_visual(source,duration_ms=analysis["duration_ms"],observations=observations,
                    cut_candidates_ms=[t for t in analysis["cut_candidates_ms"] if 0<t<analysis["duration_ms"]],output_dir=directory,
                    model=settings["visual_model"],provider=settings["provider"],base_url=settings["base_url"],
                    api_key=api_key,max_clips=settings["visual_max_clips"],report=relay,stop=stopped,
                    cache_dir=cache_dir,keep_alive=settings['ollama_keep_alive'])
                performance['visual']=result.get('performance',{})
                check()
                _check_source(movie)
                result.update(movie_id=movie_id,film_hash=movie["digest"])
                if rhythm is not None:
                    result["rhythm"] = rhythm
                for clip in result["clips"]:
                    for frame in clip["frames"]:
                        path = Path(frame["path"]).resolve()
                        if not path.is_relative_to(root):
                            raise ValueError("视觉证据帧不在当前工程内")
                        frame["path"] = path.relative_to(root).as_posix()
                def store_visual():
                    check()
                    _check_source(movie)
                    return save_visual_reference(root,result)
                note = commit_when_idle(store_visual,check,report)
                visual_path, visual_count = note["path"], len(note["clips"])
                visual_frames = sum(len(clip["frames"]) for clip in note["clips"])
                visual_status = "completed"
                stages.append({"stage":"visual-understanding","status":"completed","model":settings["visual_model"],
                               "path":visual_path,"clips":visual_count,"frames":visual_frames})
                report("visual-ready",f"画面学习已保存 · {visual_count} 个片段 / {visual_frames} 张真实帧；合格参考将用于新故事分镜")
            except Exception as exc:
                check()
                from video_semantics import ModelSetupRequired
                visual_status = "needs_setup" if isinstance(exc,ModelSetupRequired) else "failed"
                issues.append("画面理解未完成："+str(exc))
                report("visual-unavailable",issues[-1])
    check()
    _check_source(movie)
    analysis["model_analysis"] = {"semantic_status":semantic_status,"reference_path":note_path,"frame_count":frame_count,
                                  "visual_status":visual_status,"visual_reference_path":visual_path,
                                  "visual_clip_count":visual_count,"visual_frame_count":visual_frames,
                                  "stages":stages,"issues":issues,"performance":{'wall_seconds':round(time.monotonic()-started,3),**performance},
                                  "settings":{key:value for key,value in settings.items() if key!="api_key_env"}}
    return analysis
