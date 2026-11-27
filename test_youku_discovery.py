"""Youku public page shapes captured during bounded real diagnostics."""
import json
import unittest

from youku_discovery import parse_youku_search


SOURCE = "https://www.youku.com/ku/webhome"


def script(value):
    return "<script>window.__INITIAL_DATA__ ="+json.dumps(value, ensure_ascii=False)+";</script>"


class YoukuDiscoveryTests(unittest.TestCase):
    def test_script_only_captcha_is_not_reported_as_successful_empty_search(self):
        body = '''<script>sessionStorage.x5referer=window.location.href;
        window.location.replace("/search_video/q_test/_____tmd_____/punish?x5secdata=fixture");
        window._config_={"action":"captcha","url":"fixture"};</script><!--rgv587_flag:sm-->'''
        with self.assertRaisesRegex(ValueError, "验证码"):
            parse_youku_search(body, "https://so.youku.com/search_video/q_test")

    def test_actual_ssr_jump_to_video_cards_are_extracted_and_deduplicated(self):
        cards = [{"id": 1290500058, "title": "搞笑动漫：偶遇教练，机智如我！", "type": 10013,
                  "action": {"type": "JUMP_TO_VIDEO", "value": "XNTE2MjAwMDIzMg==",
                             "report": {"trackInfo": {"videoid": 1290500058}}}},
                 {"title": "合集不应伪造成单视频", "action": {"type": "JUMP_TO_SHOW", "value": "z012345"}},
                 {"title": "数字id也不转换", "videoId": 1290500058}]
        body = script({"pageMap": {"modules": [{"nodes": cards}]}})+'''<a href="https://v.youku.com/v_show/id_XNTE2MjAwMDIzMg%3D%3D.html">重复标题</a>'''
        rows = parse_youku_search(body, SOURCE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][:2], ("https://v.youku.com/v_show/id_XNTE2MjAwMDIzMg==.html", "搞笑动漫：偶遇教练，机智如我！"))

    def test_json_escape_decoding_handles_actual_page_conf_urls_without_execution(self):
        body = r'''<script>window.__PAGE_CONF__ ={"title":"搞笑名场面，沙雕买车记","vUrl":"https:\u002F\u002Fv.youku.com\u002Fv_show\u002Fid_XNDA3NjU2NTI4MA==.html"};doNotExecute();</script>'''
        rows = parse_youku_search(body, SOURCE)
        self.assertEqual(rows[0][0], "https://v.youku.com/v_show/id_XNDA3NjU2NTI4MA==.html")

    def test_direct_video_og_metadata_keeps_actual_page_title_and_description(self):
        source = "https://v.youku.com/v_show/id_XNTE2MjAwMDIzMg==.html"
        body = '''<meta property="og:title" content="搞笑动漫：偶遇教练，机智如我！">
                  <meta property="og:description" content="公开页面原始简介">
                  <meta property="og:video" content="https://v.youku.com/v_show/id_XNTE2MjAwMDIzMg==.html?vid=XNTE2MjAwMDIzMg==">'''
        self.assertEqual(parse_youku_search(body, source), [(source, "搞笑动漫：偶遇教练，机智如我！", "公开页面原始简介")])
        self.assertEqual(parse_youku_search(body, SOURCE), [])

    def test_generic_anchor_title_and_nested_heading_still_work(self):
        body = '''<a href="//v.youku.com/video?vid=XNTE2MjAwMDIzMg%3D%3D"><h3 title="真实标题">123次播放</h3></a>
                  <a href="//m.youku.com/video/id_XNDA3NjU2NTI4MA==.html"><img alt="图片说明标题"></a>'''
        self.assertEqual([row[1] for row in parse_youku_search(body, SOURCE)], ["真实标题", "图片说明标题"])

    def test_anchor_description_does_not_inherit_unrelated_page_keywords(self):
        body = '''<body>沙雕动画搞笑电影
        <a title="健身动作" href="https://v.youku.com/v_show/id_XABCDE124.html">健身动作</a>
        <a href="https://other.example">电影大全</a></body>'''
        self.assertEqual(parse_youku_search(body, SOURCE)[0][2], "健身动作")

    def test_actual_home_undefined_and_explicit_preview_id_are_read_without_show_id_conversion(self):
        body = '''<script>window.__INITIAL_DATA__ = {"ads":{"PUM":undefined},"cards":[
        {"title":"undefined 也能笑","subTitle":"喜剧预告","tags":[{"text":{"title":"电影"}}],"action":{"type":"JUMP_TO_SHOW","value":"z123"},
         "previewInfo":{"vid":1646446874,"videoId":"XNjU4NTc4NzQ5Ng==","title":null}},
        {"title":"只有剧集ID","showId":"z987","previewInfo":{"videoId":null}}]};</script>'''
        rows = parse_youku_search(body, SOURCE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "https://v.youku.com/v_show/id_XNjU4NTc4NzQ5Ng==.html")
        self.assertEqual(rows[0][1], "undefined 也能笑（预告片段）")
        self.assertIn("喜剧预告 电影", rows[0][2])

    def test_empty_and_script_shell_are_reported_separately_from_readable_no_results(self):
        for body in ("", "  ", '<script src="app.js"></script>',
                     '<html><head><title>优酷</title></head><body><div id="root"></div></body></html>',
                     '<script type="application/json">not-json</script>'):
            with self.subTest(body=body), self.assertRaisesRegex(ValueError, "空"):
                parse_youku_search(body, SOURCE)
        self.assertEqual(parse_youku_search('<p>没有搜索结果</p>', SOURCE), [])

    def test_untrusted_hosts_collection_pages_and_script_expressions_never_become_videos(self):
        body = script({"rows": [
            {"title": "bad", "url": "https://v.youku.com.evil.test/v_show/id_XNTE2MjAwMDIzMg==.html"},
            {"title": "collection", "url": "https://list.youku.com/show/id_z123456.html"},
            {"title": "escape", "url": "https://v.youku.com/v_show/id_XN123%2F..%2Fbad.html"},
            {"title": "not-video-id", "id": "XN123456"},
        ]})+"<script>window.__INITIAL_DATA__ = runArbitraryCode();</script>"
        self.assertEqual(parse_youku_search(body, SOURCE), [])

    def test_typed_json_payload_and_missing_names_do_not_invent_title(self):
        body = '<script type="application/json">'+json.dumps({"items": [
            {"videoId": "XABCDE123"}, {"name": "可读取的标题", "videoId": "XABCDE124"}]})+'</script>'
        self.assertEqual(parse_youku_search(body, SOURCE), [("https://v.youku.com/v_show/id_XABCDE124.html", "可读取的标题", "")])

    def test_oversized_and_malformed_data_are_bounded(self):
        with self.assertRaisesRegex(ValueError, "上限"):
            parse_youku_search("x"*(3*1024*1024+1), SOURCE)


if __name__ == "__main__":
    unittest.main()
