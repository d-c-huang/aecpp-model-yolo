"""Assemble existing AECPP actors, motions, expressions and backgrounds.

No image generation or frame-video synthesis is involved. Character documents
and image dependencies are copied into an adjacent, portable material folder.
Native actor/action/expression fields follow AECPP ProjectStorage.cpp.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

from aecpp_export import _validate, _transform, _vec, _text, _color


def _read_verified(item):
    path = Path(item["path"]).resolve()
    if not path.is_file() or path.stat().st_size > 256*1024*1024:
        raise ValueError("素材文件缺失或超过 256 MiB，请重新扫描")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != item["sha256"]:
        raise ValueError(f"素材已变化，请重新扫描：{path.name}")
    return raw


def _write_new(target, raw):
    if target.exists() or target.is_symlink():
        raise ValueError("输出已存在，请选择新的工程名称")
    fd, temporary = tempfile.mkstemp(prefix=".aecpp-assembly-",suffix=".tmp",dir=target.parent)
    try:
        with os.fdopen(fd,"wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        (os.rename if os.name=="nt" else os.link)(temporary,target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _materialize(plan, folder, prefix, report):
    actors, images = {}, {}
    used_bytes = 0
    def image(item, directory=None):
        nonlocal used_bytes
        key = (item["sha256"],str(directory))
        if key in images:
            return images[key]
        report(f"复制素材 · {item['path']}")
        raw = _read_verified(item)
        used_bytes += len(raw)
        if used_bytes > 1024*1024*1024:
            raise ValueError("本次素材包超过 1 GiB，请按章节拆分")
        suffix = Path(item["path"]).suffix.lower()
        # WIC in the native Windows renderer can reject a 260-character path
        # even when Python copied it successfully. Identity/dedup still use the
        # complete SHA256; short package-local names keep deep projects usable.
        relative = (directory or Path("images"))/f"{len(images)+1:05d}{suffix}"
        final_path=folder.parent/prefix/relative
        if os.name=="nt" and len(str(final_path).encode("utf-16-le"))//2 >= 250:
            raise ValueError("素材包路径过长，AECPP 可能无法读取；请将学习工程放到更短的目录后重新生成")
        path = folder/relative
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(raw)
        images[key] = relative.as_posix()
        return images[key]
    for index, actor in enumerate(plan["actors"]):
        report(f"复制现有角色与绑定部件 · {index+1}/{len(plan['actors'])} · {actor['name']}")
        raw = _read_verified(actor)
        document = json.loads(raw)
        directory = Path("actors")/f"{index+1:03d}"
        references = {}
        for dependency in actor["dependencies"]:
            relative = image(dependency,directory/"images")
            references[dependency["reference"]] = Path(relative).relative_to(directory).as_posix()
        # AECPP accepts character-local image references, independent of the
        # selected application's global asset root. Preserve bones and motions.
        for contour in document.get("boneContours",[]):
            reference = (contour.get("imagePath") or contour.get("image",{}).get("path")
                         or contour.get("image",{}).get("relativePath"))
            if reference:
                if reference not in references:
                    raise ValueError("角色部件依赖未完成绑定，请重新扫描")
                contour["imagePath"] = references[reference]
                if isinstance(contour.get("image"),dict):
                    contour["image"]["path"] = references[reference]
        target = folder/directory/"character.actor"
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(json.dumps(document,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
        actors[actor["character_id"]] = f"{prefix}/{directory.as_posix()}/character.actor"
    backgrounds = {}
    for background in plan["scenes"]:
        backgrounds[background["name"]] = prefix+"/"+image(background)
    expressions = {}
    for actor in plan["actors"]:
        for expression in actor["expressions"].values():
            # Built-in IDs use the actual AECPP atlas embedded in its engine.
            # Only custom images become project-relative external resources.
            if expression.get("kind") not in {"builtin","preset"} and expression.get("path"):
                expressions[expression["id"]] = {"path":prefix+"/"+image(expression),
                    "variants":[{"role":variant["role"],"imagePath":prefix+"/"+image(variant)}
                                for variant in expression.get("variants",[])
                                if variant["role"] in {"closed","small","open","round","wide"}]}
    return actors, backgrounds, expressions


def build_resource_project(board, plan, *, actor_paths, background_paths, expression_paths, audio_bundle=None, audio_paths=None):
    """Construct editable native layers and clips using only validated bindings."""
    names, shots, duration = _validate(board)
    if not plan.get("ready"):
        raise ValueError("素材尚未齐全："+"；".join(str(value) for value in plan.get("issues",[]))[:1800])
    layers, tracks, subtitles, regions = [], [], [], []
    def layer(name, kind, size, **extra):
        item = {"id":f"resource_layer_{len(layers)+1}","name":name,"contentKind":kind,
                "visible":True,"locked":False,"zOrder":len(layers),"transform":_transform(opacity=0),
                "shape":{"kind":0,"size":_vec(*size),"color":_color("FFFFFF")},**extra}
        layers.append(item)
        track = {"targetLayerId":item["id"],"enabled":True,"keyframes":[]}
        tracks.append(track)
        return item,track
    backgrounds = {row["name"]:layer("场景 / "+row["name"],1,(20,11.25),
        imagePath=background_paths[row["name"]],imagePixelSize=_vec(row["width"],row["height"])) for row in plan["scenes"]}
    actors = {row["character_id"]:layer("角色 / "+row["name"],3,(4,6),actor={
        "resourcePath":actor_paths[row["character_id"]],"actions":[],"expressions":[],"mouths":[]}) for row in plan["actors"]}
    serial = 0
    def key(track,at,transform,hold=True):
        nonlocal serial
        serial+=1
        track["keyframes"].append({"id":f"resource_key_{serial}","time":round(at,5),
                                  "interpolation":0 if hold else 1,"transform":transform})
    start, previous_scene, previous_subject = 0., None, ""
    actor_bindings = {row["character_id"]:row for row in plan["actors"]}
    for index,(shot,selected) in enumerate(zip(shots,plan["shots"])):
        end=start+shot["duration"]
        for name,(_,track) in backgrounds.items():
            key(track,start,_transform(opacity=float(name==shot["scene"])))
        active=selected["character_id"]
        cast = [active] if active else []
        if shot["camera"]!="close" and previous_scene==shot["scene"] and previous_subject and previous_subject not in cast:
            cast.append(previous_subject)
        cast.sort(key=lambda identifier:list(names).index(identifier))
        for character_id,(item,track) in actors.items():
            visible=character_id in cast
            primary=character_id==active
            scale={"wide":.8,"medium":1.,"close":1.3}[shot["camera"]]
            x=0 if len(cast)<2 or not visible else (-3 if cast.index(character_id)==0 else 3)
            moving=primary and shot["action"] in {"walk","run"}
            transform=_transform(x,-.4,sx=scale,sy=scale,opacity=float(visible))
            key(track,start,transform,not moving)
            if not visible:
                continue
            action=selected["action"] if primary else actor_bindings[character_id]["actions"]["idle"]
            item["actor"]["actions"].append({"id":f"action_{item['id']}_{index+1}","actionId":action["id"],
                "name":action.get("name",action["id"]),"start":start,"duration":shot["duration"],
                "offset":0,"speed":1,"transition":0,"loop":action.get("loop",True),"frozen":False,
                "talkIntensity":1,"talkFrequency":1})
            expression=selected["expression"] if primary else actor_bindings[character_id]["expressions"]["neutral"]
            expression_images=expression_paths.get(expression["id"],{})
            image=expression_images.get("path","")
            item["actor"]["expressions"].append({"id":f"expression_{item['id']}_{index+1}",
                "expressionId":expression["id"] if not image else "custom:"+expression.get("name",expression["id"]),
                "expressionAssetId":expression["id"] if image else "","imagePath":image,
                "name":expression.get("name",expression["id"]),"start":start,"duration":shot["duration"],
                "offset":0,"speed":1,"loop":True,"scale":1,"offsetX":0,"offsetY":0,"opacity":1,
                "rotation":0,"speakingEnabled":character_id==shot["speaker"] and expression.get("speaking_enabled",True),
                "speakingVariants":expression_images.get("variants",[]),"keyframes":[]})
            if moving:
                key(track,end-.0001,_transform(x+1.5,-.4,sx=scale,sy=scale),True)
        speaker=names.get(shot["speaker"],"")
        style=_text((speaker+"：" if speaker else "")+shot["text"],48,"FFFFFF")
        style.update(outlineColor=_color("242633"),outlineSize=3)
        subtitles.append({"id":f"subtitle_{index+1}","locked":False,"start":start,"end":round(end-.0001,5),"style":style})
        regions.append({"id":f"shot_{index+1}","name":f"{index+1:02d} · {shot['scene']}","start":start,"end":end})
        start,previous_scene,previous_subject=(end,shot["scene"],
            active or (previous_subject if previous_scene==shot["scene"] else ""))
    for _,track in actors.values():
        key(track,duration,_transform(opacity=0))
    audio_tracks=[]
    if audio_bundle:
        for track in audio_bundle['tracks']:
            audio_tracks.append({**{k:v for k,v in track.items() if k not in {'character_id','path','sha256'}},
                'path':audio_paths[track['id']], 'sourceSignature':track['sha256'],
                'actorLayerId':actors[track['character_id']][0]['id'] if track['character_id'] in actors else ''})
        mouth_groups={}
        for voice in audio_bundle['voices']:
            if voice['character_id'] in actors:
                mouth_groups.setdefault((voice['character_id'],voice['audio_track_id']),[]).append(voice)
        for (character_id,track_id),voices in mouth_groups.items():
            # AECPP links a mouth clip to the complete audio range when audio
            # is moved/trimmed. One source-time envelope per role preserves
            # pauses and all lines through those native edits.
            track=next(t for t in audio_bundle['tracks'] if t['id']==track_id)
            keys={0:{'time':0,'openness':0,'shape':'natural'}}
            for voice in voices:
                before=max(0,round(voice['source_offset']-.04,5))
                keys[before]={'time':before,'openness':0,'shape':'natural'}
                keys.update({k['time']:k for k in voice['keys']})
            keys[track['sourceDuration']]={'time':track['sourceDuration'],'openness':0,'shape':'natural'}
            actor=actors[character_id][0]['actor']
            actor['mouths'].append({'id':'mouth_'+track_id+'_'+character_id,'shape':'natural','start':track['offset'],
                'duration':track['duration'],'offset':0,'speed':1,'loop':False,'openness':0,
                'audioPath':audio_paths[track_id],'audioTrackId':track_id,
                'centerX':.5,'centerY':.74,'width':.24,'height':.12,'keepExpressionMouth':False,
                'keyframes':[keys[t] for t in sorted(keys)]})
        for item,_ in actors.values():
            for expression in item['actor']['expressions']:expression['speakingEnabled']=False
    title=str(board.get("title") or "素材组装动画")
    return {"format":"aecpp-project","version":6,"scene":{"name":title,
        "sceneBounds":{"left":-10,"right":10,"bottom":-5.625,"top":5.625},
        "selectedLayerId":layers[0]["id"] if layers else "","nextLayerId":len(layers)+1,
        "selectedAudioId":audio_tracks[0]['id'] if audio_tracks else '',"audioTracks":audio_tracks,"camera":{"transform":_transform(),"locked":False,"enabled":True,"keyframes":[]},
        "timelineRegions":regions,"timelineGroups":[],"subtitles":subtitles,"transitions":[],"weather":[],"layers":layers,
        "animation":{"name":title,"duration":round(duration,5),"fps":24,"tracks":tracks}}}


def export_resource_project(board,target,*,catalog,bindings=None,report=lambda _:None,stop=lambda:False,audio_bundle=None):
    from aecpp_resources import build_binding_plan, verify_binding_plan
    def checkpoint(message=None):
        if stop():
            raise ValueError("已取消素材组装")
        if message is not None:
            report(message)
    checkpoint()
    board=copy.deepcopy(board)
    plan=build_binding_plan(board,catalog,bindings)
    if not plan["ready"]:
        raise ValueError("请先补齐素材映射："+"；".join(str(row) for row in plan["issues"])[:1800])
    verify_binding_plan(plan,catalog)
    target=Path(target).resolve()
    if target.suffix.lower()!=".aecpp":
        raise ValueError("请选择 .aecpp 工程文件")
    assets=target.with_name(target.stem+".assets")
    if any(path.exists() or path.is_symlink() for path in (target,assets)):
        raise ValueError("工程或同名素材目录已存在，请使用新名称")
    target.parent.mkdir(parents=True,exist_ok=True)
    temporary=Path(tempfile.mkdtemp(prefix=".aecpp-materials-",dir=target.parent)).resolve()
    published=False
    try:
        actor_paths,background_paths,expression_paths=_materialize(plan,temporary,assets.name,checkpoint)
        audio_paths={}
        if audio_bundle:
            sound_dir=temporary/'sound';sound_dir.mkdir()
            used=0
            for index,item in enumerate([*audio_bundle['tracks'],*audio_bundle['voices']],1):
                checkpoint('正在打包配音、音效与口型来源…')
                path=Path(item['path']);raw=path.read_bytes();used+=len(raw)
                if used>1024**3:raise ValueError('音频包超过 1 GiB，请按章节生成')
                if hashlib.sha256(raw).hexdigest()!=item['sha256']:raise ValueError('配音内容已变化，请重新生成')
                relative=f'sound/{index:05d}.wav'
                final_path=assets/relative
                if os.name=='nt' and len(str(final_path).encode('utf-16-le'))//2>=250:
                    raise ValueError('音频路径过长，请将学习工程放到更短的目录')
                (temporary/relative).write_bytes(raw)
                audio_paths[item.get('id') or item['shot_id']]=assets.name+'/'+relative
            sound_report={**audio_bundle['summary'],'sources':[{k:v for k,v in voice.items() if k not in {'keys','path'}} |
                {'path':audio_paths[voice['shot_id']]} for voice in audio_bundle['voices']]}
            (sound_dir/'sound-report.json').write_text(json.dumps(sound_report,ensure_ascii=False,indent=2),encoding='utf-8')
        project=build_resource_project(board,plan,actor_paths=actor_paths,background_paths=background_paths,expression_paths=expression_paths,
                                       audio_bundle=audio_bundle,audio_paths=audio_paths)
        raw=json.dumps(project,ensure_ascii=False,indent=2,allow_nan=False).encode("utf-8")
        if len(raw)>64*1024*1024:
            raise ValueError("工程超过 64 MiB，请按章节拆分")
        (temporary/"assembly-manifest.json").write_text(json.dumps({"version":1,"kind":"existing-resource-assembly",
            "bindings":plan["config"],"catalog_fingerprint":plan["catalog_fingerprint"],
            "source_text_hash":board.get("source_hash",""),"visual_reference_summary":board.get("visual_reference_summary",{}),
            "sound":audio_bundle['summary'] if audio_bundle else {}},
            ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
        report("正在保存现有素材组装的 AECPP 工程…")
        checkpoint()
        # Directory rename is exclusive on Windows; files never replace edits.
        temporary.rename(assets)
        published=True
        _write_new(target,raw)
    except BaseException:
        if published and assets.resolve().parent==target.parent and assets.name==target.stem+".assets":
            shutil.rmtree(assets)
        raise
    finally:
        if temporary.exists() and temporary.parent==target.parent and temporary.name.startswith(".aecpp-materials-"):
            shutil.rmtree(temporary)
    report(f"素材组装完成 · {len(plan['actors'])} 个角色、{len(plan['scenes'])} 个场景；请保留同名 .assets 文件夹")
    return target
