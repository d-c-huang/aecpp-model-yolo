"""Read iQIYI's public search JSON without executing page scripts.

The public mobile search frontend uses search.video.iqiyi.com/o.  Desktop
search currently returns an application shell, so scraping its anchors alone
cannot discover videos.  This module only constructs that public search URL
and parses its metadata; networking, cancellation and progress stay with the
discovery controller.  A result is not proof that its media can be downloaded.
"""
from __future__ import annotations

import html
import json
import math
import re
from urllib.parse import urlencode, urlsplit


MAX_RESPONSE_BYTES = 3 * 1024 * 1024
MAX_RESULTS = 400


class IqiyiSearchError(ValueError):
    """An explicit public-search status, including invalid query A00004."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"爱奇艺公开搜索返回 {code}：{message}")


def iqiyi_search_url(query: str, *, page: int = 1, page_size: int = 20) -> str:
    """Return the URL displayed as the actual source of a public search."""
    query = str(query).strip()
    if not query or len(query) > 200 or any(ord(ch) < 32 for ch in query):
        raise ValueError("爱奇艺搜索词必须为 1–200 个字符")
    if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 100:
        raise ValueError("爱奇艺搜索页码必须为 1–100")
    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 20:
        raise ValueError("爱奇艺每页搜索条数必须为 1–20")
    return "https://search.video.iqiyi.com/o?" + urlencode({
        "if": "html5", "key": query, "pageNum": page, "pageSize": page_size,
    })


def _plain(value, limit: int = 1000) -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    value = html.unescape(re.sub(r"<[^>]*>", "", str(value)))
    return " ".join(value.split())[:limit]


def _video_url(value) -> str:
    if not isinstance(value, str) or len(value) > 4096 or any(ord(ch) < 32 for ch in value):
        return ""
    value = html.unescape(value.strip())
    if value.startswith("//"):
        value = "https:" + value
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"}
                or parsed.hostname not in {"iqiyi.com", "www.iqiyi.com", "m.iqiyi.com"}
                or parsed.username or parsed.password or parsed.port not in {None, 80, 443}):
            return ""
        if not re.fullmatch(r"/[vw]_[A-Za-z0-9]+\.html/?", parsed.path):
            return ""
        return "https://www.iqiyi.com" + parsed.path.rstrip("/")
    except ValueError:
        return ""


def _flag(value) -> bool:
    return value is True or value == 1 or (isinstance(value, str) and value.lower() in {"1", "true"})


def _duration(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and 0 < result <= 86400 else None
    except (TypeError, ValueError, OverflowError):
        return None


def parse_iqiyi_search(body: str | bytes) -> list[dict]:
    """Extract direct episode/play URLs plus public duration/access metadata.

    Returns dictionaries containing url, title, description, duration_seconds,
    is_vip and is_paid, plus optional video_metadata with validated public
    identifiers for unrestricted candidates.  Outer status must be A00000. Non-video destinations,
    third-party results and duplicate playback URLs are omitted.  Membership
    flags are preserved for the caller to skip restricted candidates; they do
    not establish free playback when false.  No JSONP or JavaScript is run.
    """
    if not isinstance(body, (str, bytes)):
        raise ValueError("爱奇艺搜索响应不是文本")
    size = len(body) if isinstance(body, bytes) else len(body.encode("utf-8"))
    if size > MAX_RESPONSE_BYTES:
        raise ValueError("爱奇艺搜索响应超过 3 MiB")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise ValueError("爱奇艺搜索返回的不是有效 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("爱奇艺搜索响应结构无效")
    code = str(payload.get("code", "missing"))
    if code != "A00000":
        raise IqiyiSearchError(code, _plain(payload.get("data")) or "搜索失败")
    data = payload.get("data")
    # The service explicitly returns this string for a successful empty query.
    if isinstance(data, str) and data.strip().lower() == "search result is empty":
        return []
    if not isinstance(data, dict) or not isinstance(data.get("docinfos"), list):
        raise ValueError("爱奇艺搜索响应缺少 docinfos")
    if data.get("code", 0) not in (0, "0"):
        raise IqiyiSearchError(str(data["code"]), "搜索数据状态异常")

    rows: list[dict] = []
    seen: set[str] = set()
    for doc in data["docinfos"][:100]:
        if not isinstance(doc, dict):
            continue
        album = doc.get("albumDocInfo")
        if not isinstance(album, dict) or album.get("siteId") not in (None, "", "iqiyi", "pps"):
            continue
        videos = album.get("videoinfos")
        videos = [v for v in videos[:100] if isinstance(v, dict)] if isinstance(videos, list) else []
        # Search can also return an individual video with no episode list.
        if not videos:
            videos = [{}]
        for video in videos:
            url = _video_url(video.get("itemLink") or album.get("albumLink"))
            title = _plain(video.get("itemTitle") or album.get("albumTitle"), 200)
            if not url or not title or url in seen:
                continue
            description = " ".join(filter(None, (
                _plain(album.get("albumTitle"), 200), _plain(album.get("channel"), 100),
                _plain(album.get("three_category_v2") or album.get("threeCategory"), 300),
                _plain(album.get("tvFocus") or album.get("description"), 500),
                _plain(video.get("subTitle"), 200),
            )))[:1000]
            # Explicit episode flags take precedence: a series may mix free
            # episodes and member episodes.  Album-level purchase metadata is
            # only a conservative fallback for an unknown episode flag.
            vip = (_flag(video["is_vip"]) if "is_vip" in video else
                   str(album.get("isPurchase", "0")) not in {"0", "", "None"})
            paid = _flag(video.get("is_pay", album.get("is_pay", False)))
            row = {
                "url": url, "title": title, "description": description,
                "duration_seconds": _duration(video.get("timeLength", album.get("timeLength"))),
                "is_vip": vip, "is_paid": paid,
            }
            if not vip and not paid and video.get("tvId") and video.get("vid"):
                from iqiyi_video import clean_video_metadata
                try:
                    row["video_metadata"] = clean_video_metadata(url, {
                        "site": "iqiyi", "url": url, "title": title,
                        "tvid": video["tvId"], "vid": video["vid"], "duration": row["duration_seconds"],
                    })
                except ValueError:
                    pass  # Retain the ordinary URL when auxiliary metadata is incomplete.
            rows.append(row)
            seen.add(url)
            if len(rows) >= MAX_RESULTS:
                return rows
    return rows
