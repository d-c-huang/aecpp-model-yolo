"""Managed video cache locations, verified migration and explicit deletion.

Only hashed importer directories are managed. Source videos outside these
directories and immutable training snapshots are never changed or deleted.
"""
from contextlib import contextmanager, closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
import uuid


KEY = re.compile(r"^[a-f0-9]{64}$")
CONFIG = "cache_settings.json"
OWNER = ".yingge-cache-owner.json"


def active_project_workers(root, *, processes=None):
    """Read only our workers' arguments; return no unrelated process details.

    This also protects against older application versions which predate the
    storage lease. We never terminate, suspend or otherwise control a process.
    """
    import psutil
    root = Path(root).resolve()
    application = Path(__file__).resolve().parent
    candidates = processes if processes is not None else psutil.process_iter(["pid","name"])
    found = []
    for process in candidates:
        try:
            name = (process.info.get("name") or "").casefold()
            if process.pid == os.getpid() or not name.startswith(("python","pypy")):
                continue
            try:
                args = process.cmdline()
                working = Path(process.cwd()).resolve()
            except psutil.AccessDenied:
                # An inaccessible process using this app's own interpreter
                # cannot be safely classified as idle.
                try:
                    same_interpreter = Path(process.exe()).resolve()==Path(sys.executable).resolve()
                except (psutil.Error,OSError):
                    same_interpreter = False
                if same_interpreter:
                    found.append({"pid":process.pid,"task":"unknown"})
                continue
            def resolved(value):
                path = Path(value)
                return (path if path.is_absolute() else working/path).resolve()
            for index,value in enumerate(args):
                if Path(value).name.casefold()=="model_tasks.py" and resolved(value)==application/"model_tasks.py":
                    if len(args)>index+2 and args[index+1] in {"train","train-movie","track","predict"} and resolved(args[index+2])==root:
                        found.append({"pid":process.pid,"task":args[index+1]})
                    break
                if value=="-m" and len(args)>index+3 and args[index+1]=="model_tasks" and working==application:
                    if args[index+2] in {"train","train-movie","track","predict"} and resolved(args[index+3])==root:
                        found.append({"pid":process.pid,"task":args[index+2]})
                    break
                if Path(value).name.casefold()=="online_video.py" and resolved(value)==application/"online_video.py":
                    if len(args)>index+2 and args[index+1]=="--worker":
                        request = resolved(args[index+2])
                        if any(request.is_relative_to(base) for base in cache_roots(root)):
                            found.append({"pid":process.pid,"task":"download"})
                    break
        except (psutil.NoSuchProcess,psutil.ZombieProcess,psutil.AccessDenied,OSError,ValueError):
            continue
    return found


def ensure_no_active_workers(root):
    active = active_project_workers(root)
    if active:
        labels = {"train-movie":"电影训练","train":"截图训练","track":"片段分析","predict":"辅助识别","download":"视频下载","unknown":"无法确认状态的应用任务"}
        tasks = "、".join(f"{labels.get(row['task'],row['task'])}（PID {row['pid']}）" for row in active)
        raise ValueError("本工程仍有后台任务："+tasks+"。请等待完成后再整理缓存；未停止任何任务。")


def _read(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path, value):
    path = Path(path)
    temporary = path.with_name(path.name+"."+uuid.uuid4().hex+".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def get_settings(root):
    root = Path(root).resolve()
    saved = _read(root/CONFIG)
    current = Path(saved.get("current") or root/"online_media").resolve()
    values = [current, *(Path(value).resolve() for value in saved.get("history", []) if isinstance(value, str)), root/"online_media"]
    history = list(dict.fromkeys(str(value) for value in values if value != current))
    return {"version":1,"current":str(current),"history":history}


def cache_roots(root):
    settings = get_settings(root)
    return [Path(settings["current"]), *(Path(value) for value in settings["history"])]


def _reparse(path):
    value = path.lstat()
    return stat.S_ISLNK(value.st_mode) or bool(getattr(value,"st_file_attributes",0) & 0x400)


def _plain_path(path):
    path = Path(path).absolute()
    for part in [path, *path.parents]:
        if part.exists() and _reparse(part):
            raise ValueError("缓存路径不能包含符号链接或目录联接")
    return path.resolve()


def _tree_files(folder):
    folder = _plain_path(folder)
    files = []
    for parent, directories, names in os.walk(folder, followlinks=False):
        for name in [*directories,*names]:
            path = Path(parent)/name
            if _reparse(path) or not path.resolve().is_relative_to(folder):
                raise ValueError("缓存目录包含指向外部的链接，已拒绝操作")
            if path.is_file():
                files.append(path)
    return files


def _owner(root):
    return hashlib.sha256(os.path.normcase(str(Path(root).resolve())).encode("utf-8")).hexdigest()


def _check_owner(root, folder):
    marker = _read(folder/OWNER)
    if marker and marker.get("project") != _owner(root):
        raise ValueError("该缓存目录属于另一个工程，请选择独立目录")


def _prepare_location(root, target):
    root = Path(root).resolve()
    target = _plain_path(Path(target).expanduser())
    if target == Path(target.anchor) or root == target or root.is_relative_to(target):
        raise ValueError("请选择专用缓存文件夹，不能使用磁盘根目录、工程目录或其上级目录")
    for name in ("frames","runs","logs","movie_cache","movie_features"):
        protected = root/name
        if target == protected or target.is_relative_to(protected):
            raise ValueError("不能把缓存目录放进标注、模型或日志目录")
    for previous in cache_roots(root):
        if target != previous and (target.is_relative_to(previous) or previous.is_relative_to(target)):
            raise ValueError("新旧缓存目录不能互相包含，请选择并列的独立文件夹")
    target.mkdir(parents=True,exist_ok=True)
    _check_owner(root,target)
    probe = target/(".write-check-"+uuid.uuid4().hex)
    try:
        probe.write_bytes(b"")
    finally:
        probe.unlink(missing_ok=True)
    _write(target/OWNER,{"project":_owner(root),"version":1})
    return target


class StorageBusy(ValueError):
    """A transient project storage lease held by another operation."""


@contextmanager
def storage_lease(root):
    """Exclude migration/deletion from downloads, including other app instances."""
    root = Path(root).resolve()
    root.mkdir(parents=True,exist_ok=True)
    fd = os.open(root/".cache-storage.lock",os.O_CREAT|os.O_RDWR)
    acquired = False
    try:
        if not os.fstat(fd).st_size:
            os.write(fd,b"0")
        os.lseek(fd,0,os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd,msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            raise StorageBusy("缓存正在下载或整理，请等待当前任务结束") from exc
        acquired = True
        yield
    finally:
        if acquired:
            os.lseek(fd,0,os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd,msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(fd,fcntl.LOCK_UN)
        os.close(fd)


def locate_cache(root, key):
    if not KEY.fullmatch(key):
        raise ValueError("缓存编号无效")
    roots = cache_roots(root)
    candidates = [folder/key for folder in roots if (folder/key).is_dir()]
    for folder in candidates:
        _plain_path(folder)
        info = _read(folder/"completed.json")
        relative = info.get("relative_path")
        if relative:
            path = folder/relative
            if path.resolve().is_relative_to(folder) and path.is_file():
                value = path.stat()
                if (value.st_size,value.st_mtime_ns)==(info.get("size"),info.get("mtime_ns")):
                    return folder
    if candidates:
        return _plain_path(candidates[0])
    current = roots[0]
    _plain_path(current)
    current.mkdir(parents=True,exist_ok=True)
    _check_owner(root,current)
    _write(current/OWNER,{"project":_owner(root),"version":1})
    return current/key


def _movies(root):
    path = Path(root)/"annotations.sqlite"
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT id,name,source_path FROM movies")]


def _references(folder, movies):
    return [movie for movie in movies if movie.get("source_path")
            and Path(movie["source_path"]).resolve().is_relative_to(folder)]


def scan_caches(root):
    settings = get_settings(root)
    movies = _movies(root)
    rows, ignored = [], []
    for base in cache_roots(root):
        if not base.exists():
            continue
        try:
            _plain_path(base)
            _check_owner(root,base)
        except (OSError,ValueError) as exc:
            ignored.append(f"{base}：{exc}")
            continue
        for folder in sorted(base.iterdir()):
            if not KEY.fullmatch(folder.name) or not folder.is_dir():
                continue
            complete,pending = _read(folder/"completed.json"),_read(folder/"pending.json")
            if not complete and not pending:
                continue
            error = ""
            try:
                files = _tree_files(folder)
                size = sum(path.stat().st_size for path in files)
            except (OSError,ValueError) as exc:
                size,error = 0,str(exc)
            references = _references(folder.resolve(),movies)
            valid = False
            media = ""
            if complete.get("relative_path"):
                candidate = folder/complete["relative_path"]
                if candidate.resolve().is_relative_to(folder.resolve()) and candidate.is_file():
                    value = candidate.stat()
                    valid = (value.st_size,value.st_mtime_ns)==(complete.get("size"),complete.get("mtime_ns"))
                    media = str(candidate)
            state = "unsafe" if error else "complete" if valid else "incomplete"
            rows.append({"path":str(folder),"key":folder.name,"root":str(base),"title":complete.get("title") or pending.get("config",{}).get("url") or folder.name[:12],
                         "size":size,"status":state,"error":error,"movie_ids":[m["id"] for m in references],
                         "movie_names":[m["name"] for m in references],"media_path":media,
                         "source_url":complete.get("source_url") or pending.get("config",{}).get("url",""),
                         "disposable":not references and not error})
    current = Path(settings["current"])
    nearest = next((part for part in [current,*current.parents] if part.exists()),Path.cwd())
    space = shutil.disk_usage(nearest)
    return {"settings":settings,"items":rows,"size":sum(row["size"] for row in rows),
            "free":space.free,"disk_total":space.total,"warnings":ignored}


def _safe_folder(root, value):
    path = Path(value).absolute()
    if not KEY.fullmatch(path.name):
        raise ValueError("只允许管理已登记的在线视频缓存目录")
    if path.parent.resolve() not in cache_roots(root):
        raise ValueError("目标不在此工程登记的缓存目录中")
    folder = _plain_path(path)
    _check_owner(root,folder.parent)
    if not (_read(folder/"completed.json") or _read(folder/"pending.json")):
        raise ValueError("缺少有效缓存登记，已拒绝删除未知文件夹")
    _tree_files(folder)
    return folder


def _checksum(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(8*1024*1024):
            value.update(chunk)
    return value.hexdigest()


def _copy_verified(source, destination, report):
    files = _tree_files(source)
    if destination.exists():
        existing = _tree_files(destination)
        if {p.relative_to(source) for p in files} != {p.relative_to(destination) for p in existing}:
            raise ValueError(f"目标已存在不同缓存，不会覆盖：{destination}")
        for path in files:
            other = destination/path.relative_to(source)
            if path.stat().st_size != other.stat().st_size or _checksum(path) != _checksum(other):
                if path.name in {"request.json","download.json","completed.json"}:
                    original,existing_value = _read(path),_read(other)
                    original = _replace_paths(original,[(source,destination)])
                    if path.name=="completed.json":
                        # Destination filesystem timestamp precision may differ.
                        # The media bytes are still compared independently.
                        original.pop("mtime_ns",None)
                        existing_value.pop("mtime_ns",None)
                    if original and original==existing_value:
                        continue  # A verified earlier attempt may have remapped paths.
                raise ValueError(f"目标已存在不同文件，不会覆盖：{other}")
        return
    staging = destination.with_name(".moving-"+destination.name+"-"+uuid.uuid4().hex)
    staging.mkdir()
    try:
        for index,path in enumerate(files,1):
            relative = path.relative_to(source)
            other = staging/relative
            other.parent.mkdir(parents=True,exist_ok=True)
            report(f"复制并校验 {index}/{len(files)} · {path.name}")
            shutil.copy2(path,other)
            if path.stat().st_size != other.stat().st_size or _checksum(path) != _checksum(other):
                raise OSError(f"复制校验失败：{path.name}")
        staging.rename(destination)
    finally:
        if staging.exists():
            _tree_files(staging)
            shutil.rmtree(staging)


def _tree_signature(folder):
    return {path.relative_to(folder).as_posix():(path.stat().st_size,path.stat().st_mtime_ns)
            for path in _tree_files(folder)}


def _refresh_migrated_completion(source,destination):
    """Preserve completeness when copy2 rounds timestamps on another volume."""
    original = _read(source/"completed.json")
    if not original:
        return
    relative = original.get("relative_path")
    if not isinstance(relative,str) or Path(relative).is_absolute():
        raise ValueError("缓存完成记录中的视频路径无效")
    old = (source/relative).resolve()
    new = (destination/relative).resolve()
    if not old.is_relative_to(source.resolve()) or not new.is_relative_to(destination.resolve()):
        raise ValueError("缓存完成记录中的视频路径越界，已保留原缓存")
    if not old.is_file() or not new.is_file():
        return  # A previously incomplete cache must remain incomplete.
    before,after = old.stat(),new.stat()
    if (before.st_size,before.st_mtime_ns)!=(original.get("size"),original.get("mtime_ns")):
        return
    completed = _read(destination/"completed.json")
    completed.update(size=after.st_size,mtime_ns=after.st_mtime_ns)
    if isinstance(completed.get("metadata"),dict):
        completed["metadata"]["size"] = after.st_size
    _write(destination/"completed.json",completed)


def _mapped(value, mappings):
    if not isinstance(value,str) or not value or not Path(value).is_absolute():
        return value
    path = Path(value).resolve()
    for old,new in mappings:
        if path.is_relative_to(old):
            return str(new/path.relative_to(old))
    return value


def _replace_paths(value,mappings):
    if isinstance(value,dict):
        return {key:_replace_paths(item,mappings) for key,item in value.items()}
    if isinstance(value,list):
        return [_replace_paths(item,mappings) for item in value]
    return _mapped(value,mappings)


def _switch_references(root, mappings, settings):
    database = Path(root)/"annotations.sqlite"
    old_config = (Path(root)/CONFIG).read_bytes() if (Path(root)/CONFIG).exists() else None
    with closing(sqlite3.connect(database)) as db:
        jobs = Path(root)/"online_jobs.sqlite"
        attached = jobs.is_file()
        if attached:
            db.execute("ATTACH DATABASE ? AS queue",(str(jobs),))
        try:
            db.execute("BEGIN IMMEDIATE")
            for movie_id,source in db.execute("SELECT id,source_path FROM movies").fetchall():
                replacement = _mapped(source,mappings)
                if replacement != source:
                    value = Path(replacement).stat()
                    db.execute("UPDATE movies SET source_path=?,source_size=?,source_mtime=? WHERE id=?",
                               (replacement,value.st_size,value.st_mtime_ns,movie_id))
            # Sources currently use symbolic keys; preserve those and update any
            # older schema rows which stored actual absolute source paths.
            for table in ("subtitle_tracks","scripts"):
                for identifier,source in db.execute(f"SELECT id,source FROM {table}").fetchall():
                    replacement = _mapped(source,mappings)
                    if replacement != source:
                        db.execute(f"UPDATE {table} SET source=? WHERE id=?",(replacement,identifier))
            if attached:
                for identifier,payload in db.execute("SELECT id,data FROM queue.online_jobs").fetchall():
                    previous = json.loads(payload)
                    changed = _replace_paths(previous,mappings)
                    if changed != previous:
                        db.execute("UPDATE queue.online_jobs SET data=? WHERE id=?",(json.dumps(changed,ensure_ascii=False),identifier))
            _write(Path(root)/CONFIG,settings)
            db.commit()
        except BaseException:
            db.rollback()
            if old_config is None:
                (Path(root)/CONFIG).unlink(missing_ok=True)
            else:
                (Path(root)/CONFIG).write_bytes(old_config)
            raise


@contextmanager
def automatic_learning_idle(root):
    """Exclude in-process automatic readers, including another app instance."""
    from online_jobs import _ProjectLock
    continuous = _ProjectLock(Path(root).resolve()/".continuous-learning.lock")
    lock = None
    try:
        if continuous.fd is None:
            raise ValueError("持续自动学习已启用，请停止后再整理缓存")
        folder = Path(root).resolve()/"auto_learning"
        folder.mkdir(parents=True,exist_ok=True)
        lock = _ProjectLock(folder/".worker.lock")
        if lock.fd is None:
            raise ValueError("自动学习正在读取视频，请暂停并等待当前任务结束后再整理缓存")
        yield
    finally:
        if lock is not None:
            lock.close()
        continuous.close()


def change_location(root, target, *, migrate=False, report=lambda _:None):
    root = Path(root).resolve()
    with storage_lease(root), automatic_learning_idle(root):
        ensure_no_active_workers(root)
        destination = _prepare_location(root,target)
        settings = {"version":1,"current":str(destination),"history":list(dict.fromkeys(
            str(path) for path in cache_roots(root) if path != destination))}
        if not migrate:
            _write(root/CONFIG,settings)
            return {"migrated":0,"warnings":[],"settings":settings}
        rows = scan_caches(root)["items"]
        if any(row["status"]=="unsafe" for row in rows):
            raise ValueError("存在不安全缓存路径，请先处理后再迁移")
        sources = [_safe_folder(root,row["path"]) for row in rows if Path(row["root"]) != destination]
        size = sum(row["size"] for row in rows if Path(row["root"]) != destination)
        if shutil.disk_usage(destination).free < size+1024*1024:
            raise ValueError("目标磁盘剩余空间不足以校验迁移副本")
        mappings,signatures = [],{}
        for source in sources:
            ensure_no_active_workers(root)
            target_folder = destination/source.name
            signatures[source] = _tree_signature(source)
            _copy_verified(source,target_folder,report)
            if _tree_signature(source)!=signatures[source]:
                raise OSError("迁移期间原缓存发生变化，已保留原文件；请重试")
            mappings.append((source,target_folder))
        # Update absolute downloader paths so partial downloads can resume.
        for _,target_folder in mappings:
            for name in ("request.json","download.json","completed.json"):
                for path in target_folder.rglob(name):
                    value = _read(path)
                    if value:
                        changed = _replace_paths(value,mappings)
                        if changed != value:
                            _write(path,changed)
        for source,target_folder in mappings:
            _refresh_migrated_completion(source,target_folder)
        _switch_references(root,mappings,settings)
        warnings = []
        for source,_ in mappings:
            try:
                ensure_no_active_workers(root)
                _safe_folder(root,source)
                if _tree_signature(source)!=signatures[source]:
                    raise ValueError("原缓存发生变化，保留旧副本")
                shutil.rmtree(source)
            except (OSError,ValueError) as exc:
                warnings.append(f"新位置已启用，旧副本未移除：{source} · {exc}")
        report(f"迁移完成 · {len(mappings)} 个缓存")
        return {"migrated":len(mappings),"warnings":warnings,"settings":settings}


def remove_caches(root, paths, *, allow_referenced=False, report=lambda _:None):
    with storage_lease(root), automatic_learning_idle(root):
        ensure_no_active_workers(root)
        folders = [_safe_folder(root,value) for value in dict.fromkeys(map(str,paths))]
        movies = _movies(root)
        affected = [movie for folder in folders for movie in _references(folder,movies)]
        if affected and not allow_referenced:
            raise ValueError("所选缓存仍是电影原片；删除后需重新下载才能抽帧或再次训练")
        size = sum(sum(path.stat().st_size for path in _tree_files(folder)) for folder in folders)
        for folder in folders:
            ensure_no_active_workers(root)
            report("移除视频缓存 · "+folder.name[:12])
            shutil.rmtree(folder)
        queue_path = Path(root)/"online_jobs.sqlite"
        if queue_path.is_file():
            with closing(sqlite3.connect(queue_path)) as db, db:
                for identifier,payload in db.execute("SELECT id,data FROM online_jobs").fetchall():
                    job = json.loads(payload)
                    result = job.get("result") or {}
                    def removed(value):
                        return bool(value and any(Path(value).resolve().is_relative_to(folder) for folder in folders))
                    if removed(result.get("thumbnail_path")):
                        result["thumbnail_path"] = ""
                    if job.get("status")=="completed" and removed(result.get("path")):
                        job.update(status="cancelled",phase="cancelled",learn=False,
                                   message="视频缓存已清理；可重试下载，原标注和模型保留",error="")
                    db.execute("UPDATE online_jobs SET status=?,data=? WHERE id=?",(job["status"],json.dumps(job,ensure_ascii=False),identifier))
        # Keep movie/annotation records. Their missing source is deliberately
        # visible; re-importing the same content restores the canonical source.
        return {"removed":len(folders),"size":size,"movie_ids":list(dict.fromkeys(movie["id"] for movie in affected))}


def cleanup_unused(root, *, incomplete_only=False, report=lambda _:None):
    rows = scan_caches(root)["items"]
    paths = [row["path"] for row in rows if row["disposable"] and
             (not incomplete_only or row["status"]=="incomplete")]
    return remove_caches(root,paths,report=report)
