"""Compatibility adapter for public iQIYI videos identified by search metadata.

It reuses yt-dlp's existing public stream request.  It never supplies login
cookies, fetches keys, rewrites access flags or downloads media segments.
Every returned format has a bounded, unencrypted VOD playlist checked first.
"""
from __future__ import annotations

import math
import re
from urllib.parse import urlsplit

MAX_MANIFEST_BYTES = 512 * 1024


class IqiyiPublicVideoError(ValueError):
    pass


def _canonical_url(url) -> str:
    if not isinstance(url, str) or len(url) > 4096 or any(ord(ch) < 32 for ch in url):
        return ""
    try:
        p = urlsplit(url)
        if (p.scheme != "https" or p.hostname != "www.iqiyi.com" or p.username or p.password
                or p.port is not None or p.query or p.fragment
                or not re.fullmatch(r"/[vw]_[A-Za-z0-9]+\.html", p.path)):
            return ""
        return "https://www.iqiyi.com" + p.path
    except ValueError:
        return ""


def _seconds(value) -> float:
    if isinstance(value, bool):
        raise ValueError("无效的视频时长")
    try:
        duration = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("无效的视频时长") from exc
    if not math.isfinite(duration) or not 0 < duration <= 86400:
        raise ValueError("无效的视频时长")
    return duration


def clean_video_metadata(url: str, metadata: dict) -> dict:
    """Validate the exact canonical source URL and copy only public identifiers."""
    if _canonical_url(url) != url or not url or not isinstance(metadata, dict):
        raise IqiyiPublicVideoError("爱奇艺公开播放信息无效")
    if metadata.get("site") != "iqiyi" or metadata.get("url") != url:
        raise IqiyiPublicVideoError("爱奇艺播放信息与视频地址不匹配")
    tvid, vid = metadata.get("tvid"), metadata.get("vid")
    if isinstance(tvid, bool) or not isinstance(tvid, (str, int)) or not re.fullmatch(r"[0-9]{1,20}", str(tvid)):
        raise IqiyiPublicVideoError("爱奇艺 tvid 无效")
    if not isinstance(vid, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", vid):
        raise IqiyiPublicVideoError("爱奇艺 vid 无效")
    title = metadata.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 500 or any(ord(ch) < 32 for ch in title):
        raise IqiyiPublicVideoError("爱奇艺标题无效")
    try:
        duration = _seconds(metadata.get("duration"))
    except ValueError as exc:
        raise IqiyiPublicVideoError(str(exc)) from exc
    return {"site": "iqiyi", "url": url, "tvid": str(tvid), "vid": vid.lower(),
            "duration": duration, "title": title.strip()}


def _active(value) -> bool:
    return value not in (None, False, 0, "", "0", "false", "False", [], {})


def _restricted(data: dict) -> bool:
    for key in ("bossStatus", "prv", "previewType", "previewTime", "isPreview", "is_trial",
                "is_vip", "is_pay", "vipTypes", "hdcp", "drm", "has_drm", "is_drm"):
        if _active(data.get(key)):
            return True
    # The observed public tmts response uses drmType=1 even for a clear HLS
    # playlist.  That value alone does not establish DRM.  Such a stream still
    # must pass the no-KEY manifest check below; unknown DRM types are refused.
    if data.get("drmType") not in (None, "", 0, "0", 1, "1"):
        return True
    if data.get("dr") not in (None, "", -1, "-1", 0, "0"):
        return True
    return False


def _stream_url(url) -> str:
    if not isinstance(url, str) or len(url) > 16384 or any(ord(ch) < 32 for ch in url):
        return ""
    try:
        p = urlsplit(url)
        host = p.hostname or ""
        if (p.scheme not in {"http", "https"} or p.username or p.password
                or p.port not in {None, 80, 443}
                or not (host.endswith(".iqiyi.com") or host.endswith(".qiyi.com"))):
            return ""
        return url
    except ValueError:
        return ""


def _verify_clear_manifest(ydl, url: str) -> None:
    # Fetch just a bounded playlist, never its media or an encryption key.
    with ydl.urlopen(url) as response:
        final_url = getattr(response, "url", url)
        if not _stream_url(final_url):
            raise IqiyiPublicVideoError("爱奇艺播放清单跳转到不支持的地址")
        body = response.read(MAX_MANIFEST_BYTES + 1)
    if len(body) > MAX_MANIFEST_BYTES:
        raise IqiyiPublicVideoError("爱奇艺播放清单过大")
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise IqiyiPublicVideoError("爱奇艺播放清单不是有效文本") from exc
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines[0] != "#EXTM3U" or "#EXT-X-ENDLIST" not in lines:
        raise IqiyiPublicVideoError("爱奇艺播放清单不是完整点播视频")
    if any(line.upper().startswith(("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:"))
           and not re.search(r"(?:^|[:,])\s*METHOD=NONE(?:,|$)", line.upper()) for line in lines):
        raise IqiyiPublicVideoError("爱奇艺播放清单包含加密或 DRM，仅支持公开无加密视频")
    if any(line.startswith("#EXT-X-STREAM-INF:") for line in lines):
        raise IqiyiPublicVideoError("爱奇艺返回未解析的主播放清单")
    if not any(line.startswith("#EXTINF:") for line in lines):
        raise IqiyiPublicVideoError("爱奇艺播放清单没有视频片段")


def extract_public_video(ydl, url: str, metadata: dict) -> dict:
    """Return a standard yt-dlp info dictionary for verified clear public HLS."""
    hint = clean_video_metadata(url, metadata)
    from yt_dlp.extractor.iqiyi import IqiyiIE
    raw = IqiyiIE(ydl).get_raw_data(hint["tvid"], hint["vid"])
    if not isinstance(raw, dict) or raw.get("code") != "A00000":
        code = raw.get("code", "missing") if isinstance(raw, dict) else "invalid"
        raise IqiyiPublicVideoError(f"爱奇艺公开播放接口返回 {code}")
    data = raw.get("data")
    if not isinstance(data, dict) or data.get("bossStatus") not in (0, "0"):
        raise IqiyiPublicVideoError("爱奇艺视频需要会员或播放权限")
    if str(data.get("tvid")) != hint["tvid"] or str(data.get("vid", "")).lower() != hint["vid"]:
        raise IqiyiPublicVideoError("爱奇艺返回的播放标识不匹配")
    if _restricted(data):
        raise IqiyiPublicVideoError("爱奇艺视频受会员、试看或 DRM 限制")
    duration = _seconds(data.get("duration"))
    if duration + max(2.0, hint["duration"] * .05) < hint["duration"]:
        raise IqiyiPublicVideoError("爱奇艺返回时长短于检索记录，可能是试看内容")
    streams = data.get("vidl")
    if not isinstance(streams, list):
        raise IqiyiPublicVideoError("爱奇艺没有返回公开播放格式")
    formats, seen, rejected = [], set(), []
    for stream in streams[:30]:
        if not isinstance(stream, dict) or _restricted(stream):
            continue
        stream_url = _stream_url(stream.get("m3utx"))
        if not stream_url or stream_url in seen:
            continue
        try:
            _verify_clear_manifest(ydl, stream_url)
        except Exception as exc:
            rejected.append(str(exc))
            continue
        vd = str(stream.get("vd", "unknown"))
        fmt = {"url": stream_url, "format_id": vd, "ext": "mp4", "protocol": "m3u8_native",
               "quality": IqiyiIE._FORMATS_MAP.get(vd, -1), "has_drm": False}
        dimensions = re.fullmatch(r"([0-9]{1,5})x([0-9]{1,5})", str(stream.get("screenSize", "")))
        if dimensions and all(0 < int(value) <= 16384 for value in dimensions.groups()):
            fmt.update(width=int(dimensions[1]), height=int(dimensions[2]))
        if str(stream.get("fileFormat", "")).upper() == "H265":
            fmt["vcodec"] = "hevc"
        formats.append(fmt)
        seen.add(stream_url)
    if not formats:
        detail = "; ".join(rejected[:2]) or "没有公开无加密格式"
        raise IqiyiPublicVideoError(f"爱奇艺视频无法直接下载：{detail}")
    return {"id": hint["vid"], "display_id": hint["tvid"], "title": hint["title"],
            "duration": duration, "webpage_url": url, "extractor_key": "Iqiyi", "extractor": "iqiyi",
            "is_live": False, "formats": formats}
