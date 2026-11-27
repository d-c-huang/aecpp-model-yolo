"""Restricted downloads must not stall continuous learning or hide retryable faults."""
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from online_jobs import OnlineJobManager
from online_video import OnlineVideoError, _friendly_error, _run_download, classify_online_error


YOUKU_ERROR = "ERROR: [youku] X123: Youku server reported error -6004: 该账号检测到可疑违规行为，已限制播放"


def wait_idle(manager):
    deadline = time.monotonic() + 4
    while manager.busy and time.monotonic() < deadline:
        time.sleep(.01)
    if manager.busy:
        raise AssertionError("queue did not finish")


class ErrorClassificationTests(unittest.TestCase):
    def test_explicit_platform_restrictions_are_structured(self):
        cases = {
            YOUKU_ERROR: "youku_restricted_6004",
            "优酷限制了此视频的播放（错误 -6004），无法下载；可换用其他公开可播放的视频。": "youku_restricted_6004",
            "This video is DRM protected": "drm_protected",
            "此视频受 DRM 保护，无法读取": "drm_protected",
            "爱奇艺播放清单包含加密或 DRM，仅支持公开无加密视频": "drm_protected",
            "爱奇艺视频受会员、试看或 DRM 限制": "content_access_restricted",
            "This is a members-only video": "membership_required",
            "爱奇艺视频需要会员或播放权限": "membership_required",
            "爱奇艺返回时长短于检索记录，可能是试看内容": "membership_required",
            "This video is private": "private_video",
            "You don't have permission to view this video": "private_video",
            "This video is not available in your country": "geo_restricted",
            "Sign in to watch this video": "login_required",
        }
        for message, code in cases.items():
            with self.subTest(message=message):
                result = classify_online_error(message)
                self.assertEqual(result["error_code"], code)
                self.assertTrue(result["restricted"])
                self.assertFalse(result["retryable"])
                self.assertTrue(result["skip_reason"])

    def test_ambiguous_legacy_and_network_or_parser_errors_remain_retryable(self):
        cases = [
            "HTTP Error 403: Forbidden", "HTTP Error 401: Unauthorized",
            "HTTP Error 429: Too Many Requests", "Unable to extract video info",
            "Unable to load cookies from file", "Can't find any video",
            "视频来源需要登录或未允许当前请求。请使用可直接访问的链接。",
            "Unable to extract DRM information", "视频不受 DRM 保护，但解析失败",
            "This is not DRM protected, decoder failed", "Connection reset by peer",
            "request timeout; use --cookies-from-browser if needed",
            "[youku] Unable to parse https://example.org/error-6004",
            "permission denied: C:/cache/video.mp4", "未检测到可用字幕",
            "此视频不需要会员，但连接超时",
        ]
        for message in cases:
            with self.subTest(message=message):
                result = classify_online_error(message)
                self.assertFalse(result["restricted"])
                self.assertTrue(result["retryable"])
                self.assertEqual(result["skip_reason"], "")
        self.assertEqual(classify_online_error("network timeout")["error_code"], "network_error")
        self.assertEqual(classify_online_error("Unable to parse")["error_code"], "download_error")

    def test_job_records_prefer_valid_structured_error_and_reject_unknown_codes(self):
        result = classify_online_error({"error": "Display text changed", "error_detail": {"error_code": "drm_protected"}})
        self.assertTrue(result["restricted"])
        result = classify_online_error({"error": "timeout", "error_code": "unknown_custom", "restricted": True})
        self.assertEqual(result["error_code"], "network_error")
        self.assertFalse(result["restricted"])
        self.assertFalse(classify_online_error({"url": "https://[bad", "error": "parser failed"})["restricted"])
        self.assertFalse(classify_online_error({"error_code": ["bad"], "error": "timeout"})["restricted"])
        self.assertEqual(classify_online_error({"url": "https://v.youku.com/v_show/id_X123.html", "error": "error -6004"})["error_code"], "youku_restricted_6004")

    def test_friendly_messages_do_not_turn_ambiguous_cookies_or_drm_parse_into_restrictions(self):
        for message in ("Unable to extract DRM information", "Could not read cookies", "HTTP Error 403: Forbidden"):
            self.assertFalse(classify_online_error(_friendly_error(message))["restricted"])
        self.assertIn("-6004", _friendly_error(YOUKU_ERROR))
        self.assertNotIn("secret", _friendly_error("Failed https://example.org/video?secret=token"))

    def test_worker_response_retains_structured_error_despite_changed_display_message(self):
        class FakeProcess:
            def __init__(self, args, **kwargs):
                self.stdout, self.returncode = io.StringIO(""), 1
                Path(args[-1]).write_text(json.dumps({"error": "Display text changed", "error_code": "youku_restricted_6004"}), encoding="utf-8")
            def poll(self): return self.returncode
            def wait(self, **kwargs): return self.returncode
        with tempfile.TemporaryDirectory() as directory, patch("online_video.subprocess.Popen", FakeProcess), patch("online_video._ProcessTree"):
            with self.assertRaises(OnlineVideoError) as error:
                _run_download(Path(directory), {"url": "https://example.org/video"}, lambda text: None, None)
        self.assertEqual(str(error.exception), "Display text changed")
        self.assertEqual(error.exception.error_code, "youku_restricted_6004")
        self.assertTrue(classify_online_error(error.exception)["restricted"])


class ContinuousRestrictedQueueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="yingge-restricted-jobs-")
        self.root = Path(self.directory.name)
        self.managers = []

    def tearDown(self):
        for manager in self.managers:
            manager.close()
            wait_idle(manager)
        self.directory.cleanup()

    def manager(self, importer):
        manager = OnlineJobManager(self.root, importer=importer)
        self.managers.append(manager)
        return manager

    @staticmethod
    def success(root, url, mode, report, **kwargs):
        return {"movie_id": 1, "title": "公开素材", "path": str(root / "movie.mp4"), "source_url": url}

    @staticmethod
    def add_continuous(manager, url):
        job_id = manager.add(url, learning_kind="story-reference")["added"][0]
        manager.attach_discovery(job_id, {"continuous": True, "round_id": "round-1", "source_url": "https://example.org/search"})
        return job_id

    def test_restricted_then_network_then_success_continue_without_pausing_and_survive_reopen(self):
        calls = []
        def importer(root, url, mode, report, **kwargs):
            calls.append(url)
            if url.endswith("restricted"):
                raise ValueError(YOUKU_ERROR)
            if url.endswith("network"):
                raise ValueError("Connection reset by peer")
            return self.success(root, url, mode, report, **kwargs)
        manager = self.manager(importer)
        ids = [self.add_continuous(manager, "https://example.org/" + name) for name in ("restricted", "network", "success")]
        manager.start()
        wait_idle(manager)
        rows = {row["id"]: row for row in manager.list_jobs()}
        self.assertEqual([rows[i]["status"] for i in ids], ["skipped", "failed", "completed"])
        self.assertEqual(len(calls), 3)
        self.assertFalse(manager.paused)
        self.assertFalse(manager.has_pending)
        self.assertEqual(rows[ids[0]]["error_code"], "youku_restricted_6004")
        self.assertIn("-6004", rows[ids[0]]["skip_reason"])
        self.assertFalse(rows[ids[0]]["learn"])
        self.assertEqual(rows[ids[1]]["error_category"], "network")
        events = manager.drain_events()
        skipped = next(e for e in events if e["type"] == "skipped")
        self.assertEqual(skipped["job_id"], ids[0])
        self.assertEqual(skipped["skip_reason"], rows[ids[0]]["skip_reason"])
        self.assertEqual([e["job_id"] for e in events if e["type"] == "completed"], [ids[2]])
        manager.close()
        reopened = self.manager(importer)
        self.assertEqual(next(r for r in reopened.list_jobs() if r["id"] == ids[0])["status"], "skipped")
        reopened.start()
        wait_idle(reopened)
        self.assertEqual(len(calls), 3)
        self.assertFalse(reopened.add("https://example.org/restricted", learning_kind="story-reference")["added"])

    def test_manual_restricted_download_stays_failed_with_classification(self):
        def denied(*args, **kwargs): raise ValueError("This video is DRM protected")
        manager = self.manager(denied)
        manager.add("https://example.org/manual")
        manager.start()
        wait_idle(manager)
        row = manager.list_jobs()[0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_code"], "drm_protected")
        self.assertTrue(row["restricted"])
        self.assertFalse(any(e["type"] == "skipped" for e in manager.drain_events()))

    def test_explicit_retry_of_skipped_video_clears_error_and_can_complete(self):
        attempts = []
        def importer(root, url, mode, report, **kwargs):
            attempts.append(url)
            if len(attempts) == 1: raise OnlineVideoError("平台受限", classification={"error_code": "youku_restricted_6004"})
            return self.success(root, url, mode, report, **kwargs)
        manager = self.manager(importer)
        job_id = self.add_continuous(manager, "https://example.org/video")
        manager.start()
        wait_idle(manager)
        self.assertEqual(manager.list_jobs()[0]["status"], "skipped")
        # Older producers may have retained nested details; an explicit retry
        # must not make a later successful job appear restricted in summaries.
        with manager._db(write=True) as db:
            previous = manager._get(db, job_id)
            previous["error_detail"] = {"error_code": "youku_restricted_6004"}
            manager._save(db, previous)
        self.assertEqual(manager.retry([job_id]), [job_id])
        row = manager.list_jobs()[0]
        self.assertFalse(row["restricted"])
        self.assertFalse(classify_online_error(row)["restricted"])
        self.assertEqual(row["skip_reason"], "")
        manager.start()
        wait_idle(manager)
        row = manager.list_jobs()[0]
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["error_code"], "")
        self.assertTrue(row["learn"])
        self.assertIn("6004", row["last_skip"]["reason"])
        from learning_totals import summarize_learning
        totals = summarize_learning(manager.list_jobs(), [])
        self.assertEqual(totals["skipped_count"], 1)
        self.assertEqual(totals["video_count"], 0)

    def test_local_history_migration_is_idempotent_and_never_starts_network(self):
        def forbidden(*args, **kwargs): raise AssertionError("migration attempted network")
        manager = self.manager(forbidden)
        continuous_id = self.add_continuous(manager, "https://example.org/old")
        network_id = self.add_continuous(manager, "https://example.org/timeout")
        ambiguous_id = self.add_continuous(manager, "https://example.org/ambiguous")
        manual_id = manager.add("https://example.org/manual")["added"][0]
        errors = {continuous_id: _friendly_error(YOUKU_ERROR), network_id: "timeout", manual_id: YOUKU_ERROR,
                  ambiguous_id: "视频来源需要登录或未允许当前请求。请使用可直接访问的链接。"}
        with manager._db(write=True) as db:
            for job_id, error in errors.items():
                row = manager._get(db, job_id)
                row.update(status="failed", phase="failed", error=error, message=error)
                for key in ("error_code", "error_category", "restricted", "skip_reason", "retryable"):
                    row.pop(key, None)
                manager._save(db, row)
        self.assertEqual(manager.migrate_restricted_jobs(), [continuous_id])
        self.assertEqual(manager.migrate_restricted_jobs(), [])
        rows = {r["id"]: r for r in manager.list_jobs()}
        self.assertEqual(rows[continuous_id]["status"], "skipped")
        for job_id in (network_id, manual_id, ambiguous_id): self.assertEqual(rows[job_id]["status"], "failed")
        self.assertFalse(rows[ambiguous_id]["restricted"])
        self.assertFalse(manager.busy)

    def test_user_pause_wins_over_restriction_and_does_not_emit_skip(self):
        started = threading.Event()
        def importer(root, url, mode, report, *, stop, **kwargs):
            started.set()
            stop.wait(3)
            raise ValueError(YOUKU_ERROR)
        manager = self.manager(importer)
        self.add_continuous(manager, "https://example.org/video")
        manager.start()
        self.assertTrue(started.wait(2))
        manager.pause()
        wait_idle(manager)
        row = manager.list_jobs()[0]
        self.assertEqual(row["status"], "paused")
        self.assertEqual(row["error_code"], "")
        self.assertFalse(any(e["type"] == "skipped" for e in manager.drain_events()))


if __name__ == "__main__":
    unittest.main()
