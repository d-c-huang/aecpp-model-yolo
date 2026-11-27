"""Read public Youku HTML/SSR metadata without executing JavaScript.

The live desktop search can return a script-only CAPTCHA page. That is an
explicit unavailable source, not an empty successful search. Public SSR cards
use title + action(JUMP_TO_VIDEO, value) rather than ordinary href links.
"""
from collections import deque
import json
import re
from urllib.parse import parse_qs, unquote, urljoin, urlsplit


MAX_BYTES = 3*1024*1024
VIDEO_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9=_-]{2,199}\Z")
ASSIGNMENT = re.compile(r"(?:window\.)?__(?:INITIAL_DATA|PAGE_CONF|INITIAL_STATE)__\s*=\s*")
JSON_STRING_OR_UNDEFINED = re.compile(r'"(?:\\.|[^"\\])*"|\bundefined\b')


def _plain(value, limit=1000):
    if not isinstance(value, str):
        return ""
    return " ".join(re.sub(r"<[^>]*>", " ", value).split())[:limit]


def _video_id_url(value):
    if isinstance(value, str) and VIDEO_ID.fullmatch(value):
        return "https://v.youku.com/v_show/id_"+value+".html"
    return None


def _video_url(value, source_url):
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        parts = urlsplit(urljoin(source_url, value))
        if (parts.scheme not in {"http", "https"} or parts.hostname not in {"v.youku.com", "m.youku.com"}
                or parts.username or parts.password or parts.port not in {None, 80, 443}):
            return None
        path = unquote(parts.path)
        if path.rstrip("/") == "/video":
            return _video_id_url(parse_qs(parts.query).get("vid", [""])[0])
        match = re.fullmatch(r"/(?:v_show|video)/id_([A-Za-z0-9][A-Za-z0-9=_-]{2,199})\.html/?", path)
        return _video_id_url(match[1]) if match else None
    except ValueError:
        return None


def _payloads(page):
    decoder = json.JSONDecoder()
    for script in page.find_all("script")[:40]:
        content = script.get_text()
        if len(content) > MAX_BYTES:
            continue
        if script.get("type") in {"application/json", "application/ld+json"}:
            try:
                yield json.loads(content)
            except (ValueError, TypeError):
                pass
        for match in list(ASSIGNMENT.finditer(content))[:4]:
            try:
                # Public Youku SSR includes literal undefined values in ads and
                # anonymous account state. Replace only tokens outside quoted
                # JSON strings; all other JS expressions remain invalid JSON.
                raw = JSON_STRING_OR_UNDEFINED.sub(
                    lambda token: "null" if token.group() == "undefined" else token.group(),
                    content[match.end():])
                value, _ = decoder.raw_decode(raw)
                yield value
            except (ValueError, TypeError):
                pass


def parse_youku_search(body, source_url):
    """Return (single_video_url, title, description) tuples from public HTML.

    Works for the official search, home and video pages; search engine HTML is
    still parsed by the application's generic adapter. Numeric show IDs and
    JUMP_TO_SHOW targets are deliberately never converted into video URLs.
    """
    if not isinstance(body, str) or len(body.encode("utf-8")) > MAX_BYTES:
        raise ValueError("优酷公开响应超过读取上限或格式无效")
    if ("_____tmd_____/punish" in body or re.search(r'["\']action["\']\s*:\s*["\']captcha["\']', body)):
        raise ValueError("优酷站内搜索要求验证码/安全验证；本轮不会自动通过验证，将尝试其他公开来源")
    from bs4 import BeautifulSoup
    page = BeautifulSoup(body, "html.parser")
    page_title = page.title.get_text(" ", strip=True) if page.title else ""
    if any(word in page_title.casefold() for word in ("验证码", "安全验证", "captcha", "verify you are human")):
        raise ValueError("优酷来源要求验证码/安全验证；本轮不会自动通过验证")
    records = {}

    def add(url, title, description=""):
        url, title = _video_url(url, source_url), _plain(title, 200)
        if url and title and url not in records:
            records[url] = (url, title, _plain(description))

    # A public detail page may be used as a provenance source for that same
    # video, but a generic home page's title is never a fabricated video title.
    meta = {tag.get("property") or tag.get("name"): tag.get("content", "")
            for tag in page.select("meta[property],meta[name]")[:150]}
    payloads = list(_payloads(page))
    visible_text = " ".join(str(node).strip() for node in page.find_all(string=True)
                            if node.parent.name not in {"script", "style", "title", "head", "noscript"})
    if not (payloads or visible_text.strip() or meta.get("og:title") or meta.get("title")):
        raise ValueError("优酷公开响应为空或仅包含脚本空壳，尚未取得可读取的检索内容")
    if _video_url(source_url, source_url):
        add(meta.get("og:video") or source_url, meta.get("og:title") or meta.get("title"),
            meta.get("og:description") or meta.get("description", ""))
    for payload in payloads:
        pending, visited = deque([payload]), 0
        while pending and visited < 30000:
            item = pending.popleft()
            visited += 1
            if isinstance(item, dict):
                title = item.get("title") or item.get("videoTitle") or item.get("name")
                description = item.get("description") or item.get("desc") or item.get("subTitle") or item.get("subtitle", "")
                if isinstance(title, str):
                    # Tags belong to this card (for example "电影"), never to
                    # surrounding page navigation or another recommendation.
                    tag_titles = []
                    tags = item.get("tags")
                    for tag in tags[:20] if isinstance(tags, list) else []:
                        label = tag.get("text") if isinstance(tag, dict) else None
                        if isinstance(label, dict):
                            tag_titles.append(_plain(label.get("title"), 80))
                    description = " ".join(filter(None, [_plain(description), *tag_titles]))
                    action = item.get("action")
                    if isinstance(action, dict):
                        if action.get("type") == "JUMP_TO_VIDEO":
                            add(_video_id_url(action.get("value")), title, description)
                        elif action.get("type") in {"JUMP_TO_URL", "JUMP_TO_WEB"}:
                            add(action.get("value"), title, description)
                    # These fields explicitly identify a video, unlike generic
                    # id/showId/trackInfo values which can refer to collections.
                    for key in ("videoId", "video_id", "vid"):
                        add(_video_id_url(item.get(key)), title, description)
                    preview = item.get("previewInfo")
                    if isinstance(preview, dict):
                        add(_video_id_url(preview.get("videoId")), title+"（预告片段）",
                            _plain(description)+" 优酷公开预告片段")
                    for key in ("url", "playUrl", "play_url", "videoUrl", "contentUrl", "vUrl", "h5Url"):
                        add(item.get(key), title, description)
                pending.extend(value for value in item.values() if isinstance(value, (dict, list)))
            elif isinstance(item, list):
                pending.extend(item[:2000])
    for anchor in page.select("a[href]")[:2000]:
        heading = anchor.select_one("h2, h3, .title, .c-title")
        image = anchor.find("img")
        title = (anchor.get("title") or (heading.get("title") or heading.get_text(" ", strip=True) if heading else "")
                 or (image.get("alt", "") if image else "") or anchor.get_text(" ", strip=True))
        description = anchor.get_text(" ", strip=True)[:1000]
        add(anchor.get("href"), title, description)
    return list(records.values())
