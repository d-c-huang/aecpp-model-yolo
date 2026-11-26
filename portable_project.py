"""Verified project archives. Historical training runs are copied byte-for-byte.

Import relocates the active databases only. New training must take a fresh source
snapshot; a historical snapshot deliberately continues to describe its old run.
"""
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import tempfile
import zipfile

import cache_storage
from online_jobs import _ProjectLock


FORMAT = "aecpp-project"
VERSION = 1
MANIFEST = "manifest.json"
VIDEOS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".ts", ".m2ts", ".flv", ".wmv", ".mpeg", ".mpg"}
DATABASES = {"annotations.sqlite", "online_jobs.sqlite"}
TRANSIENT = {".cache-storage.lock", ".online_queue.lock", cache_storage.OWNER}


def _relative(value):
    if not isinstance(value, str) or not value or "\\" in value or any(ord(char) < 32 for char in value):
        raise ValueError("迁移包包含不安全的文件路径")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(
        part in {".", ".."} or any(char in part for char in ':<>|?*') or part.endswith((".", " "))
        or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part)
        for part in path.parts
    ):
        raise ValueError("迁移包包含不安全的文件路径")
    return value


def _plain(path):
    return cache_storage._plain_path(path)


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _signature(path):
    value = path.stat()
    return value.st_size, value.st_mtime_ns


@contextmanager
def _idle_project(root, is_busy):
    def check():
        if is_busy is not None and is_busy():
            raise ValueError("工程仍在执行任务，请等待完成后再导出迁移包")
        cache_storage.ensure_no_active_workers(root)
    check()
    with cache_storage.storage_lease(root):
        lock = _ProjectLock(root / ".online_queue.lock")
        if lock.fd is None:
            raise ValueError("在线视频队列仍在运行，请暂停并等待当前任务结束后再导出")
        try:
            check()
            yield check
        finally:
            lock.close()


def _backup(source, destination):
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as db:
        with closing(sqlite3.connect(destination)) as target:
            db.backup(target)
            if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("工程数据库校验失败")


def _portable_weights(root, files, add):
    """Resolve detector hashes without loading or modifying model checkpoints."""
    def read(path):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}
    runs = sorted((root / "runs").glob("*/completed.json"), key=lambda path: path.stat().st_mtime_ns, reverse=True)
    expected = {}
    latest = None
    latest_run = None
    settings = {}
    candidates = {path for path in files.values() if path.suffix.lower() == ".pt" and path.name != "movie_temporal.pt"}
    application = Path(__file__).resolve().parent
    candidates.update((application / "weights").glob("*.pt"))
    for marker in runs:
        completed = read(marker)
        corpus = read(marker.parent / "movie_corpus.json")
        if completed.get("kind") != "movie-temporal" and not corpus:
            continue
        digest = corpus.get("weights_sha256")
        job = read(root / "logs" / (marker.parent.name + "-train.json"))
        job.update(read(marker.parent / "job.json"))
        for key in ("weights_path", "weights"):
            value = job.get(key)
            if isinstance(value, str) and value:
                path = Path(value)
                candidates.update([path] if path.is_absolute() else [root / path, application / path, application / "weights" / path])
        if latest is None:
            latest = digest or ""
            latest_run = marker.parent.relative_to(root).as_posix()
            metrics = read(marker.parent / "movie_metrics.json")
            values = {**metrics, **completed, **job}
            settings = {key: values[key] for key in ("sample_fps", "patience") if key in values}
            settings["epochs"] = job.get("epochs", values.get("requested_epochs", 30))
            settings["learning_rate"] = job.get("learning_rate", values.get("initial_learning_rate", .001))
        if digest:
            if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise ValueError("历史模型检测器校验值无效")
            expected.setdefault(digest, []).append(marker.parent.name)
    located = {}
    for path in candidates:
        if not expected:
            break
        if path.is_file():
            path = _plain(path)
            digest = _hash(path)
            if digest in expected:
                located.setdefault(digest, path)
    detectors, missing = [], []
    for digest, run_ids in expected.items():
        if digest in located:
            relative = f"weights/{digest}.pt"
            add(located[digest], relative)
            detectors.append({"sha256": digest, "path": relative, "runs": run_ids})
        else:
            missing.append({"sha256": digest, "runs": run_ids})
    return {"version": 1, "detectors": detectors, "missing_weights": missing,
            "latest_movie_detector": (f"weights/{latest}.pt" if latest in located else "" if latest == "" else None),
            "latest_movie_settings": settings, "latest_movie_run": latest_run}


def export_project(root, destination_zip, *, include_videos=True, is_busy=None, report=lambda _: None):
    """Export idle project; return counts, missing sources and resumability.

    ``is_busy`` is an optional zero-argument UI guard. The destination must be a
    new file outside the project and its configured cache directories.
    """
    root = _plain(root)
    destination = _plain(destination_zip)
    if not (root / "annotations.sqlite").is_file():
        raise ValueError("请选择包含 annotations.sqlite 的学习工程")
    if destination.exists():
        raise ValueError("迁移包已存在，请选择新的文件名")
    saved_cache = cache_storage._read(root / cache_storage.CONFIG)
    for value in [saved_cache.get("current"), *saved_cache.get("history", [])]:
        if isinstance(value, str) and value:
            _plain(value)  # Validate before cache_roots resolves any junctions.
    caches = cache_storage.cache_roots(root)
    if any(destination.is_relative_to(base) for base in [root, *caches]):
        raise ValueError("请把迁移包保存在工程和缓存目录之外")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with _idle_project(root, is_busy) as check_idle, tempfile.TemporaryDirectory(
        prefix=".aecpp-export-", dir=destination.parent
    ) as temporary:
        temporary = Path(temporary)
        database_versions = {root / (name + suffix): _signature(root / (name + suffix)) if (root / (name + suffix)).exists() else None
                             for name in DATABASES for suffix in ("", "-wal")}
        for name in DATABASES:
            if (root / name).is_file():
                _backup(root / name, temporary / name)
        with closing(sqlite3.connect(temporary / "annotations.sqlite")) as db:
            movies = db.execute("SELECT id,name,source_path FROM movies ORDER BY id").fetchall()
            frame_count = db.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
        declared_sources = {Path(source).resolve() for _, _, source in movies if source}
        files = {}
        originals = {}
        mappings = [{"source": str(root), "target": ""}]
        for base in caches:
            if base.exists():
                _plain(base)
                cache_storage._check_owner(root, base)
            mappings.append({"source": str(base), "target": "online_media"})

        def add(source, relative):
            relative = _relative(relative)
            source = _plain(source)
            if not source.is_file():
                raise ValueError(f"资源不是普通文件：{relative}")
            if relative in files and source != files[relative]:
                if _hash(source) != _hash(files[relative]):
                    raise ValueError(f"缓存文件冲突：{relative}")
                return
            files[relative] = source
            originals[source] = _signature(source)

        def project_files():
            result = []
            for path in cache_storage._tree_files(root):
                relative = path.relative_to(root).as_posix()
                if path.name in TRANSIENT or relative in {name + suffix for name in DATABASES for suffix in ("-wal", "-shm", "-journal")}:
                    continue
                if not include_videos and not relative.startswith("runs/") and (path in declared_sources or path.suffix.lower() in VIDEOS):
                    continue
                result.append(path)
            return result

        initial_paths = project_files()
        for path in initial_paths:
            relative = path.relative_to(root).as_posix()
            for base in sorted(caches, key=lambda value: len(str(value)), reverse=True):
                if path.is_relative_to(base):
                    relative = "online_media/" + path.relative_to(base).as_posix()
                    break
            add(temporary / relative if relative in DATABASES else path, relative)
        if include_videos:
            for base in caches:
                if base.is_relative_to(root) or not base.exists():
                    continue
                base = _plain(base)
                for folder in base.iterdir():
                    if cache_storage.KEY.fullmatch(folder.name) and folder.is_dir():
                        for path in cache_storage._tree_files(folder):
                            if path.name not in TRANSIENT:
                                add(path, "online_media/" + path.relative_to(base).as_posix())

        movie_paths = []
        missing = []
        for identifier, name, source in movies:
            relative = None
            if include_videos and source:
                path = _plain(source)
                if path.is_file():
                    for mapping in sorted(mappings, key=lambda value: len(value["source"]), reverse=True):
                        base = Path(mapping["source"])
                        if path.is_relative_to(base):
                            relative = (PurePosixPath(mapping["target"]) / path.relative_to(base).as_posix()).as_posix()
                            break
                    if relative is None:
                        relative = f"portable_media/{identifier}/{path.name}"
                        mappings.append({"source": str(path), "target": relative})
                    add(path, relative)
            if relative is None:
                missing.append({"id": identifier, "name": name})
            movie_paths.append({"id": identifier, "path": relative})

        portable = _portable_weights(root, files, add)
        _json(temporary / "portable_settings.json", portable)
        files.pop("portable_settings.json", None)
        add(temporary / "portable_settings.json", "portable_settings.json")
        summary = {"movies": len(movies), "frames": frame_count,
                   "models": sum(name.startswith("runs/") and name.endswith(".pt") for name in files),
                   "files": len(files), "include_videos": bool(include_videos), "missing_videos": missing,
                   "missing_weights": portable["missing_weights"],
                   "can_resume_latest_model": bool(movies) and not missing and portable["latest_movie_detector"] is not None
                       and bool(portable["latest_movie_run"]) and (root / portable["latest_movie_run"] / "movie_temporal.pt").is_file(),
                   "can_continue_movie_learning": bool(movies) and not missing}
        entries = []
        package = temporary / "project.zip"
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
            for index, (relative, source) in enumerate(sorted(files.items()), 1):
                report(f"打包工程资源 · {index}/{len(files)} · {relative}")
                digest = hashlib.sha256()
                size = 0
                info = zipfile.ZipInfo("project/" + relative)
                info.compress_type = zipfile.ZIP_STORED if source.suffix.lower() in VIDEOS | {".pt", ".npz", ".jpg", ".png"} else zipfile.ZIP_DEFLATED
                with source.open("rb") as reader, archive.open(info, "w", force_zip64=True) as writer:
                    while block := reader.read(8 * 1024 * 1024):
                        digest.update(block)
                        size += len(block)
                        writer.write(block)
                entries.append({"path": relative, "size": size, "sha256": digest.hexdigest(), "mtime_ns": originals[source][1]})
            summary["bytes"] = sum(entry["size"] for entry in entries)
            manifest = {"format": FORMAT, "version": VERSION, "include_videos": bool(include_videos), "created_at": datetime.now(timezone.utc).isoformat(),
                        "files": entries, "mappings": mappings, "movies": movie_paths, "summary": summary}
            archive.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False))
        check_idle()
        if (set(initial_paths) != set(project_files()) or any(_signature(path) != signature for path, signature in originals.items())
                or any((_signature(path) if path.exists() else None) != signature for path, signature in database_versions.items())):
            raise ValueError("导出期间工程资源发生变化，请等待工程空闲后重试")
        # Publish only after the complete archive and idle-state checks succeed.
        # Exclusive creation prevents overwriting a file created during export.
        try:
            with destination.open("xb") as output, package.open("rb") as source:
                shutil.copyfileobj(source, output, 8 * 1024 * 1024)
        except FileExistsError:
            raise ValueError("迁移包目标已存在，请选择新的文件名") from None
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        return {**summary, "archive": str(destination), "archive_bytes": destination.stat().st_size}


def _read_manifest(archive):
    infos = archive.infolist()
    names = [entry.filename for entry in infos]
    if len(names) != len(set(name.casefold() for name in names)) or MANIFEST not in names:
        raise ValueError("迁移包目录重复或缺少清单")
    for info in infos:
        _relative(info.filename)
        mode = info.external_attr >> 16
        if info.is_dir() or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in {0, stat.S_IFREG}) or info.flag_bits & 1:
            raise ValueError("迁移包只允许普通文件，不能含符号链接或加密文件")
    if archive.getinfo(MANIFEST).file_size > 16 * 1024 * 1024:
        raise ValueError("迁移包清单过大")
    manifest = json.loads(archive.read(MANIFEST))
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or manifest.get("version") != VERSION:
        raise ValueError("不支持的工程迁移包格式")
    entries = manifest.get("files")
    if not isinstance(entries, list) or len(entries) > 200000:
        raise ValueError("迁移包文件清单无效")
    paths = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("迁移包文件清单无效")
        path = _relative(entry.get("path"))
        if path.casefold() in paths:
            raise ValueError("迁移包包含重复路径")
        paths.add(path.casefold())
        if type(entry.get("size")) is not int or entry["size"] < 0 or type(entry.get("mtime_ns")) is not int or not 0 <= entry["mtime_ns"] <= 2**63 - 1 or not re.fullmatch(r"[a-f0-9]{64}", str(entry.get("sha256", ""))):
            raise ValueError("迁移包文件校验信息无效")
        info = archive.getinfo("project/" + path)
        if info.file_size != entry["size"]:
            raise ValueError("迁移包文件大小不符")
    if set(names) != {MANIFEST, *("project/" + entry["path"] for entry in entries)} or not {"annotations.sqlite", "portable_settings.json"}.issubset(paths):
        raise ValueError("迁移包文件与清单不一致")
    if type(manifest.get("include_videos")) is not bool:
        raise ValueError("迁移包原片设置无效")
    if not isinstance(manifest.get("movies"), list) or not isinstance(manifest.get("mappings"), list):
        raise ValueError("迁移包路径映射无效")
    for mapping in manifest["mappings"]:
        if not isinstance(mapping, dict) or not isinstance(mapping.get("source"), str) or not mapping["source"]:
            raise ValueError("迁移包路径映射无效")
        if mapping.get("target") != "":
            _relative(mapping.get("target"))
    for movie in manifest["movies"]:
        if not isinstance(movie, dict) or type(movie.get("id")) is not int:
            raise ValueError("迁移包影片清单无效")
        if movie.get("path") is not None and _relative(movie["path"]).casefold() not in paths:
            raise ValueError("迁移包缺少原片文件")
    return manifest


def _relocate(staging, destination, manifest):
    mappings = sorted(manifest["mappings"], key=lambda value: len(value["source"]), reverse=True)

    def replace(value):
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        if not isinstance(value, str):
            return value
        normalized = value.replace("\\", "/")
        for mapping in mappings:
            old = mapping["source"].replace("\\", "/").rstrip("/")
            if normalized.casefold() == old.casefold():
                return str(destination / mapping["target"])
            if normalized.casefold().startswith(old.casefold() + "/"):
                suffix = _relative(normalized[len(old) + 1:])
                return str(destination / mapping["target"] / suffix)
        return value

    with closing(sqlite3.connect(staging / "annotations.sqlite")) as db, db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("工程数据库校验失败")
        names = dict(db.execute("SELECT id,name FROM movies"))
        frame_count = db.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
        actual_ids = set(names)
        if actual_ids != {movie["id"] for movie in manifest["movies"]} or len(actual_ids) != len(manifest["movies"]):
            raise ValueError("影片清单与工程数据库不一致")
        for movie in manifest["movies"]:
            relative = movie["path"]
            if relative:
                source = staging / relative
                metadata = source.stat()
                db.execute("UPDATE movies SET source_path=?,source_size=?,source_mtime=? WHERE id=?",
                           (str(destination / relative), metadata.st_size, metadata.st_mtime_ns, movie["id"]))
            else:
                db.execute("UPDATE movies SET source_path=NULL,source_size=NULL,source_mtime=NULL WHERE id=?", (movie["id"],))
        for table in ("subtitle_tracks", "scripts"):
            for identifier, source in db.execute(f"SELECT id,source FROM {table}").fetchall():
                db.execute(f"UPDATE {table} SET source=? WHERE id=?", (replace(source), identifier))
        for identifier, path in db.execute("SELECT id,path FROM frames").fetchall():
            # Current frame paths are relative; support old absolute paths too.
            changed = replace(path)
            if changed != path:
                changed = Path(changed).relative_to(destination).as_posix()
                db.execute("UPDATE frames SET path=? WHERE id=?", (changed, identifier))
            _relative(changed)
            if not (staging / changed).is_file():
                raise ValueError(f"迁移包缺少截图：{changed}")
    jobs = staging / "online_jobs.sqlite"
    if jobs.is_file():
        movie_paths = {movie["id"]: str(destination / movie["path"]) if movie["path"] else "" for movie in manifest["movies"]}
        with closing(sqlite3.connect(jobs)) as db, db:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("在线视频队列数据库校验失败")
            for identifier, payload in db.execute("SELECT id,data FROM online_jobs").fetchall():
                job = replace(json.loads(payload))
                result = job.get("result")
                if isinstance(result, dict) and result.get("movie_id") in movie_paths:
                    result["path"] = movie_paths[result["movie_id"]]
                if job.get("status") in {"running", "queued", "paused"}:
                    job.update(status="paused", phase="paused", control=None, message="已迁移到新电脑；确认后继续队列")
                db.execute("UPDATE online_jobs SET data=?,status=? WHERE id=?", (json.dumps(job, ensure_ascii=False), job["status"], identifier))
            db.execute("INSERT OR REPLACE INTO online_meta VALUES('paused','1')")
    # Cache metadata is mutable operational state; runs/ remains untouched.
    for path in (staging / "online_media").rglob("*.json"):
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        updated = replace(previous)
        if updated != previous:
            _json(path, updated)
    for folder in (staging / "online_media").glob("*"):
        marker = folder / "completed.json"
        if not marker.is_file():
            continue
        data = json.loads(marker.read_text(encoding="utf-8"))
        relative = _relative(data.get("relative_path"))
        source = folder / relative
        if source.is_file():
            value = source.stat()
            data.update(size=value.st_size, mtime_ns=value.st_mtime_ns)
            _json(marker, data)
        else:
            marker.unlink()
    _json(staging / cache_storage.CONFIG, {"version": 1, "current": str(destination / "online_media"), "history": []})
    (staging / "online_media").mkdir(exist_ok=True)
    _json(staging / "online_media" / cache_storage.OWNER, {"version": 1, "project": cache_storage._owner(destination)})
    settings = json.loads((staging / "portable_settings.json").read_text(encoding="utf-8"))
    if not isinstance(settings, dict) or not isinstance(settings.get("missing_weights"), list):
        raise ValueError("迁移训练设置无效")
    latest_detector = settings.get("latest_movie_detector")
    if latest_detector is not None and not isinstance(latest_detector, str):
        raise ValueError("迁移检测器路径无效")
    if latest_detector:
        _relative(latest_detector)
        if not (staging / latest_detector).is_file():
            raise ValueError("迁移包缺少最近模型使用的人物检测权重")
    if not isinstance(settings.get("detectors"), list):
        raise ValueError("迁移检测器清单无效")
    for detector in settings["detectors"]:
        if not isinstance(detector, dict) or not isinstance(detector.get("sha256"), str):
            raise ValueError("迁移检测器清单无效")
        path = staging / _relative(detector.get("path"))
        if not path.is_file() or _hash(path) != detector["sha256"]:
            raise ValueError("迁移检测器校验失败")
    missing = [{"id": movie["id"], "name": names[movie["id"]]} for movie in manifest["movies"] if not movie["path"]]
    latest_run = settings.get("latest_movie_run")
    if latest_run is not None:
        _relative(latest_run)
    return {"movies": len(names), "frames": frame_count, "files": len(manifest["files"]),
            "models": sum(entry["path"].startswith("runs/") and entry["path"].endswith(".pt") for entry in manifest["files"]),
            "bytes": sum(entry["size"] for entry in manifest["files"]), "missing_videos": missing,
            "include_videos": manifest["include_videos"],
            "missing_weights": settings["missing_weights"], "can_continue_movie_learning": bool(names) and not missing,
            "can_resume_latest_model": bool(names) and not missing and latest_detector is not None
                and bool(latest_run) and (staging / latest_run / "movie_temporal.pt").is_file()}


def import_project(archive, destination, *, report=lambda _: None):
    """Validate all archive bytes, then publish to a new or empty directory."""
    archive = _plain(archive)
    destination = _plain(destination)
    existed = destination.exists()
    if existed and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError("导入位置必须是全新或空文件夹，不能覆盖现有工程")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".aecpp-import-", dir=destination.parent) as temporary:
        staging = Path(temporary) / "project"
        staging.mkdir()
        try:
            with zipfile.ZipFile(archive) as package:
                manifest = _read_manifest(package)
                required = sum(entry["size"] for entry in manifest["files"])
                if required > shutil.disk_usage(staging).free:
                    raise ValueError("目标磁盘空间不足")
                for index, entry in enumerate(manifest["files"], 1):
                    report(f"校验并导入 · {index}/{len(manifest['files'])} · {entry['path']}")
                    target = staging / entry["path"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256()
                    with package.open("project/" + entry["path"]) as source, target.open("xb") as output:
                        while block := source.read(8 * 1024 * 1024):
                            digest.update(block)
                            output.write(block)
                    if digest.hexdigest() != entry["sha256"]:
                        raise ValueError(f"迁移包文件校验失败：{entry['path']}")
                    os.utime(target, ns=(entry["mtime_ns"], entry["mtime_ns"]))
            summary = _relocate(staging, destination, manifest)
            # Recheck at publication time; never replace a populated directory.
            if destination.exists():
                if not existed or any(destination.iterdir()):
                    raise ValueError("导入目标已发生变化，请选择新的文件夹")
                destination.rmdir()
            try:
                staging.rename(destination)
            except BaseException:
                if existed and not destination.exists():
                    destination.mkdir()
                raise
        except (zipfile.BadZipFile, KeyError, TypeError, json.JSONDecodeError, sqlite3.Error) as exc:
            raise ValueError("工程迁移包损坏或格式不正确") from exc
    return {**summary, "project": str(destination)}
