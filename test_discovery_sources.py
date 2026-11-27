"""Finite public-page fixtures; no remote media or accounts in test runs."""
import base64
import io
import json
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import discovery_sources as sources


class Response(io.BytesIO):
    def __init__(self, body, url, headers=None):
        super().__init__(body.encode("utf-8") if isinstance(body, str) else body)
        self.url, self.headers = url, headers or {}

    def geturl(self):
        return self.url


def html_link(url, title="沙雕动画 · 早睡计划"):
    return f'<html><head><title>公开搜索</title></head><body><a href="{url}">{title}</a></body></html>'


class DiscoverySourcesTests(unittest.TestCase):
    def test_bilibili_actual_card_uses_matching_title_before_thumbnail_statistics(self):
        # Reduced from the observed public search card: cover link is first,
        # but its visible text is only views/comments/duration, not the title.
        body = '''<div class="bili-video-card__wrap">
          <a href="//www.bilibili.com/video/BV15d4y1h7Gq/">
            <div class="bili-video-card__image"><picture><img alt="封面旧标题"></picture></div>
            <div class="bili-video-card__stats"><span>10.4万</span><span>293</span><span>01:58:01</span></div>
          </a>
          <div class="bili-video-card__info"><a href="//www.bilibili.com/video/BV15d4y1h7Gq/">
            <h3 class="bili-video-card__info--tit" title="沙雕动画：小时候的那些事"><em>沙雕动画</em>：小时候的那些事</h3>
          </a></div>
        </div>
        <div class="bili-video-card__wrap"><a href="//www.bilibili.com/video/BV1xx411c7mD/">
          <h3 title="另一个视频">另一个视频</h3></a></div>'''
        with patch.object(sources, "urlopen", side_effect=lambda request, timeout: Response(body, request.full_url)) as network:
            rows = sources.discover("沙雕动画", sites=["bilibili"], limit=1)
        self.assertEqual(network.call_count, 1)
        self.assertEqual(rows[0]["title"], "沙雕动画：小时候的那些事")
        self.assertEqual(rows[0]["url"], "https://www.bilibili.com/video/BV15d4y1h7Gq")

    def test_explicit_headings_and_generic_link_fallbacks_keep_their_own_labels(self):
        body = '''<a href="/a" title="明确标题"><h3>次要标题</h3>123</a>
                  <a href="/b"><h3 title="段落标题">123</h3>456</a>
                  <a href="/c">普通文字标题</a>
                  <a href="/d"><img alt="图片标题"></a>
                  <div class="bili-video-card__wrap">
                    <a href="//www.bilibili.com/video/BV15d4y1h7Gq/"><img alt="当前封面标题">123</a>
                    <a href="//www.bilibili.com/video/BV1xx411c7mD/"><h3 title="不同视频标题">456</h3></a>
                  </div>'''
        rows = sources._search_links(body, "https://example.com/search")
        self.assertEqual([row[1] for row in rows[:4]], ["明确标题", "段落标题", "普通文字标题", "图片标题"])
        self.assertEqual(rows[4][1], "当前封面标题")

    def test_known_urls_are_skipped_before_new_candidate_limit(self):
        old = "https://www.bilibili.com/video/BV1xx411c7mD"
        new = "https://www.bilibili.com/video/BV1xx411c7mE"
        body = html_link(old)+html_link(new)
        events = []
        with patch.object(sources, "urlopen", side_effect=lambda request, timeout: Response(body, request.full_url)):
            rows = sources.discover("沙雕动画", sites=["bilibili"], limit=1,
                                    exclude_urls=[old+"?utm_source=old"], report=events.append)
        self.assertEqual([row["url"] for row in rows], [new])
        self.assertTrue(any(row["phase"] == "duplicate" for row in events))

    def test_only_three_sites_and_single_videos_are_canonicalized(self):
        cases = {
            "http://m.bilibili.com/video/BV1xx411c7mD/?spm_id_from=123": "https://www.bilibili.com/video/BV1xx411c7mD",
            "https://v.youku.com/video?vid=XNjU3OTY3NjkzMg==&s=series": "https://v.youku.com/v_show/id_XNjU3OTY3NjkzMg==.html",
            "https://m.youku.com/video/id_XN123456.html": "https://v.youku.com/v_show/id_XN123456.html",
            "https://v.youku.com/v_show/id_XNTE2MjAwMDIzMg%3D%3D.html?spm=test": "https://v.youku.com/v_show/id_XNTE2MjAwMDIzMg==.html",
            "https://m.iqiyi.com/v_19rt123abc.html?src=search": "https://www.iqiyi.com/v_19rt123abc.html",
        }
        for value, expected in cases.items():
            self.assertEqual(sources.canonical_video_url(value), expected)
        for value in ("https://www.youtube.com/watch?v=abcdefghijk", "https://archive.org/details/comedy",
                      "https://www.bilibili.com/", "https://live.bilibili.com/123", "https://bilibili.com.evil.test/video/BV1xx411c7mD",
                      "https://v.youku.com/video?s=series", "https://www.iqiyi.com/a_123.html",
                      "https://v.youku.com/download/setup.exe", "https://user:pass@www.iqiyi.com/v_123.html",
                      "https://[broken", "https://www.iqiyi.com:444/v_123.html",
                      "https://v.youku.com/v_show/id_XN123%253D.html",
                      "https://v.youku.com/v_show/id_XN123%2Fdownload.html",
                      "https://v.youku.com/v_show/id_XN123%00.html"):
            self.assertIsNone(sources.canonical_video_url(value), value)
        self.assertIsNone(sources.canonical_video_url(cases[next(iter(cases))], ["youku"]))

    def test_native_search_reports_source_and_stops_at_limit(self):
        events, requests = [], []
        def read(request, timeout):
            requests.append(request.full_url)
            return Response(html_link("https://www.bilibili.com/video/BV1xx411c7mD"), request.full_url)
        with patch.object(sources, "urlopen", side_effect=read):
            rows = sources.discover("沙雕动画", sites=["bilibili"], limit=1, report=events.append)
        self.assertEqual(len(requests), 1)
        self.assertEqual(rows[0]["source_url"], requests[0])
        self.assertEqual(rows[0]["site"], "bilibili")
        self.assertEqual([row["phase"] for row in events], ["query", "fetch", "parse", "complete"])
        self.assertTrue(all(row["query"] == "沙雕动画" for row in events))

    def test_each_selected_site_can_contribute_real_candidate_urls(self):
        requests = []
        def read(request, timeout):
            url = request.full_url
            requests.append(url)
            if "search.video.iqiyi.com" in url:
                body = json.dumps({"code":"A00000", "data":{"docinfos":[{"albumDocInfo":{
                    "albumTitle":"喜剧电影片段", "albumLink":"https://www.iqiyi.com/w_19rt123abc.html"}}]}})
                return Response(body, url)
            target = ("https://v.youku.com/video?vid=XNjU3OTY3NjkzMg==" if "youku" in url
                      else "https://www.iqiyi.com/w_19rt123abc.html")
            return Response(html_link(target, "喜剧电影片段"), url)
        with patch.object(sources, "urlopen", side_effect=read):
            rows = sources.discover("喜剧电影", category="movie", sites=["youku", "iqiyi"], limit=2)
        self.assertEqual([row["site"] for row in rows], ["youku", "iqiyi"])
        self.assertTrue(all(row["category"] == "movie" for row in rows))
        self.assertEqual(len(requests), 2)

    def test_captcha_and_irrelevant_search_results_are_visible_and_not_downloadable(self):
        events = []
        def read(request, timeout):
            url = request.full_url
            if "search.bilibili" in url:
                body = "<html><title>验证码_哔哩哔哩</title></html>"
            elif "bing.com" in url:
                body = '<rss><channel><item><title>沙雕动画</title><link>https://www.youtube.com/watch?v=abcdefghijk</link></item></channel></rss>'
            elif "google.com" in url:
                body = html_link("/url?q=https%3A%2F%2Fwww.bilibili.com%2Fvideo%2FBV1xx411c7mD")
            else:
                raise AssertionError("enough results; no further fetch")
            return Response(body, url)
        with patch.object(sources, "urlopen", side_effect=read):
            rows = sources.discover("沙雕动画", sites=["bilibili"], limit=1, report=events.append)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["url"].startswith("https://www.bilibili.com/video/"))
        self.assertTrue(any("验证码" in row["message"] for row in events))
        self.assertIn("google.com", rows[0]["source_url"])

    def test_homepage_is_only_a_keyword_filtered_fallback(self):
        def read(request, timeout):
            url = request.full_url
            body = "<rss><channel/></rss>" if "bing.com" in url else "<html><title>动态页面</title></html>"
            if url == sources.SITES["youku"]["home"]:
                body = html_link("https://v.youku.com/video?s=collection", "喜剧电影") + html_link(
                    "https://v.youku.com/video?vid=XNjU3OTY3NjkzMg==", "喜剧电影 · 单片")
            return Response(body, url)
        with patch.object(sources, "urlopen", side_effect=read):
            rows = sources.discover("喜剧电影", category="movie", sites=["youku"], limit=1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_url"], "https://www.youku.com/ku/webhome")

    def test_empty_results_do_not_become_fake_successes(self):
        events = []
        def read(request, timeout):
            body = "<rss><channel/></rss>" if "bing.com" in request.full_url else html_link(
                "https://www.bilibili.com/video/BV1xx411c7mD", "与关键词毫无关系的商品介绍")
            return Response(body, request.full_url)
        with patch.object(sources, "urlopen", side_effect=read) as network:
            rows = sources.discover("沙雕动画", sites=["bilibili"], report=events.append)
        self.assertEqual(rows, [])
        self.assertEqual(network.call_count, 4)
        self.assertEqual(events[-1]["message"], "本次发现 0 个三站单视频候选")

    def test_duplicate_tracking_links_become_one_candidate(self):
        def read(request, timeout):
            body = html_link("https://www.bilibili.com/video/BV1xx411c7mD?spm=one") + html_link(
                "https://m.bilibili.com/video/BV1xx411c7mD/?spm=two")
            return Response(body, request.full_url)
        with patch.object(sources, "urlopen", side_effect=read) as network:
            rows = sources.discover("沙雕动画", sites=["bilibili"], limit=5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(network.call_count, 1)

    def test_malformed_unrelated_anchor_does_not_discard_valid_results(self):
        body = html_link("https://[broken") + html_link("https://www.bilibili.com/video/BV1xx411c7mD")
        with patch.object(sources, "urlopen", side_effect=lambda request, timeout: Response(body, request.full_url)):
            rows = sources.discover("沙雕动画", sites=["bilibili"], limit=1)
        self.assertEqual(rows[0]["url"], "https://www.bilibili.com/video/BV1xx411c7mD")

    def test_query_provider_sites_and_limits_are_validated_before_network(self):
        with patch.object(sources, "urlopen", side_effect=AssertionError("network forbidden")):
            for kwargs in ({"query": ""}, {"query": "a\nb"}, {"query": "沙雕", "provider": "archive"},
                           {"query": "沙雕", "provider": "youtube"}, {"query": "沙雕", "sites": ["other"]},
                           {"query": "沙雕", "sites": [{}]}, {"query": "沙雕", "sites": []},
                           {"query": "沙雕", "limit": True}, {"query": "沙雕", "limit": 21}):
                with self.assertRaises(ValueError):
                    sources.discover(**kwargs)

    def test_stop_before_and_during_io_leaves_no_candidates(self):
        stop = threading.Event()
        stop.set()
        with patch.object(sources, "urlopen", side_effect=AssertionError("cancelled")):
            with self.assertRaises(sources.DiscoveryCancelled):
                sources.discover("沙雕动画", stop=stop)
        stop.clear()
        response = Response("<html>partial", "https://search.bilibili.com/all")
        original = response.read
        def partial(size):
            value = original(size)
            stop.set()
            return value
        response.read = partial
        with patch.object(sources, "urlopen", return_value=response):
            with self.assertRaises(sources.DiscoveryCancelled):
                sources.discover("沙雕动画", sites=["bilibili"], stop=stop)
        self.assertTrue(response.closed)

    def test_source_response_limit_and_http_errors_are_real_failures(self):
        def huge(request, timeout):
            return Response("x", request.full_url, {"Content-Length": str(sources.MAX_BYTES+1)})
        with patch.object(sources, "urlopen", side_effect=huge):
            with self.assertRaisesRegex(OSError, "超过"):
                sources.discover("沙雕动画", sites=["bilibili"])
        with patch.object(sources, "urlopen", side_effect=HTTPError("https://www.bilibili.com/", 403, "forbidden", {}, None)):
            with self.assertRaisesRegex(OSError, "403"):
                sources.discover("沙雕动画", sites=["bilibili"])

    def test_bing_encoded_single_video_link_is_unwrapped_without_following_it(self):
        target = "https://www.bilibili.com/video/BV1xx411c7mD"
        encoded = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
        self.assertEqual(sources.canonical_video_url("https://www.bing.com/ck/a?u=a1"+encoded), target)
        self.assertIsNone(sources.canonical_video_url("https://www.bing.com/ck/a?u=a1!!!!"))


if __name__ == "__main__":
    unittest.main()
