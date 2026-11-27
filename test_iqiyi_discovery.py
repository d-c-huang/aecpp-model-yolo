import json
import unittest
from urllib.parse import parse_qs, urlsplit

from iqiyi_discovery import IqiyiSearchError, iqiyi_search_url, parse_iqiyi_search


def response(*albums):
    return json.dumps({"code": "A00000", "data": {
        "code": 0, "docinfos": [{"albumDocInfo": album} for album in albums],
    }})


class IqiyiDiscoveryTests(unittest.TestCase):
    def test_url_preserves_public_query_and_bounded_paging(self):
        parsed = urlsplit(iqiyi_search_url("搞笑动画 & 电影", page=2, page_size=5))
        self.assertEqual(parsed.hostname, "search.video.iqiyi.com")
        self.assertEqual(parse_qs(parsed.query), {
            "if": ["html5"], "key": ["搞笑动画 & 电影"], "pageNum": ["2"], "pageSize": ["5"],
        })
        for kwargs in ({"page": 0}, {"page_size": 100}, {"page": True}):
            with self.assertRaises(ValueError):
                iqiyi_search_url("动画", **kwargs)
        for query in ("", "a" * 201, "test\nquery"):
            with self.assertRaises(ValueError):
                iqiyi_search_url(query)

    def test_real_mobile_schema_uses_episode_links_and_metadata(self):
        rows = parse_iqiyi_search(response({
            "siteId": "iqiyi", "albumTitle": "吃豆人搞笑动画", "channel": "动漫,4",
            "albumLink": "http://www.iqiyi.com/a_21b7sp7v3bx.html",
            "three_category_v2": "动作,脑洞,动画", "tvFocus": "迷宫大冒险",
            "videoinfos": [{"itemTitle": "吃豆人搞笑动画 第10集",
                "itemLink": "http://www.iqiyi.com/v_1u76moq2k44.html?src=search",
                "timeLength": 33, "is_vip": False, "is_pay": False}],
        }))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["url"], "https://www.iqiyi.com/v_1u76moq2k44.html")
        self.assertEqual(rows[0]["duration_seconds"], 33)
        self.assertIn("脑洞", rows[0]["description"])
        self.assertFalse(rows[0]["is_vip"])

    def test_status_is_not_silently_reported_as_empty_success(self):
        with self.assertRaises(IqiyiSearchError) as caught:
            parse_iqiyi_search('{"data":" search key is invalid ","code":"A00004"}')
        self.assertEqual(caught.exception.code, "A00004")
        with self.assertRaises(IqiyiSearchError):
            parse_iqiyi_search('{"code":"A00001","data":{}}')

    def test_explicit_empty_and_malformed_responses(self):
        self.assertEqual(parse_iqiyi_search('{"data":" search result is empty ","code":"A00000"}'), [])
        self.assertEqual(parse_iqiyi_search(response()), [])
        for body in ('<html>搜索</html>', 'callback({});', '[]', '{}', '{"code":"A00000","data":{}}'):
            with self.assertRaises(ValueError):
                parse_iqiyi_search(body)
        with self.assertRaises(ValueError):
            parse_iqiyi_search(" " * (3 * 1024 * 1024 + 1))

    def test_only_direct_iqiyi_video_urls(self):
        invalid = ["https://example.com/v_test.html", "https://iqiyi.com.evil.test/v_a.html",
                   "https://www.iqiyi.com/a_album.html", "javascript:alert(1)",
                   "https://user:pass@www.iqiyi.com/v_a.html", "https://www.iqiyi.com:9000/v_a.html",
                   "https://so.iqiyi.com/links/redirect", "https://www.iqiyi.com/v_a.html\n"]
        for url in invalid:
            with self.subTest(url=url):
                self.assertEqual(parse_iqiyi_search(response({"albumTitle": "动画", "albumLink": url})), [])
        self.assertEqual(parse_iqiyi_search(response({"siteId": "youku", "albumTitle": "动画",
            "albumLink": "https://www.iqiyi.com/v_a.html"})), [])

    def test_duplicate_urls_and_single_video_fallback(self):
        albums = [{"albumTitle": "<em>搞笑</em> &amp; 动画", "albumLink": url, "timeLength": "10"}
                  for url in ("http://m.iqiyi.com/w_123.html?x=1", "https://www.iqiyi.com/w_123.html")]
        rows = parse_iqiyi_search(response(*albums))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "搞笑 & 动画")
        self.assertEqual(rows[0]["duration_seconds"], 10)

    def test_episode_membership_flags_override_album_metadata(self):
        rows = parse_iqiyi_search(response({"albumTitle": "电影", "isPurchase": 2,
            "videoinfos": [
                {"itemTitle": "免费预告", "itemLink": "https://www.iqiyi.com/v_free.html", "is_vip": False},
                {"itemTitle": "会员", "itemLink": "https://www.iqiyi.com/v_vip.html", "is_vip": True},
                {"itemTitle": "未知", "itemLink": "https://www.iqiyi.com/v_unknown.html"},
                {"itemTitle": "付费", "itemLink": "https://www.iqiyi.com/v_paid.html", "is_pay": "true"},
            ]}))
        self.assertEqual([r["is_vip"] for r in rows], [False, True, True, True])
        self.assertTrue(rows[-1]["is_paid"])

    def test_bad_rows_and_invalid_durations_do_not_abort_good_results(self):
        albums = [{"albumTitle": "动画", "albumLink": f"https://www.iqiyi.com/v_a{i}.html",
                   "timeLength": duration} for i, duration in enumerate((True, -1, "nan", "inf", "bad", None))]
        rows = parse_iqiyi_search(response(None, [], {}, *albums))
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(row["duration_seconds"] is None for row in rows))

    def test_public_video_identifiers_are_validated_and_restricted_hints_omitted(self):
        videos = [{"itemTitle": "搞笑短片", "itemLink": f"https://www.iqiyi.com/v_a{i}.html",
                   "tvId": 6964905246217000, "vid": "af6630d00d5e22a43561083efdc44404",
                   "timeLength": 10, "is_vip": False, **extra}
                  for i, extra in enumerate(({}, {"is_vip": True}, {"is_pay": True}, {"vid": "bad"}))]
        rows = parse_iqiyi_search(response({"siteId": "iqiyi", "videoinfos": videos}))
        self.assertEqual(rows[0]["video_metadata"]["tvid"], "6964905246217000")
        self.assertEqual(rows[0]["video_metadata"]["url"], rows[0]["url"])
        self.assertEqual(set(rows[0]["video_metadata"]), {"site", "url", "tvid", "vid", "title", "duration"})
        self.assertTrue(all("video_metadata" not in row for row in rows[1:]))


if __name__ == "__main__":
    unittest.main()
