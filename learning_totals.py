"""Pure aggregation of durable learning summaries and restricted-source skips.

No files, model reports, network connections or queue workers are opened here.
Callers may cache this small snapshot and supply the current movie catalogue.
Previously published results remain visible while a replacement job runs.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def _dict(value):
    return value if isinstance(value, dict) else {}


def _text(value, limit=4000):
    return value.replace("\x00", "").strip()[:limit] if isinstance(value, str) else ""


def _count(value):
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,12}", value):
        return int(value)
    return 0


def _movie_id(value):
    result = _count(value)
    return result if result else None


def _url(value):
    value = _text(value)
    if not value:
        return ""
    try:
        p = urlsplit(value)
        if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
            return ""
        host = p.hostname.lower()
        if p.port not in (None, 80, 443):
            return ""
        path = p.path or "/"
        if host in {"iqiyi.com", "www.iqiyi.com", "m.iqiyi.com"} and re.fullmatch(r"/[vw]_[A-Za-z0-9]+\.html/?", path):
            return "https://www.iqiyi.com" + path.rstrip("/")
        if host in {"v.youku.com", "m.youku.com"} and re.fullmatch(r"/(?:v_show|video)/id_[A-Za-z0-9=_-]+\.html", path):
            return "https://v.youku.com/v_show/" + path.rsplit("/", 1)[-1]
        pairs = [(key, val) for key, val in parse_qsl(p.query, keep_blank_values=True)
                 if not key.lower().startswith("utm_") and key.lower() not in {"spm_id_from", "vd_source", "feature"}]
        if host in {"bilibili.com", "www.bilibili.com", "m.bilibili.com"} and re.fullmatch(r"/video/(?:BV[A-Za-z0-9]+|av[0-9]+)/?", path):
            pages = [(key, val) for key, val in pairs if key == "p" and val not in {"", "1"}]
            return "https://www.bilibili.com" + path.rstrip("/") + ("?" + urlencode(pages) if pages else "")
        return urlunsplit((p.scheme.lower(), host, path, urlencode(sorted(pairs)), ""))
    except ValueError:
        return ""


def _time(value):
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return 0


def _site(url):
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    for site in ("bilibili", "youku", "iqiyi"):
        if host == site + ".com" or host.endswith("." + site + ".com"):
            return site
    return ""


def summarize_learning(online_jobs, learning_jobs, candidates=(), *, movies=None,
                       skipped=(), recent_limit=10):
    """Return deduplicated current published outcomes and recent skip reasons.

    Inputs are manager list_jobs/list_candidates snapshots. ``movies`` is
    dataset.movies(root), a movie-id mapping, or an iterable of current ids.
    ``None`` means source existence was not checked. ``skipped`` accepts rows
    classified by the caller (including historical failed-6004 records); this
    service never guesses that an arbitrary download error is access-related.

    A pending/failed job contributes only its earlier explicit published_result.
    Legacy completed/partial jobs use their persisted counts. For each connected
    movie/source/digest identity, only the latest valid publication is counted.
    Validation movies count as analysed videos but not as generation references.
    Counts survive reopen/retry via durable queue data, not a process counter.
    """
    online = [r for r in (online_jobs or ()) if isinstance(r, dict)]
    learning = [r for r in (learning_jobs or ()) if isinstance(r, dict)]
    discovered = [r for r in (candidates or ()) if isinstance(r, dict)]
    supplied_skips = [r for r in (skipped or ()) if isinstance(r, dict)]
    catalogue = {}
    if movies is not None:
        entries = ([dict(_dict(value), id=key) for key, value in movies.items()]
                   if isinstance(movies, dict) else movies)
        for movie in entries:
            movie = movie if isinstance(movie, dict) else {"id": movie}
            identifier = _movie_id(movie.get("id"))
            if identifier is not None:
                catalogue[identifier] = movie

    # Union aliases so retries at another quality and identical local sources
    # cannot inflate cumulative counts, even when different movie ids exist.
    parents = {}

    def find(key):
        parents.setdefault(key, key)
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    def join(keys):
        keys = [key for key in keys if key]
        if not keys:
            return ""
        root = find(keys[0])
        for key in keys[1:]:
            parents[find(key)] = root
        return root

    def aliases(row, movie=None):
        movie = movie or _movie_id(row.get("movie_id")) or _movie_id(_dict(row.get("result")).get("movie_id"))
        keys = [f"movie:{movie}"] if movie else []
        for value in (row.get("url"), _dict(row.get("result")).get("source_url")):
            canonical = _url(value)
            if canonical:
                keys.append("url:" + canonical)
        return keys

    online_by_id, online_rows = {}, {}
    for row in online:
        keys = aliases(row)
        online_by_id[str(row.get("id", ""))] = keys
        online_rows[str(row.get("id", ""))] = row
        join(keys)
    for movie_id, movie in catalogue.items():
        keys = [f"movie:{movie_id}"]
        digest = _text(movie.get("digest"), 256)
        if digest:
            keys.append("digest:" + digest)
        if _url(movie.get("source_url")):
            keys.append("url:" + _url(movie["source_url"]))
        join(keys)
    for row in [*discovered, *learning, *supplied_skips]:
        join(aliases(row) + online_by_id.get(str(row.get("online_job_id", "")), []))
    evaluation_keys = {find(f"movie:{movie_id}") for movie_id, movie in catalogue.items()
                       if movie.get("mode") == "val"}

    results, stale = {}, set()
    for job in learning:
        movie_id = _movie_id(job.get("movie_id"))
        if not movie_id:
            continue
        published = job.get("published_result")
        outcome = published if isinstance(published, dict) else job
        status = outcome.get("status")
        if status not in ("completed", "partial"):
            continue
        understanding = _dict(outcome.get("model_analysis"))
        subtitle_count = _count(outcome.get("example_count"))
        clip_count = (_count(understanding.get("visual_clip_count"))
                      if understanding.get("visual_status") == "completed" else 0)
        frame_count = _count(understanding.get("visual_frame_count")) if clip_count else 0
        if not (subtitle_count or clip_count):
            continue
        key = find(f"movie:{movie_id}")
        current = catalogue.get(movie_id, {})
        old_hash = _text(outcome.get("film_hash"), 256)
        current_hash = _text(current.get("digest"), 256)
        if movies is not None and (movie_id not in catalogue or
                (old_hash and current_hash and old_hash != current_hash)):
            stale.add(key)
            continue
        verified = bool(movies is not None and old_hash and current_hash and old_hash == current_hash)
        mode = current.get("mode", outcome.get("mode", ""))
        timestamp = outcome.get("published_at") or job.get("updated") or job.get("created")
        row = {"status": status, "subtitle": subtitle_count, "clips": clip_count, "frames": frame_count,
               "evaluation": mode == "val" or key in evaluation_keys, "verified": verified,
               "rank": (_time(timestamp), status == "completed", subtitle_count + clip_count,
                        str(job.get("id", "")))}
        if key not in results or row["rank"] > results[key]["rank"]:
            results[key] = row

    # A skip can occur in several persisted views of the same task. Keep one
    # latest explanation per source, retaining the actual discovery URL.
    skips = {}
    skip_rows = [(r, False) for r in [*online, *discovered] if r.get("status", r.get("state")) == "skipped"]
    skip_rows.extend(({**row["last_skip"], "id": row.get("id"), "discovery": row.get("discovery", {})}, True)
                     for row in online if isinstance(row.get("last_skip"), dict))
    skip_rows.extend((r, True) for r in supplied_skips)
    for row, classified in skip_rows:
        parent = online_rows.get(str(row.get("online_job_id", "")), {})
        provenance = _dict(row.get("discovery")) or _dict(parent.get("discovery"))
        url = _text(row.get("url"))
        keys = aliases(row) + online_by_id.get(str(row.get("online_job_id", "")), [])
        key = find(keys[0]) if keys else ""
        if not key:
            identifier = _text(row.get("id"), 200)
            if not identifier:
                continue
            key = "skip:" + identifier
        reason = next((_text(row.get(field), 2000) for field in
                       ("reason", "skip_reason", "error", "message", "detail") if _text(row.get(field), 2000)), "")
        if not reason:
            reason = "该来源受限，已跳过"
        timestamp = _text(row.get("timestamp") or row.get("updated") or row.get("created"), 100)
        detail = {"reason": reason, "title": _text(provenance.get("title") or row.get("title"), 500),
                  "url": url, "source_url": _text(row.get("source_url") or provenance.get("source_url") or url),
                  "site": _text(row.get("site"), 30) or _site(url), "timestamp": timestamp}
        rank = (_time(timestamp), bool(classified), bool(detail["source_url"] != url), reason)
        if key not in skips or rank > skips[key][0]:
            skips[key] = (rank, detail)
    ordered_skips = [entry[1] for entry in sorted(skips.values(), key=lambda entry: entry[0], reverse=True)]
    recent_limit = min(100, max(0, _count(recent_limit)))
    outcomes = list(results.values())
    references = [row for row in outcomes if not row["evaluation"]]
    evaluations = sum(row["evaluation"] for row in outcomes)
    unverified = sum(not row["verified"] for row in outcomes)
    validation = ("unchecked" if movies is None else "existence-only" if unverified else "verified")
    messages = []
    if stale:
        messages.append(f"已排除 {len(stale)} 项来源已删除或变化的旧成果")
    if unverified:
        messages.append(f"{unverified} 项历史摘要未核验源指纹" if movies is not None else "尚未核验当前影片来源")
    if evaluations:
        messages.append(f"含 {evaluations} 部仅评估影片，其字幕与画面不计生成参考")
    return {
        "ready": True, "video_count": len(outcomes),
        "completed_count": sum(row["status"] == "completed" for row in outcomes),
        "partial_count": sum(row["status"] == "partial" for row in outcomes),
        "subtitle_reference_count": sum(row["subtitle"] for row in references),
        "visual_clip_count": sum(row["clips"] for row in references),
        "visual_frame_count": sum(row["frames"] for row in references),
        "skipped_count": len(ordered_skips), "latest_skip": ordered_skips[0] if ordered_skips else None,
        "recent_skips": ordered_skips[:recent_limit], "evaluation_count": evaluations,
        "stale_count": len(stale), "source_validation": validation, "message": "；".join(messages),
    }
