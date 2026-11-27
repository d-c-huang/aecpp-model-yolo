"""Bounded public metadata discovery; this module never downloads video files.

Results are candidates, not a promise that a host will allow a later download.
Every source request is reported so a desktop queue can show where it is looking.
"""
import base64
import html
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree


PROVIDERS = {"web": "三站公开检索"}
SITES = {"bilibili": {"label": "哔哩哔哩", "domain": "bilibili.com", "home": "https://www.bilibili.com/"},
         "youku": {"label": "优酷", "domain": "youku.com", "home": "https://www.youku.com/ku/webhome"},
         "iqiyi": {"label": "爱奇艺", "domain": "iqiyi.com", "home": "https://www.iqiyi.com/"}}
DEFAULT_PROVIDERS = {"animation": "web", "movie": "web"}
MAX_BYTES = 3 * 1024 * 1024
TIMEOUT = 8
USER_AGENT = "Mozilla/5.0 (compatible; YinggeVideoDiscovery/1.0; public video metadata)"


class DiscoveryCancelled(RuntimeError):
    pass


def _text(value, maximum=1000):
    if isinstance(value, list):
        value = " · ".join(str(item) for item in value[:20])
    value = re.sub(r"<[^>]*>", " ", str(value or ""))
    return " ".join(html.unescape(value).replace("\x00", "").split())[:maximum]


def _http_url(value):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
        return None
    try:
        parts = urlsplit(html.unescape(value.strip()))
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return None
        if parts.username is not None or parts.password is not None or parts.port not in {None, 80, 443}:
            return None
    except ValueError:
        return None
    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path or "/", parts.query, ""))


def _unwrap(value):
    """Unwrap search-engine result links without following arbitrary redirects."""
    value = html.unescape(str(value or ""))
    parts = urlsplit(urljoin("https://www.bing.com", value))
    if parts.hostname in {"www.bing.com", "cn.bing.com", "bing.com"} and parts.path == "/ck/a":
        encoded = parse_qs(parts.query).get("u", [""])[0]
        if encoded.startswith("a1"):
            try:
                payload = encoded[2:]
                value = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode("utf-8")
            except (ValueError, UnicodeError):
                return ""
    return value


def canonical_video_url(value, sites=None):
    """Only admit known single-video URLs, never software/search/playlist pages."""
    try:
        value = _http_url(_unwrap(value))
    except ValueError:
        return None
    if not value:
        return None
    parts = urlsplit(value)
    host = parts.hostname.lower()
    allowed = set(sites if sites is not None else SITES)
    if "bilibili" in allowed and host in {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}:
        match = re.fullmatch(r"/video/(BV[A-Za-z0-9]{10}|av\d+)/?", parts.path, re.I)
        if match:
            key = match[1]
            key = "BV" + key[2:] if key[:2].lower() == "bv" else "av" + key[2:]
            return "https://www.bilibili.com/video/" + key
    if "youku" in allowed and host in {"v.youku.com", "m.youku.com"}:
        if parts.path.rstrip("/") == "/video":
            identifier = parse_qs(parts.query).get("vid", [""])[0]
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9=_-]{2,199}", identifier):
                return "https://v.youku.com/v_show/id_" + identifier + ".html"
        # Public indexes percent-encode the base64 padding in Youku IDs.
        # Decode once, then apply the same strict single-video path grammar.
        match = re.fullmatch(r"/(?:v_show|video)/id_([A-Za-z0-9][A-Za-z0-9=_-]{2,199})\.html/?", unquote(parts.path))
        if match:
            return "https://v.youku.com/v_show/id_" + match[1] + ".html"
    if "iqiyi" in allowed and host in {"www.iqiyi.com", "iqiyi.com", "m.iqiyi.com"}:
        match = re.fullmatch(r"/([vw]_[A-Za-z0-9]+)\.html/?", parts.path)
        if match:
            return "https://www.iqiyi.com/" + match[1] + ".html"
    return None


class _Search:
    def __init__(self, query, category, provider, report, stop):
        self.query, self.category, self.provider = query, category, provider
        self.report, self.stop = report, stop
        self.site = ""
        self.deadline = time.monotonic() + 60

    def check(self):
        if self.stop is not None and (self.stop.is_set() if hasattr(self.stop, "is_set") else self.stop()):
            raise DiscoveryCancelled("联网发现已停止")
        if time.monotonic() >= self.deadline:
            raise TimeoutError("本轮检索已达到时间上限，可稍后重试或更换来源")

    def emit(self, phase, url, message, **values):
        self.check()
        self.report({"phase": phase, "url": url, "source_url": url, "message": message,
                     "query": self.query, "provider": self.provider, "category": self.category,
                     "site": self.site, "title": "", **values})

    def read(self, url):
        self.emit("fetch", url, "正在获取来源信息：" + urlsplit(url).hostname)
        request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json,application/rss+xml,text/html;q=0.8"})
        try:
            with urlopen(request, timeout=max(.5, min(TIMEOUT, self.deadline-time.monotonic()))) as response:
                final = response.geturl()
                if final != url:
                    self.emit("fetch", final, "来源重定向至：" + (urlsplit(final).hostname or final))
                length = response.headers.get("Content-Length")
                if length and length.isdigit() and int(length) > MAX_BYTES:
                    raise ValueError("来源信息超过 3 MiB，本轮已跳过")
                blocks, size = [], 0
                while True:
                    self.check()
                    block = response.read(min(65536, MAX_BYTES+1-size))
                    if not block:
                        break
                    blocks.append(block)
                    size += len(block)
                    if size > MAX_BYTES:
                        raise ValueError("来源信息超过 3 MiB，本轮已跳过")
            self.check()
            return b"".join(blocks).decode("utf-8-sig")
        except HTTPError as exc:
            if exc.code in {401, 403, 412, 429}:
                raise OSError(f"来源拒绝当前请求（HTTP {exc.code}），可稍后重试或更换来源") from exc
            raise OSError(f"来源返回 HTTP {exc.code}") from exc
        except (URLError, UnicodeError) as exc:
            raise OSError("未能读取来源信息：" + _text(str(exc), 250)) from exc

    def result(self, title, url, source_url, **values):
        return {"title": _text(title, 200) or url, "url": url, "source_url": source_url,
                "provider": self.provider, "category": self.category, "site": self.site, **values}


def _site_search(site, query):
    if site == "bilibili":
        return "https://search.bilibili.com/all?" + urlencode({"keyword": query})
    if site == "youku":
        return "https://so.youku.com/search_video/q_" + quote(query, safe="")
    return "https://www.iqiyi.com/so/q_" + quote(query, safe="")


def _link_title(anchor, href, source_url):
    """Prefer explicit video names over thumbnail play-count/duration overlays."""
    label = anchor.get("title")
    heading = anchor.select_one("h3, h2, .bili-video-card__info--tit")
    if not label and heading:
        label = heading.get("title") or heading.get_text(" ", strip=True)
    # Actual Bilibili search cards put the thumbnail link before a separate
    # title link. Read only the same card AND same canonical video URL so an
    # adjacent recommendation's heading cannot label this thumbnail.
    card = anchor.find_parent(class_="bili-video-card__wrap")
    target = canonical_video_url(href)
    if not label and card and target:
        for candidate in card.select("a[href]"):
            try:
                same_video = canonical_video_url(urljoin(source_url, candidate["href"])) == target
            except (TypeError, ValueError):
                continue
            title = candidate.select_one("h3, h2, .bili-video-card__info--tit")
            if same_video and title:
                label = candidate.get("title") or title.get("title") or title.get_text(" ", strip=True)
                if label:
                    break
    picture = anchor.find("img")
    if not label and card and picture:
        label = picture.get("alt", "")
    if not label:
        label = anchor.get_text(" ", strip=True)
    if not label and picture:
        label = picture.get("alt", "")
    return label


def _link_description(anchor, href, source_url):
    """Read only a known result card containing this single canonical video.

    Generic parents can be a whole page or a multi-result list. Their headings,
    query text and other video captions are not evidence about this candidate.
    """
    target = canonical_video_url(href)
    if not target:
        return ""
    card = anchor.find_parent(class_="bili-video-card__wrap")
    if card is None:
        card = anchor.find_parent("li", class_="b_algo")
    if card is None:
        card = anchor.find_parent("div", class_="g")
    if card is None:
        return ""
    anchors = card.select("a[href]")
    if len(anchors) > 80:
        return ""
    videos = set()
    for candidate in anchors:
        try:
            url = canonical_video_url(urljoin(source_url, candidate["href"]))
        except (TypeError, ValueError):
            continue
        if url:
            videos.add(url)
    if videos != {target}:
        return ""
    return _text(card.get_text(" ", strip=True))


def _reject_challenge(page):
    title = page.title.get_text(" ", strip=True).casefold() if page.title else ""
    # A video discussing CAPTCHA is still valid content. Match explicit page
    # headings or verification controls, not a keyword anywhere in body text.
    headings = {"验证码", "安全验证", "访问验证", "人机验证", "百度安全验证", "captcha",
                "captcha verification", "security check", "security verification", "robot check",
                "verify you are human", "just a moment", "just a moment..."}
    title_parts = {part.strip() for part in re.split(r"[_|｜—-]", title)}
    controls = page.select_one(
        'form#captcha-form,form[action*="/sorry/"],form[action*="CaptchaRedirect"],'
        'form[action*="/captcha/"],#g-recaptcha,.g-recaptcha,.h-captcha,'
        'iframe[src*="/recaptcha/"],iframe[src*="hcaptcha.com/captcha"]')
    if title_parts & headings or controls is not None:
        raise ValueError("来源要求验证码或安全验证，本轮不会自动通过验证")


def _explicit_no_results(page):
    visible = page.get_text(" ", strip=True)[:20000]
    return bool(re.search(
        r"(?:没有找到|未找到|暂无|没有)[^。！？!?\n]{0,120}(?:相关|匹配|搜索)[^。！？!?\n]{0,40}(?:结果|视频|内容)"
        r"|(?:no\s+(?:matching\s+)?results?(?:\s+found)?|did not match any documents)",
        visible, re.I))


def _search_links(body, source_url, rss=False):
    if not isinstance(body, str) or not body.strip():
        raise ValueError("来源返回空页面，无法确认搜索结果；请稍后重试")
    if rss:
        feed = ElementTree.fromstring(body)
        if feed.tag == "rss" and feed.find("channel") is not None:
            return [(item.findtext("link"), item.findtext("title"), item.findtext("description"))
                    for item in feed.findall("./channel/item")[:80]]
        # An HTML challenge returned by an RSS endpoint is not an empty feed.
        from bs4 import BeautifulSoup
        _reject_challenge(BeautifulSoup(body, "html.parser"))
        raise ValueError("来源未返回有效的 RSS 搜索结果，可能需要验证或页面已变化")
    from bs4 import BeautifulSoup
    page = BeautifulSoup(body, "html.parser")
    _reject_challenge(page)
    links = []
    for anchor in page.select("a[href]")[:2000]:
        try:
            href = urljoin(source_url, anchor["href"])
            parsed = urlsplit(href)
        except (TypeError, ValueError):
            continue
        if parsed.hostname in {"www.google.com", "google.com"} and parsed.path == "/url":
            params = parse_qs(parsed.query)
            href = (params.get("q") or params.get("url") or [""])[0]
        label = _link_title(anchor, href, source_url)
        description = _link_description(anchor, href, source_url)
        links.append((href, label, description))
    # Structured public page data is parsed as data only; no JavaScript runs.
    for script in page.select('script[type="application/ld+json"],script[type="application/json"]')[:20]:
        try:
            value = json.loads(script.get_text())
        except (ValueError, TypeError):
            continue
        todo, visits = [value], 0
        while todo and visits < 3000:
            item = todo.pop()
            visits += 1
            if isinstance(item, dict):
                name = item.get("title") or item.get("name")
                if isinstance(name, str):
                    for key in ("url", "playUrl", "play_url", "videoUrl", "contentUrl"):
                        if isinstance(item.get(key), str):
                            links.append((item[key], name, item.get("description", "")))
                todo.extend(value for value in item.values() if isinstance(value, (list, dict)))
            elif isinstance(item, list):
                todo.extend(item[:500])
    if not links and not _explicit_no_results(page):
        raise ValueError("来源页面没有可读取的搜索结果，也未明确说明无结果；可能依赖动态加载或暂时不可用")
    return links


def _matches_query(query, title, description):
    value = _text(title + " " + _text(description)).casefold()
    parts = re.findall(r"[a-z0-9]{2,}|[\u3400-\u9fff]+", query.casefold())
    tokens = []
    for part in parts:
        if re.search(r"[\u3400-\u9fff]", part) and len(part) > 2:
            tokens.extend(part[i:i+2] for i in range(len(part)-1))
        else:
            tokens.append(part)
    return not tokens or any(token in value for token in tokens)


def _web(search, limit, sites, excluded=frozenset()):
    from iqiyi_discovery import IqiyiSearchError, iqiyi_search_url, parse_iqiyi_search
    from youku_discovery import parse_youku_search
    found, errors, successful_pages = {}, [], 0
    restricted_urls = set()
    for site in sites:
        search.site = site
        info = SITES[site]
        keyword = search.query + " site:" + ("v.youku.com/v_show" if site == "youku" else info["domain"])
        requests = [(_site_search(site, search.query), False, info["label"] + "站内搜索"),
                    ("https://www.bing.com/search?" + urlencode({"q": keyword, "format": "rss"}), True, "Bing 公开索引"),
                    ("https://www.google.com/search?" + urlencode({"q": keyword}), False, "Google 公开索引"),
                    (info["home"], False, info["label"] + "首页公开推荐（按关键词筛选）")]
        if site == "iqiyi":
            requests.insert(0, (iqiyi_search_url(search.query), False, "爱奇艺公开搜索接口"))
        for source_url, rss, label in requests:
            search.check()
            search.emit("query", source_url, "正在检索" + label + " · 仅接收" + info["label"] + "单视频")
            try:
                body = search.read(source_url)
                search.emit("parse", source_url, "正在筛选关键词匹配的单视频链接")
                matched_query = search.query
                host = urlsplit(source_url).hostname
                if site == "iqiyi" and host == "search.video.iqiyi.com":
                    try:
                        records = parse_iqiyi_search(body)
                    except IqiyiSearchError as exc:
                        # The public service rejects some vocabulary explicitly.
                        # A single disclosed synonym retry is not an empty result.
                        if exc.code != "A00004" or "沙雕" not in search.query:
                            raise
                        search.emit("warning", source_url, str(exc))
                        matched_query = search.query.replace("沙雕", "搞笑").replace("搞笑动画 搞笑", "搞笑动画")
                        source_url = iqiyi_search_url(matched_query)
                        search.emit("query", source_url, "爱奇艺拒绝原关键词，尝试同义词：" + matched_query,
                                    effective_query=matched_query)
                        body = search.read(source_url)
                        search.emit("parse", source_url, "正在筛选爱奇艺公开单视频结果")
                        records = parse_iqiyi_search(body)
                else:
                    links = (parse_youku_search(body, source_url)
                             if site == "youku" and host in {"so.youku.com", "www.youku.com", "v.youku.com", "m.youku.com"}
                             else _search_links(body, source_url, rss))
                    records = [{"url": href, "title": title, "description": description}
                               for href, title, description in links]
                # Only parsed results or an explicit empty result count. Blank
                # pages, challenge forms and JS shells raise before this point.
                successful_pages += 1
                count = 0
                for record in records:
                    search.check()
                    url = canonical_video_url(record["url"], [site])
                    title, description = _text(record["title"], 200), _text(record.get("description"))
                    if record.get("is_vip") or record.get("is_paid"):
                        if url:
                            restricted_urls.add(url)
                            found.pop(url, None)
                        search.emit("restricted", source_url, "公开搜索标记此视频为会员或付费，跳过自动下载候选", title=title)
                        continue
                    if url in restricted_urls:
                        search.emit("restricted", source_url, "此视频已被公开搜索标为会员或付费，其他来源不重复加入", title=title)
                        continue
                    if url and url in excluded:
                        search.emit("duplicate", source_url, "此视频已发现过，继续查找新素材")
                        continue
                    if url and title and url not in found and _matches_query(matched_query, title, description):
                        extra = {"description": description}
                        if record.get("duration_seconds") is not None:
                            extra["duration"] = record["duration_seconds"]
                        if isinstance(record.get("video_metadata"), dict):
                            extra["video_metadata"] = record["video_metadata"]
                        found[url] = search.result(title, url, source_url, **extra)
                        count += 1
                        if len(found) >= limit:
                            return list(found.values())
                if count:
                    break
                search.emit("warning", source_url, "此来源没有符合当前关键词的单视频结果，继续查找其他来源")
            except DiscoveryCancelled:
                raise
            except (OSError, ValueError, ElementTree.ParseError) as exc:
                errors.append(str(exc))
                search.emit("warning", source_url, _text(str(exc), 350))
    if not found and errors and successful_pages == 0:
        raise OSError("所选站点检索未完成：" + errors[-1])
    return list(found.values())


def discover(query, *, category="animation", provider="web", sites=None, limit=5,
             report=lambda event: None, stop=None, exclude_urls=None):
    """Find metadata on the user's allowed three sites; never download media."""
    if not isinstance(query, str) or not query.strip() or len(query) > 200 or any(ord(c) < 32 for c in query):
        raise ValueError("搜索关键词须为 1–200 个字符，不可含换行或控制字符")
    if category not in DEFAULT_PROVIDERS:
        raise ValueError("请选择沙雕动画或电影")
    if provider != "web":
        raise ValueError("当前仅允许优酷、爱奇艺、哔哩哔哩的公开检索")
    if sites is None:
        sites = list(SITES)
    if (not isinstance(sites, (list, tuple)) or not sites or len(sites) > 3 or
            any(not isinstance(site, str) or site not in SITES for site in sites)):
        raise ValueError("请选择优酷、爱奇艺、哔哩哔哩中的一个或多个站点")
    sites = list(dict.fromkeys(sites))
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("每个关键词的候选上限须为 1–20")
    search = _Search(query.strip(), category, provider, report, stop)
    search.check()
    excluded = {canonical_video_url(value, sites) for value in (exclude_urls or ())}
    excluded.discard(None)
    rows = _web(search, limit, sites, excluded)
    search.emit("complete", rows[0]["source_url"] if rows else "", f"本次发现 {len(rows)} 个三站单视频候选")
    return rows
