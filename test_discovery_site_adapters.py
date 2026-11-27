"""Network-free contracts between site adapters and the real discovery loop."""
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import discovery_sources as sources
from test_discovery_sources import Response


def iqiyi(*videos):
    return json.dumps({"code":"A00000","data":{"docinfos":[{"albumDocInfo":{
        "siteId":"iqiyi", "albumTitle":"搞笑动画", "videoinfos":list(videos)}}]}})


def video(key="one", **values):
    return {"itemLink":f"https://www.iqiyi.com/v_{key}.html", "itemTitle":"搞笑动画 · " + key,
            "timeLength":10,"is_vip":False, **values}


class SiteAdapterIntegrationTests(unittest.TestCase):
    def test_iqiyi_public_json_preserves_real_source_duration_and_deduplicates(self):
        requests = []
        def read(request, timeout):
            requests.append(request.full_url)
            return Response(iqiyi(video("old"),video("new")),request.full_url)
        with patch.object(sources,"urlopen",side_effect=read):
            rows=sources.discover("搞笑动画",sites=["iqiyi"],limit=1,
                                  exclude_urls=["https://www.iqiyi.com/v_old.html"])
        self.assertEqual(len(requests),1)
        self.assertEqual(rows[0]["url"],"https://www.iqiyi.com/v_new.html")
        self.assertEqual(rows[0]["source_url"],requests[0])
        self.assertEqual(rows[0]["duration"],10)

    def test_invalid_keyword_uses_one_disclosed_synonym_request(self):
        requests,events=[],[]
        def read(request,timeout):
            requests.append(request.full_url)
            query=parse_qs(urlsplit(request.full_url).query)["key"][0]
            body='{"code":"A00004","data":"search key is invalid"}' if "沙雕" in query else iqiyi(video())
            return Response(body,request.full_url)
        with patch.object(sources,"urlopen",side_effect=read):
            rows=sources.discover("沙雕动画 搞笑",sites=["iqiyi"],limit=1,report=events.append)
        self.assertEqual(len(requests),2)
        self.assertEqual(parse_qs(urlsplit(requests[1]).query)["key"],["搞笑动画"])
        self.assertEqual(rows[0]["source_url"],requests[1])
        self.assertTrue(any("A00004" in e["message"] for e in events))
        self.assertTrue(any(e.get("effective_query")=="搞笑动画" for e in events))

    def test_paid_candidates_do_not_fill_the_limit(self):
        events=[]
        body=iqiyi(video("vip",is_vip=True),video("paid",is_pay=True),video("free"))
        with patch.object(sources,"urlopen",side_effect=lambda r,timeout:Response(body,r.full_url)):
            rows=sources.discover("搞笑动画",sites=["iqiyi"],limit=1,report=events.append)
        self.assertEqual(rows[0]["url"],"https://www.iqiyi.com/v_free.html")
        self.assertEqual(len([e for e in events if e["phase"]=="restricted"]),2)

    def test_api_failure_is_not_treated_as_keyword_rejection(self):
        urls,events=[],[]
        def read(request,timeout):
            urls.append(request.full_url)
            body=('{"code":"A00001","data":"service unavailable"}' if "search.video.iqiyi.com" in request.full_url
                  else '<a href="https://www.iqiyi.com/v_fallback.html">沙雕动画</a>')
            return Response(body,request.full_url)
        with patch.object(sources,"urlopen",side_effect=read):
            rows=sources.discover("沙雕动画",sites=["iqiyi"],limit=1,report=events.append)
        self.assertEqual(sum("search.video.iqiyi.com" in url for url in urls),1)
        self.assertTrue(any("A00001" in e["message"] for e in events))
        self.assertIn("/so/",rows[0]["source_url"])

    def test_known_paid_video_cannot_reenter_through_html_fallback(self):
        def read(request,timeout):
            url=request.full_url
            if "search.video.iqiyi.com" in url:
                body=iqiyi(video("vip",is_vip=True))
            elif "bing.com" in url:
                body='<rss><channel><item><title>搞笑动画</title><link>https://www.iqiyi.com/v_vip.html</link></item></channel></rss>'
            else:
                body='<a href="https://www.iqiyi.com/v_vip.html">搞笑动画</a>'
            return Response(body,url)
        with patch.object(sources,"urlopen",side_effect=read):
            rows=sources.discover("搞笑动画",sites=["iqiyi"],limit=1)
        self.assertEqual(rows,[])

    def test_youku_captcha_falls_back_to_real_ssr_home_cards(self):
        events=[]
        def read(request,timeout):
            url=request.full_url
            body='<rss><channel/></rss>' if "bing.com" in url else '<html><title>搜索</title><p>未找到相关结果</p></html>'
            if "so.youku.com" in url:
                body='<script>window.location="/_____tmd_____/punish";</script>'
            elif url==sources.SITES["youku"]["home"]:
                body='<script>window.__INITIAL_DATA__='+json.dumps({"title":"搞笑电影预告","action":{"type":"JUMP_TO_VIDEO","value":"XN123456=="}})+';</script>'
            return Response(body,url)
        with patch.object(sources,"urlopen",side_effect=read):
            rows=sources.discover("喜剧电影",sites=["youku"],limit=1,report=events.append)
        self.assertEqual(rows[0]["url"],"https://v.youku.com/v_show/id_XN123456==.html")
        self.assertEqual(rows[0]["source_url"],sources.SITES["youku"]["home"])
        self.assertTrue(any("验证码" in e["message"] for e in events))
