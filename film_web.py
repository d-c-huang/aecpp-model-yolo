"""Discover film pages through web search and read attributable TMDB credits."""
import json
from http.client import IncompleteRead
import re
import unicodedata
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree


ENGINES = {"Bing": ("https://www.bing.com/search", "q"),
           "Google": ("https://www.google.com/search", "q"),
           "百度": ("https://www.baidu.com/s", "wd")}
TMDB = "https://www.themoviedb.org"
YEAR = r"(?:18|19|20)\d{2}"
RELEASE_TAG = (r"(?:[248]K(?:\d+FPS)?|\d{3,4}[pi]|(?:BD|HD)\d{3,4}|Blu[ .-]?ray|"
               r"WEB[ ._-]?DL|x26[45]|超高清|高清|超清|蓝光|加长完整版|修复版|"
               r"国语(?:中字)?|粤语(?:中字)?|国粤双语|双语字幕|中文字幕)")


def identity(title, year=""):
    """Recover the film title from a filename or a decorated upload title."""
    name = re.sub(r"\.(?:mp4|mkv|avi|mov|m4v|webm|ts|wmv)$", "", title.strip(), flags=re.I)
    filename = name != title.strip()
    # A bracketed year outside the quoted film name is common in video titles.
    explicit_year = re.search(rf"[(（\[]({YEAR})[)）\]]", name)
    year = year or (explicit_year[1] if explicit_year else "")
    name = re.sub(rf"^[【\[(（]\s*{RELEASE_TAG}[^】\])）]*[】\])）]\s*(?=\S)", "", name, flags=re.I)
    quoted = re.findall(r"《([^《》]+)》", name)
    headline = re.match(r"^【([^【】]+)】", name)
    edition = re.match(rf"^[(（]([^()（）]+)[)）]\s*(?={RELEASE_TAG})", name, re.I)
    if len(quoted) == 1:
        name = quoted[0]
    elif headline or edition:
        name = (headline or edition)[1]
    else:
        name = re.split(rf"[ ._\-/|｜（(【\[]+{RELEASE_TAG}", name, maxsplit=1, flags=re.I)[0]
    match = re.search(rf"\s*[(（\[]({YEAR})[)）\]]$|[._]({YEAR})$", name)
    if match and name[:match.start()].strip() and (match[1] or filename):
        year = year or match[1] or match[2]
        name = name[:match.start()].rstrip("._ ")
    if filename:
        name = name.replace(".", " ").replace("_", " ")
    return name.strip(), year


def _key(title):
    return " ".join(unicodedata.normalize("NFKC", title).casefold().split())


def exact_titles(title, candidates):
    key = _key(title)
    return [row for row in candidates if any(_key(label) == key
            for label in (row["title"], *row.get("aliases", [])))]


def unique_match(title, year, candidates):
    matches = [row for row in exact_titles(title, candidates)
               if not year or not row.get("year") or row["year"] == year]
    if len(matches) == 1:
        row = matches[0]
        if row.get("catalogued") and (not year or row.get("year") == year):
            return row


def search_url(title, year, engine="Bing", *, discover=False):
    endpoint, key = ENGINES[engine]
    terms = [title, year, "site:themoviedb.org/movie" if discover else "电影 演员 角色 剧情"]
    params = {key: " ".join(filter(None, terms))}
    if discover and engine == "Bing":
        params["format"] = "rss"
    return endpoint + "?" + urlencode(params)


def read_page(url):
    request = Request(url, headers={"User-Agent": "YinggeFilmStudy/1.0 (desktop film metadata reader)"})
    catalog = url.startswith(TMDB+"/")
    attempts = 2 if catalog else 1
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=10 if catalog else 6) as response:
                data = response.read(4 * 1024 * 1024 + 1)
            break
        except HTTPError as exc:
            if exc.code not in {502, 503, 504} or attempt + 1 == attempts:
                raise
        except (URLError, TimeoutError, ConnectionError, IncompleteRead) as exc:
            if attempt + 1 == attempts:
                raise OSError("读取影片资料网页失败："+str(exc)) from exc
    if len(data) > 4 * 1024 * 1024:
        raise ValueError("影片资料网页过大")
    return data.decode("utf-8-sig")


def _movie_id(url, *, local=False):
    parts = urlsplit(url)
    if not (local and not parts.netloc) and parts.hostname not in {"themoviedb.org", "www.themoviedb.org"}:
        return None
    match = re.fullmatch(r"/movie/(\d+)(?:-[^/]*)?/?", parts.path)
    return match[1] if match else None


def _candidate(identifier, title, year, source, catalogued=False):
    return {"id": "tmdb:"+identifier, "title": title, "year": year, "catalogued": catalogued,
            "description": " · ".join(filter(None, [year, source])), "url": f"{TMDB}/movie/{identifier}"}


def _web_results(body, engine):
    from bs4 import BeautifulSoup
    if engine == "Bing":
        feed = ElementTree.fromstring(body)
        links = [(item.findtext("link"), item.findtext("title")) for item in feed.findall("./channel/item")]
    else:
        page = BeautifulSoup(body, "html.parser")
        links = []
        for link in page.select("a[href]"):
            href = link["href"]
            if href.startswith("/url?"):
                query = parse_qs(urlsplit(href).query)
                href = (query.get("q") or query.get("url") or [""])[0]
            if engine == "百度" and (container := link.find_parent(attrs={"mu": True})):
                href = container["mu"]
            links.append((href, link.get_text(" ", strip=True)))
    for href, label in links:
        identifier = _movie_id(href or "")
        if identifier and label:
            title = re.split(r"\s+[—|｜]\s+|\s+- The Movie Database", label, maxsplit=1)[0]
            title, year = identity(title)
            yield _candidate(identifier, title, year, engine+" · TMDB")


def search_candidates(title, year, engine, *, read=read_page):
    from bs4 import BeautifulSoup
    candidates, errors = {}, []
    try:
        for row in _web_results(read(search_url(title, year, engine, discover=True)), engine):
            candidates.setdefault(row["id"], row)
    except (OSError, ValueError, ElementTree.ParseError) as exc:
        errors.append(str(exc))
    language = "en-US" if title.isascii() else "zh-CN"
    try:
        url = TMDB+"/search/movie?"+urlencode({"query": title, "language": language})
        page = BeautifulSoup(read(url), "html.parser")
        for heading in page.select("#movie_results h2"):
            link = heading.find_parent("a", href=True)
            identifier = _movie_id(link["href"], local=True) if link else None
            if identifier:
                date = link.find_next_sibling(class_="release_date")
                found = re.search(YEAR, date.get_text()) if date else None
                labels = heading.find_all("span", recursive=False)
                title_text = (labels[0] if labels else heading).get_text(" ", strip=True)
                row = _candidate(identifier, title_text, found[0] if found else "", "TMDB", True)
                row["aliases"] = [span.get_text(" ", strip=True).strip("()（） ") for span in labels[1:]]
                candidates[row["id"]] = row
    except (OSError, ValueError) as exc:
        errors.append(str(exc))
    rows = sorted(candidates.values(), key=lambda r: (_key(r["title"]) != _key(title),
                  bool(year and r["year"] != year), not r["catalogued"]))
    return rows, errors


def fetch(identifier, *, read=read_page):
    from bs4 import BeautifulSoup
    if not re.fullmatch(r"\d+", identifier):
        raise ValueError("无效的 TMDB 影片编号")
    url = f"{TMDB}/movie/{identifier}?language=zh-CN"
    page = BeautifulSoup(read(url), "html.parser")
    for script in page.select('script[type="application/ld+json"]'):
        try:
            movie = json.loads(script.get_text())
        except ValueError:
            continue
        if isinstance(movie, dict) and movie.get("@type") == "Movie" and _movie_id(movie.get("url", "")) == identifier:
            break
    else:
        raise ValueError("TMDB 未返回对应影片资料，请选择其他候选或稍后重试")
    cast = []
    for card in page.select("ol.people.scroller li.card"):
        name, role = card.select_one("p a[href^='/person/']"), card.select_one("p.character")
        if name:
            cast.append({"actor": name.get_text(" ", strip=True),
                         "role": role.get_text(" ", strip=True) if role else "", "aliases": []})
    credited = {row["actor"] for row in cast}
    for person in movie.get("actor") or []:
        if isinstance(person, dict) and (name := person.get("name")) and name not in credited:
            cast.append({"actor": name, "role": "", "aliases": []})
            credited.add(name)
    year = re.match(YEAR, movie.get("datePublished") or "")
    return {"title": movie["name"], "year": year[0] if year else "", "cast": cast,
            "synopsis": movie.get("description") or "", "events": [],
            "sources": [{"url": url, "title": "TMDB · "+movie["name"]}]}
