"""Reviewed film context, with attributable public metadata and manual fallback.

Network results are suggestions. Only an explicit user confirmation makes them
eligible for training; actor names never silently become character names.
"""
from datetime import datetime, timezone
import html
import json
import re
import sqlite3
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from dataset import connect


VERSION = 1
USER_AGENT = "YinggeFilmStudy/1.0 (desktop film metadata reader; local review required)"
WIKIDATA = "https://www.wikidata.org/w/api.php"
LANGUAGES = "zh-hans|zh|zh-hant|en"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema(root):
    with connect(root) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS movie_profiles (
            movie INTEGER PRIMARY KEY REFERENCES movies(id), data TEXT NOT NULL,
            updated_at TEXT NOT NULL)""")


def _empty(movie_id, title=""):
    return {"version": VERSION, "movie_id": movie_id, "title": title, "year": "",
            "cast": [], "synopsis": "", "events": [], "sources": [],
            "status": "draft", "allow_missing": False, "research": {}, "updated_at": ""}


def snapshot_from_db(db, movie_id):
    """Read within the caller's snapshot transaction, without changing schema."""
    movie = db.execute("SELECT name FROM movies WHERE id=?", (movie_id,)).fetchone()
    if movie is None:
        raise ValueError("电影已不存在，请重新选择")
    profile = _empty(movie_id, movie[0])
    try:
        row = db.execute("SELECT data FROM movie_profiles WHERE movie=?", (movie_id,)).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return profile
    if row:
        profile.update(json.loads(row[0]))
    return profile


def get(root, movie_id):
    with connect(root) as db:
        return snapshot_from_db(db, movie_id)


def is_ready(profile):
    return isinstance(profile, dict) and profile.get("status") == "confirmed"


def required_missing(root, movie_ids):
    with connect(root) as db:
        ids = ([row[0] for row in db.execute("SELECT id FROM movies ORDER BY id")]
               if movie_ids is None else list(dict.fromkeys(movie_ids)))
        return [movie_id for movie_id in ids if not is_ready(snapshot_from_db(db, movie_id))]


def _text(value, maximum, name):
    value = str(value or "").strip().replace("\x00", "")
    if len(value) > maximum:
        raise ValueError(f"{name}最多 {maximum} 个字符")
    return value


def clean_profile(profile, movie_id):
    result = _empty(movie_id)
    result["title"] = _text(profile.get("title"), 300, "电影标题")
    if not result["title"]:
        raise ValueError("请填写电影标题")
    year = _text(profile.get("year"), 4, "年份")
    if year and (not year.isdigit() or not 1800 <= int(year) <= 2200):
        raise ValueError("年份请填四位数字，未知可留空")
    result["year"] = year
    cast = profile.get("cast") or []
    if not isinstance(cast, list) or len(cast) > 1000:
        raise ValueError("演员与角色表格式无效，最多 1000 行")
    seen = set()
    for item in cast:
        if not isinstance(item, dict):
            raise ValueError("演员与角色表格式无效")
        role, actor = (_text(item.get(key), 128, label) for key, label in (("role", "角色名"), ("actor", "演员名")))
        aliases = item.get("aliases") or []
        if not isinstance(aliases, list):
            raise ValueError("角色别名需为列表")
        aliases = list(dict.fromkeys(_text(alias, 128, "角色别名") for alias in aliases if str(alias).strip()))
        if not role and aliases:
            raise ValueError("填写角色别名前请先填写角色名")
        if (role or actor) and (role, actor) not in seen:
            result["cast"].append({"role": role, "actor": actor, "aliases": aliases})
            seen.add((role, actor))
    result["synopsis"] = _text(profile.get("synopsis"), 40000, "剧情梗概")
    events = profile.get("events") or []
    if not isinstance(events, list) or len(events) > 2000:
        raise ValueError("剧情时间段格式无效，最多 2000 条")
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("剧情时间段格式无效")
        start, end = event.get("start_ms"), event.get("end_ms")
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= 604800000:
            raise ValueError("剧情时间段须满足 0 ≤ 开始 < 结束，最长 7 天")
        text = _text(event.get("text"), 10000, "片段剧情")
        if not text:
            raise ValueError("请填写时间段对应的剧情")
        roles = event.get("roles") or []
        if not isinstance(roles, list):
            raise ValueError("片段角色格式无效")
        result["events"].append({"start_ms": start, "end_ms": end, "text": text,
                                 "roles": list(dict.fromkeys(_text(role, 128, "片段角色") for role in roles if str(role).strip()))})
    result["events"].sort(key=lambda event: (event["start_ms"], event["end_ms"]))
    sources = profile.get("sources") or []
    if not isinstance(sources, list) or len(sources) > 100:
        raise ValueError("来源列表格式无效，最多 100 个来源")
    urls = set()
    for source in sources:
        if isinstance(source, str):
            source = {"url": source}
        if not isinstance(source, dict):
            raise ValueError("资料来源格式无效")
        url = _text(source.get("url"), 3000, "来源网址")
        if not url:
            continue
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            raise ValueError("资料来源请填写 HTTP/HTTPS 网页链接")
        if url not in urls:
            result["sources"].append({"url": url, "title": _text(source.get("title"), 300, "来源标题"),
                                       "license": _text(source.get("license"), 200, "来源许可")})
            urls.add(url)
    if isinstance(profile.get("research"), dict):
        result["research"] = {key: _text(profile["research"].get(key), 2000, "检索记录")
                              for key in ("attempted_at", "query", "status", "message")}
    return result


def missing_fields(profile):
    missing = []
    if not any(item.get("actor") for item in profile.get("cast", [])):
        missing.append("演员表")
    if not any(item.get("role") for item in profile.get("cast", [])):
        missing.append("角色表")
    if not profile.get("synopsis", "").strip():
        missing.append("剧情梗概")
    return missing


def save(root, movie_id, profile, *, confirm=False, allow_missing=False):
    result = clean_profile(profile, movie_id)
    missing = missing_fields(result)
    if confirm and missing and not allow_missing:
        raise ValueError("尚缺" + "、".join(missing) + "；请补充，或明确勾选暂缺资料继续")
    result["allow_missing"] = bool(confirm and allow_missing and missing)
    result["status"] = ("confirmed" if confirm else "manual-needed" if result["research"].get("status")
                        in {"failed", "not-found"} else "draft")
    result["updated_at"] = _now()
    ensure_schema(root)
    with connect(root) as db:
        if not db.execute("SELECT 1 FROM movies WHERE id=?", (movie_id,)).fetchone():
            raise ValueError("电影已不存在，请重新选择")
        db.execute("INSERT OR REPLACE INTO movie_profiles VALUES(?,?,?)",
                   (movie_id, json.dumps(result, ensure_ascii=False, allow_nan=False), result["updated_at"]))
        if confirm:
            position = db.execute("SELECT COALESCE(MAX(position),-1)+1 FROM movie_cast WHERE movie=?", (movie_id,)).fetchone()[0]
            for item in result["cast"]:
                if item["role"]:
                    cursor = db.execute("INSERT OR IGNORE INTO movie_cast(movie,name,position) VALUES(?,?,?)",
                                        (movie_id, item["role"], position))
                    position += cursor.rowcount
    return result


def record_research(root, movie_id, result):
    """Record a search attempt without clobbering existing human-written fields."""
    profile = get(root, movie_id)
    profile["research"] = {"attempted_at": _now(), "query": result.get("query", profile["title"]),
                            "status": result.get("status", "failed"), "message": result.get("message", "")}
    return save(root, movie_id, profile, confirm=is_ready(profile), allow_missing=profile.get("allow_missing", False))


def parse_cast(text):
    rows = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        parts = [part.strip() for part in line.replace("｜", "|").split("|")]
        if len(parts) > 3:
            raise ValueError(f"角色表第 {number} 行格式应为：角色 | 演员 | 别名")
        parts += [""] * (3 - len(parts))
        rows.append({"role": parts[0], "actor": parts[1],
                     "aliases": [alias.strip() for alias in re.split("[,，、;；]", parts[2]) if alias.strip()]})
    return rows


def format_cast(rows):
    return "\n".join(" | ".join((row.get("role", ""), row.get("actor", ""), "、".join(row.get("aliases", [])))).rstrip(" |") for row in rows)


def _milliseconds(value):
    parts = value.strip().split(":")
    if not 1 <= len(parts) <= 3 or any(not re.fullmatch(r"\d+(?:\.\d+)?", part) for part in parts):
        raise ValueError("时间请使用秒数或 时:分:秒")
    total = 0.
    for part in parts:
        total = total * 60 + float(part)
    return round(total * 1000)


def parse_events(text):
    events = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        parts = [part.strip() for part in line.replace("｜", "|").split("|")]
        if len(parts) not in {2, 3}:
            raise ValueError(f"剧情时间段第 {number} 行应为：开始-结束 | 剧情 | 角色")
        times = re.split(r"\s*[-–—]\s*", parts[0])
        if len(times) != 2:
            raise ValueError(f"剧情时间段第 {number} 行缺少开始或结束时间")
        events.append({"start_ms": _milliseconds(times[0]), "end_ms": _milliseconds(times[1]), "text": parts[1],
                       "roles": [role.strip() for role in re.split("[,，、;；]", parts[2] if len(parts)>2 else "") if role.strip()]})
    return events


def format_events(events):
    return "\n".join(f"{event['start_ms']/1000:g}-{event['end_ms']/1000:g} | {event['text']} | "
                     + "、".join(event.get("roles", [])) for event in events)


def search_url(title, year=""):
    return "https://www.bing.com/search?" + urlencode({"q": f"{title} {year} 电影 演员 角色 剧情".strip()})


def _api(url, params, *, timeout=8):
    request = Request(url + "?" + urlencode({**params, "format": "json", "formatversion": 2}),
                      headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        raw = response.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("在线资料响应过大，请手动查询")
    result = json.loads(raw)
    if not isinstance(result, dict) or result.get("error"):
        raise ValueError("在线资料服务暂时无法响应，请手动查询或稍后重试")
    return result


def search_online(title, year="", *, api=None):
    """Return alternatives; never auto-select a film based on title alone."""
    api = api or _api
    query = _text(title, 300, "查询标题")
    if not query:
        raise ValueError("请先填写要查询的电影标题")
    errors, candidates, seen = [], [], set()
    for language in ("zh", "en"):
        try:
            response = api(WIKIDATA, {"action": "wbsearchentities", "search": query,
                                     "language": language, "uselang": "zh", "type": "item", "limit": 8})
            for row in response.get("search", []):
                entity = row.get("id", "")
                if not re.fullmatch(r"Q\d+", entity) or entity in seen:
                    continue
                seen.add(entity)
                candidates.append({"id": entity, "title": row.get("label") or entity,
                                   "description": row.get("description", ""),
                                   "url": "https://www.wikidata.org/wiki/" + entity})
            if candidates:
                break
        except Exception as exc:
            errors.append(str(exc))
    if year:
        candidates.sort(key=lambda row: str(year) not in row["description"])
    status = "found" if candidates else "failed" if errors else "not-found"
    message = (f"找到 {len(candidates)} 个候选，请核对片名、年份及说明后选择。" if candidates else
               "在线资料服务暂时不可用，请点击浏览器查询并手动填写。" if errors else
               "没有找到对应资料；可调整片名，或点击浏览器查询后手动填写。")
    return {"query": query, "status": status, "candidates": candidates, "message": message,
            "errors": errors, "search_url": search_url(query, year)}


def _label(entity):
    labels = entity.get("labels", {})
    return next((labels[language].get("value", "") for language in LANGUAGES.split("|") if language in labels), "")


def _value(snak):
    return (snak.get("datavalue") or {}).get("value")


def _plot(text):
    sections = re.split(r"(?m)^\s*==+\s*(.*?)\s*==+\s*$", str(text or ""))
    for index in range(1, len(sections)-1, 2):
        if re.search(r"剧情|劇情|故事|情节|情節|plot|synopsis", sections[index], re.I):
            return sections[index+1].strip()[:12000]
    return sections[0].strip()[:4000]


def fetch_candidate(candidate, *, api=None):
    """Fetch public claims and attributable article text for explicit review."""
    api = api or _api
    entity_id = candidate.get("id", "")
    if not re.fullmatch(r"Q\d+", entity_id):
        raise ValueError("请选择有效的电影候选")
    response = api(WIKIDATA, {"action": "wbgetentities", "ids": entity_id,
                             "props": "labels|claims|sitelinks", "languages": LANGUAGES})
    entity = response.get("entities", {}).get(entity_id, {})
    if not entity or "missing" in entity:
        raise ValueError("该电影条目已不存在，请重新查询")
    profile = _empty(None, _label(entity) or candidate.get("title", entity_id))
    profile["sources"] = [{"url": "https://www.wikidata.org/wiki/"+entity_id,
                            "title": "Wikidata · "+profile["title"], "license": "CC0"}]
    claims = entity.get("claims", {})
    for claim in claims.get("P577", []):
        value = _value(claim.get("mainsnak", {}))
        if isinstance(value, dict):
            match = re.match(r"\+(\d{4})-", value.get("time", ""))
            if match:
                profile["year"] = match[1]
                break
    cast_rows, labels_needed = [], set()
    for claim in claims.get("P161", [])[:300]:
        if claim.get("rank") == "deprecated":
            continue
        actor = _value(claim.get("mainsnak", {}))
        if not isinstance(actor, dict) or not re.fullmatch(r"Q\d+", actor.get("id", "")):
            continue
        actor_id = actor["id"]
        labels_needed.add(actor_id)
        qualifiers = claim.get("qualifiers", {})
        names = [value for snak in qualifiers.get("P4633", []) if isinstance(value := _value(snak), str)]
        roles = [value["id"] for snak in qualifiers.get("P453", [])
                 if isinstance(value := _value(snak), dict) and re.fullmatch(r"Q\d+", value.get("id", ""))]
        labels_needed.update(roles)
        cast_rows.append((actor_id, names, roles))
    entities, warnings = {}, []
    for offset in range(0, len(labels_needed), 50):
        ids = sorted(labels_needed)[offset:offset+50]
        try:
            entities.update(api(WIKIDATA, {"action": "wbgetentities", "ids": "|".join(ids),
                                           "props": "labels|aliases", "languages": LANGUAGES}).get("entities", {}))
        except Exception:
            warnings.append("部分演员或角色名称未能读取，请手动补充。")
    for actor_id, names, role_ids in cast_rows:
        actor = _label(entities.get(actor_id, {}))
        role_names = names or [_label(entities.get(role_id, {})) for role_id in role_ids]
        for role in role_names or [""]:
            if role or actor:
                profile["cast"].append({"role": role, "actor": actor, "aliases": []})
    for language in ("zh", "en"):
        sitelink = entity.get("sitelinks", {}).get(language+"wiki", {})
        title = sitelink.get("title")
        if not title:
            continue
        try:
            article = api(f"https://{language}.wikipedia.org/w/api.php", {
                "action": "query", "prop": "extracts", "titles": title, "redirects": 1,
                "explaintext": 1, "exchars": 24000})
            pages = article.get("query", {}).get("pages", [])
            if isinstance(pages, dict):
                pages = pages.values()
            page = next((page for page in pages if page.get("extract")), {})
            synopsis = _plot(page.get("extract", ""))
            if synopsis:
                profile["synopsis"] = html.unescape(synopsis)
                profile["sources"].append({"url": f"https://{language}.wikipedia.org/wiki/"+quote(title.replace(" ", "_")),
                                            "title": "Wikipedia · "+title, "license": "CC BY-SA 4.0"})
                break
        except Exception:
            warnings.append("百科剧情暂未读取成功，可打开来源网页后补充。")
    if not profile["synopsis"]:
        warnings.append("条目没有可读取的剧情说明，请手动补充。")
    profile["research"] = {"attempted_at": _now(), "query": candidate.get("title", ""), "status": "selected",
                            "message": "；".join(dict.fromkeys(warnings)) or "资料已读取，请核对后确认用于学习。"}
    return profile
