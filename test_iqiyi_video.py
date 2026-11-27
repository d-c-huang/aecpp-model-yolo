import copy
import io
import unittest
from unittest.mock import patch

from iqiyi_video import IqiyiPublicVideoError, clean_video_metadata, extract_public_video


URL = "https://www.iqiyi.com/v_1wq38ds5n44.html"
VID = "af6630d00d5e22a43561083efdc44404"
HINT = {"site": "iqiyi", "url": URL, "tvid": "6964905246217000", "vid": VID,
        "duration": 10, "title": "搞笑短片"}
MANIFEST = b"#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\nhttps://data.video.qiyi.com/public.ts\n#EXT-X-ENDLIST\n"


def raw_response():
    return {"code": "A00000", "data": {"tvid": int(HINT["tvid"]), "vid": VID,
        "bossStatus": 0, "previewType": "", "prv": "", "hdcp": 0, "duration": 10,
        "vidl": [{"vd": 1, "screenSize": "280x504", "drmType": 1, "dr": -1,
                  "m3utx": "https://mus.video.iqiyi.com/public.m3u8"}]}}


class FakeYDL:
    def __init__(self, body=MANIFEST):
        self.body, self.calls = body, []

    def urlopen(self, url):
        self.calls.append(url)
        response = io.BytesIO(self.body)
        response.url = url
        return response


class IqiyiVideoTests(unittest.TestCase):
    def test_clean_metadata_strips_extra_fields_and_requires_exact_url(self):
        clean = clean_video_metadata(URL, {**HINT, "cookies": "secret", "download_url": "https://evil.test"})
        self.assertEqual(clean, HINT)
        for change in ({"url": URL + "?id=other"}, {"site": "youku"}, {"tvid": "123/../45"},
                       {"tvid": True}, {"vid": "short"}, {"duration": float("nan")},
                       {"duration": -1}, {"title": "bad\nheader"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                clean_video_metadata(URL, {**HINT, **change})
        for url in (URL.replace("https:", "http:"), URL + "?x=1", URL.replace("www", "WWW")):
            with self.assertRaises(ValueError):
                clean_video_metadata(url, {**HINT, "url": url})

    @patch("yt_dlp.extractor.iqiyi.IqiyiIE.get_raw_data")
    def test_invalid_metadata_never_requests_streams(self, get_raw):
        with self.assertRaises(ValueError):
            extract_public_video(FakeYDL(), URL, {**HINT, "url": URL + "?other"})
        get_raw.assert_not_called()

    @patch("yt_dlp.extractor.iqiyi.IqiyiIE.get_raw_data")
    def test_public_clear_stream_has_actual_portrait_dimensions(self, get_raw):
        get_raw.return_value = raw_response()
        ydl = FakeYDL()
        info = extract_public_video(ydl, URL, HINT)
        self.assertEqual(info["webpage_url"], URL)
        self.assertEqual(info["id"], VID)
        self.assertEqual(info["duration"], 10)
        self.assertEqual(info["formats"][0]["height"], 504)
        self.assertEqual(info["formats"][0]["width"], 280)
        self.assertFalse(info["formats"][0]["has_drm"])
        self.assertEqual(ydl.calls, ["https://mus.video.iqiyi.com/public.m3u8"])
        get_raw.assert_called_once_with(HINT["tvid"], VID)

    @patch("yt_dlp.extractor.iqiyi.IqiyiIE.get_raw_data")
    def test_outer_errors_and_membership_preview_drm_never_read_manifest(self, get_raw):
        for key, value in (("bossStatus", 1), ("prv", "1"), ("previewType", 1),
                           ("hdcp", 1), ("has_drm", True), ("drmType", 2),
                           ("duration", 3), ("vid", "0" * 32), ("tvid", 123)):
            raw = raw_response(); raw["data"][key] = value; get_raw.return_value = raw
            ydl = FakeYDL()
            with self.subTest(key=key), self.assertRaises(ValueError):
                extract_public_video(ydl, URL, HINT)
            self.assertFalse(ydl.calls)
        get_raw.return_value = {"code": "A00111"}
        with self.assertRaisesRegex(ValueError, "A00111"):
            extract_public_video(FakeYDL(), URL, HINT)

    @patch("yt_dlp.extractor.iqiyi.IqiyiIE.get_raw_data")
    def test_encrypted_manifest_is_rejected_without_fetching_key_or_media(self, get_raw):
        get_raw.return_value = raw_response()
        for method in (b'AES-128', b'SAMPLE-AES'):
            ydl = FakeYDL(MANIFEST.replace(b"#EXTINF", b'#EXT-X-KEY:METHOD=' + method + b',URI="https://keys.test/key"\n#EXTINF'))
            with self.assertRaisesRegex(ValueError, "加密"):
                extract_public_video(ydl, URL, HINT)
            self.assertEqual(len(ydl.calls), 1)

    @patch("yt_dlp.extractor.iqiyi.IqiyiIE.get_raw_data")
    def test_non_vod_large_and_invalid_manifests_are_rejected(self, get_raw):
        get_raw.return_value = raw_response()
        for body in (b"not a manifest", MANIFEST.replace(b"#EXT-X-ENDLIST", b""), b"a" * (512 * 1024 + 1)):
            with self.subTest(size=len(body)), self.assertRaises(ValueError):
                extract_public_video(FakeYDL(body), URL, HINT)

    @patch("yt_dlp.extractor.iqiyi.IqiyiIE.get_raw_data")
    def test_untrusted_stream_hosts_and_restricted_formats_are_skipped(self, get_raw):
        raw = raw_response(); valid = copy.deepcopy(raw["data"]["vidl"][0])
        raw["data"]["vidl"] = [{**valid, "m3utx": "https://127.0.0.1/private"},
                                  {**valid, "bossStatus": 1}, valid, valid]
        get_raw.return_value = raw
        ydl = FakeYDL(); info = extract_public_video(ydl, URL, HINT)
        self.assertEqual(len(info["formats"]), 1)
        self.assertEqual(len(ydl.calls), 1)

    @patch("yt_dlp.extractor.iqiyi.IqiyiIE.get_raw_data")
    def test_unknown_dimensions_are_omitted_not_invented(self, get_raw):
        raw = raw_response(); raw["data"]["vidl"][0].pop("screenSize")
        get_raw.return_value = raw
        info = extract_public_video(FakeYDL(), URL, HINT)
        self.assertNotIn("height", info["formats"][0])


if __name__ == "__main__":
    unittest.main()
