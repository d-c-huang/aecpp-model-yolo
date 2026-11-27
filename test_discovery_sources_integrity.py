"""Candidate-local evidence and honest empty/challenge source handling."""
import unittest
from unittest.mock import patch

import discovery_sources as sources
import test_discovery_sources as fixtures


VIDEO = "https://www.bilibili.com/video/BV1xx411c7mD"
OTHER = "https://www.bilibili.com/video/BV15d4y1h7Gq"


class DiscoveryIntegrityTests(unittest.TestCase):
    def test_page_heading_and_sibling_caption_do_not_describe_unrelated_anchor(self):
        for body in (
            f'<html><title>首页</title><body><h1>沙雕动画 搞笑</h1><a href="{VIDEO}">Python 数据库课程</a></body></html>',
            f'<div><a href="{OTHER}">沙雕动画 搞笑</a><a href="{VIDEO}">Python 数据库课程</a></div>',
        ):
            row = next(row for row in sources._search_links(body,"https://www.bilibili.com/") if row[0] == VIDEO)
            self.assertEqual(row[2], "")
            self.assertFalse(sources._matches_query("沙雕动画 搞笑",row[1],row[2]))

    def test_same_video_bilibili_card_keeps_its_local_description(self):
        body = f'''<h1>首页推荐</h1><div class="bili-video-card__wrap">
          <a href="{VIDEO}"><img alt="角色过生日"></a>
          <a href="{VIDEO}?tracking=1"><h3>角色过生日</h3></a>
          <p>沙雕动画：朋友们准备惊喜</p></div>
          <div class="bili-video-card__wrap"><a href="{OTHER}">Python 教学</a></div>'''
        rows = sources._search_links(body,"https://www.bilibili.com/")
        self.assertIn("朋友们准备惊喜",rows[0][2])
        self.assertNotIn("Python",rows[0][2])
        self.assertTrue(sources._matches_query("沙雕动画",rows[0][1],rows[0][2]))

    def test_mixed_video_card_cannot_share_descriptions(self):
        body = f'<div class="bili-video-card__wrap"><a href="{OTHER}">沙雕动画</a><a href="{VIDEO}">编程教学</a></div>'
        rows = sources._search_links(body,"https://www.bilibili.com/")
        self.assertTrue(all(row[2] == "" for row in rows))
        self.assertFalse(sources._matches_query("沙雕动画",rows[1][1],rows[1][2]))

    def test_body_verification_controls_are_rejected_without_challenge_title(self):
        controls = ('<form id="captcha-form">请输入验证码</form>',
                    '<form action="/sorry/index">继续</form>',
                    '<div class="g-recaptcha" data-sitekey="fixture"></div>')
        for control in controls:
            with self.subTest(control=control), self.assertRaisesRegex(ValueError,"验证码"):
                sources._search_links('<html><title>Google</title><body>'+control+'</body></html>',
                                      "https://www.google.com/search")

    def test_videos_discussing_captcha_are_not_blocked_by_plain_words(self):
        body = f'<html><title>验证码是什么：沙雕动画讲解</title><body><a href="{VIDEO}">沙雕动画：验证码难倒了谁</a><p>讨论安全验证</p></body></html>'
        rows=sources._search_links(body,"https://www.bilibili.com/")
        self.assertEqual(len(rows),1)
        self.assertIn("验证码",rows[0][1])

    def test_rss_requires_real_feed_and_preserves_valid_empty_feed(self):
        self.assertEqual(sources._search_links('<rss><channel/></rss>',"https://www.bing.com/search",True),[])
        with self.assertRaisesRegex(ValueError,"验证码"):
            sources._search_links('<html><title>Google</title><body><form id="captcha-form"/></body></html>',
                                  "https://www.bing.com/search",True)
        with self.assertRaisesRegex(ValueError,"RSS"):
            sources._search_links('<html><title>Loading</title><body/></html>',"https://www.bing.com/search",True)

    def test_blank_or_dynamic_shell_does_not_mask_all_other_source_failures(self):
        for empty in ("", " \n\t", '<html><title>搜索</title><body><div id="app"></div><script>load()</script></body></html>',
                      '<html><title>Google</title><body><form id="captcha-form">验证</form></body></html>'):
            events=[]
            def read(request,timeout):
                if "search.bilibili.com" in request.full_url:
                    return fixtures.Response(empty,request.full_url)
                raise OSError("fixture source denied")
            with self.subTest(empty=empty),patch.object(sources,"urlopen",side_effect=read):
                with self.assertRaisesRegex(OSError,"检索未完成"):
                    sources.discover("沙雕动画",sites=["bilibili"],report=events.append)
            self.assertFalse(any(row["phase"]=="complete" for row in events))

    def test_explicit_empty_search_page_is_normal_zero_result(self):
        for phrase in ("抱歉，没有找到相关视频", "未找到与关键词匹配的搜索结果", "No results found"):
            self.assertEqual(sources._search_links('<html><title>搜索</title><body>'+phrase+'</body></html>',
                             "https://search.bilibili.com/all"),[])
        def read(request,timeout):
            if "search.bilibili.com" in request.full_url:
                return fixtures.Response('<html><title>搜索</title><body>抱歉，没有找到相关视频</body></html>',request.full_url)
            raise OSError("fixture source denied")
        events=[]
        with patch.object(sources,"urlopen",side_effect=read):
            self.assertEqual(sources.discover("沙雕动画",sites=["bilibili"],report=events.append),[])
        self.assertEqual(events[-1]["phase"],"complete")


if __name__ == "__main__":
    unittest.main()
