"""Discovery queue tests never contact public services or download media."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from discovery_learning import DiscoveryManager, clean_config, canonical_url, video_site
from online_jobs import _ProjectLock


def wait_for(predicate, timeout=4):
    until = time.monotonic()+timeout
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("discovery worker did not finish")


def result(category="animation", number=0):
    url = f"https://www.bilibili.com/video/BV1xx411c7m{number}/" if category == "animation" else f"https://www.iqiyi.com/v_fixture{number}.html"
    return {"title": f"素材 {category} {number}", "url": url,
            "source_url": "https://www.bing.com/search?q=fixture", "description": "公开搜索结果"}


class DiscoveryManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)/"project"
        self.managers = []
        self.releases = []

    def tearDown(self):
        for event in self.releases:
            event.set()
        for manager in self.managers:
            manager.close()
        for manager in self.managers:
            wait_for(lambda: not manager.busy)
        self.temporary.cleanup()

    def manager(self, searcher=None):
        def search(query, *, category, provider, sites, limit, report, stop):
            report({"phase": "fetch", "url": "https://www.bing.com/search?q="+category,
                    "site": sites[0], "message": "正在获取搜索信息"})
            return [result(category, number) for number in range(limit)]
        manager = DiscoveryManager(self.root, searcher=searcher or search)
        self.managers.append(manager)
        return manager

    def complete(self, manager, **config):
        identifier = manager.start(config)
        wait_for(lambda: not manager.busy)
        return identifier

    def test_default_discovers_both_types_and_records_real_source_activity(self):
        manager = self.manager()
        round_id = self.complete(manager)
        state = manager.snapshot()
        self.assertEqual(state["rounds"][0]["status"], "completed")
        self.assertEqual(len(state["candidates"]), 6)
        self.assertEqual({row["category"] for row in state["candidates"]}, {"animation", "movie"})
        fetching = [row for row in state["events"] if row["phase"] == "fetch"]
        self.assertEqual(len(fetching), 2)
        self.assertTrue(all(row["query"] and row["url"].startswith("https://www.bing.com") for row in fetching))
        self.assertTrue(all(type(row["id"]) is int for row in state["events"]))
        self.assertTrue(manager.drain_events())
        self.assertFalse(manager.drain_events())
        self.assertTrue(manager.list_events())
        self.assertEqual(state["rounds"][0]["id"], round_id)

    def test_budget_claims_once_and_ack_stores_download_and_learning_state(self):
        manager = self.manager()
        self.complete(manager)
        claimed = manager.pending_candidates()
        self.assertEqual(len(claimed), 4)
        self.assertIn("movie", {row["category"] for row in claimed})
        self.assertFalse(manager.pending_candidates())
        item = claimed[0]
        self.assertTrue(manager.ack_candidate(item["id"], "download-1", learning_kind="story-reference"))
        self.assertTrue(manager.ack_candidate(item["id"], "download-1"))
        self.assertTrue(manager.set_candidate_state(item["id"], "downloading", "正在下载素材", progress={"percent": 25, "speed": 1024}))
        self.assertTrue(manager.set_candidate_state(item["id"], "learning", "正在学习字幕节奏", movie_id=7,
                                                     learning={"phase": "analysing", "message": "读取第 30 秒"}))
        current = next(row for row in manager.list_candidates() if row["id"] == item["id"])
        self.assertEqual(current["online_job_id"], "download-1")
        self.assertEqual(current["movie_id"], 7)
        self.assertEqual(current["learning"]["phase"], "analysing")
        self.assertFalse(manager.set_candidate_state(item["id"], "learning", "正在学习字幕节奏", movie_id=7,
                                                      learning={"phase": "analysing", "message": "读取第 30 秒"}))

    def test_cross_round_url_dedup_preserves_original_provenance(self):
        manager = self.manager()
        original = self.complete(manager, auto_download=False)
        self.complete(manager, auto_download=False)
        self.assertEqual(len(manager.list_candidates()), 6)
        self.assertEqual(manager.list_rounds()[0]["candidate_count"], 0)
        self.assertEqual(manager.list_rounds()[0]["duplicate_count"], 6)
        self.assertEqual({row["round_id"] for row in manager.list_candidates()}, {original})
        self.assertEqual(canonical_url(result()["url"]+"?utm_source=test"), canonical_url(result()["url"]))

    def test_duplicate_candidates_do_not_use_new_candidate_limit(self):
        calls = []
        def search(*args, **kwargs):
            calls.append(True)
            return [result()] if len(calls) == 1 else [result(), result(number=1), result(number=2)]
        manager = self.manager(search)
        self.complete(manager, movie_enabled=False, max_per_category=1)
        second = self.complete(manager, movie_enabled=False, max_per_category=1)
        row = manager.get_round(second)
        self.assertEqual(row["candidate_count"], 1)
        self.assertEqual(row["duplicate_count"], 1)
        self.assertEqual(len(manager.list_candidates()), 2)

    def test_only_selected_platforms_single_videos_can_enter_results(self):
        def bad(query, **kwargs):
            return [result("movie"), {**result(), "url": "https://www.bilibili.com.evil.test/video/BV1xx411c7m0/"},
                    {**result(), "url": "https://www.bilibili.com/search/"}, result()]
        manager = self.manager(bad)
        self.complete(manager, movie_enabled=False, sites=["bilibili"], max_per_category=10)
        self.assertEqual(len(manager.list_candidates()), 1)
        self.assertEqual(manager.list_candidates()[0]["site"], "bilibili")
        with self.assertRaises(ValueError):
            video_site("https://archive.org/details/anything")

    def test_search_video_metadata_survives_reopen_and_pending_handoff(self):
        item = result("movie")
        metadata = {"site": "iqiyi", "url": item["url"], "tvid": "123456789", "vid": "a" * 32,
                    "duration": 12.5, "title": item["title"]}
        manager = self.manager(lambda *args, **kwargs: [{**item, "video_metadata": metadata}])
        self.complete(manager, animation_enabled=False)
        self.assertEqual(manager.list_candidates()[0]["video_metadata"], metadata)
        manager.close()
        reopened = self.manager()
        self.assertEqual(reopened.list_candidates()[0]["video_metadata"], metadata)
        reopened.resume_pending([reopened.list_rounds()[0]["id"]])
        self.assertEqual(reopened.pending_candidates()[0]["video_metadata"], metadata)

    def test_invalid_search_metadata_keeps_candidate_and_reports_fallback(self):
        item = result("movie")
        invalid = {"site": "iqiyi", "url": "https://www.iqiyi.com/v_other.html", "tvid": "123456789",
                   "vid": "a" * 32, "duration": 12.5, "title": item["title"]}
        manager = self.manager(lambda *args, **kwargs: [{**item, "video_metadata": invalid}])
        self.complete(manager, animation_enabled=False)
        candidate = manager.list_candidates()[0]
        self.assertEqual(candidate["url"], item["url"])
        self.assertNotIn("video_metadata", candidate)
        self.assertIn("不匹配", candidate["metadata_warning"])
        warning = next(event for event in manager.list_events() if event["type"] == "warning")
        self.assertEqual(warning["candidate_id"], candidate["id"])
        self.assertIn("使用原网页解析", warning["message"])
        self.assertEqual(manager.list_rounds()[0]["status"], "completed")

    def test_oversized_search_metadata_does_not_fail_remaining_results(self):
        item = result("movie")
        manager = self.manager(lambda *args, **kwargs: [{**item, "video_metadata": {str(i): i for i in range(41)}},
                                                       result("movie", 1)])
        self.complete(manager, animation_enabled=False)
        self.assertEqual(len(manager.list_candidates()), 2)
        self.assertEqual(manager.list_rounds()[0]["status"], "completed")
        candidate = next(row for row in manager.list_candidates() if row["url"] == item["url"])
        self.assertNotIn("video_metadata", candidate)
        self.assertIn("过大", candidate["metadata_warning"])

    def test_stop_cooperates_with_network_and_blocks_late_handoff(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def search(query, **kwargs):
            entered.set()
            release.wait(3)
            return [result()]
        manager = self.manager(search)
        manager.start({})
        self.assertTrue(entered.wait(1))
        manager.stop()
        self.assertTrue(manager.busy)
        self.assertFalse(manager.pending_candidates())
        release.set()
        wait_for(lambda: not manager.busy)
        self.assertFalse(manager.list_candidates())
        self.assertEqual(manager.list_rounds()[0]["status"], "stopped")

    def test_stopping_after_discovery_cancels_unhanded_candidates(self):
        manager = self.manager()
        self.complete(manager)
        claim = manager.pending_candidates()[0]
        manager.stop()
        self.assertFalse(manager.pending_candidates())
        self.assertFalse(manager.ack_candidate(claim["id"], "late-download"))
        self.assertTrue(all(row["state"] == "cancelled" for row in manager.list_candidates()))
        manager.request_download([claim["id"]])
        self.assertEqual([row["id"] for row in manager.pending_candidates()], [claim["id"]])

    def test_manual_selection_only_and_budget_error_are_explicit(self):
        manager = self.manager()
        self.complete(manager, auto_download=False, max_total_downloads=1)
        candidates = manager.list_candidates()
        self.assertFalse(manager.pending_candidates())
        with self.assertRaisesRegex(ValueError, "下载名额"):
            manager.request_download([row["id"] for row in candidates[:2]])
        self.assertFalse(manager.pending_candidates())
        manager.request_download([candidates[0]["id"]])
        self.assertEqual(len(manager.pending_candidates()), 1)

    def test_search_failure_is_visible_and_other_category_can_complete(self):
        def search(query, **kwargs):
            kwargs["report"]({"phase": "fetch", "url": "https://www.bing.com/search?q=failed",
                              "title": "当前搜索页面", "message": "查询来源"})
            if kwargs["category"] == "animation":
                raise OSError("搜索来源连接超时")
            return [result("movie")]
        manager = self.manager(search)
        self.complete(manager)
        row = manager.list_rounds()[0]
        self.assertEqual(row["status"], "partial")
        self.assertIn("动画", manager.get_config()["animation_query"])
        event = next(event for event in manager.list_events() if event["type"] == "error")
        self.assertEqual(event["url"], "https://www.bing.com/search?q=failed")
        self.assertEqual(event["title"], "当前搜索页面")
        self.assertEqual(event["provider"], "web")
        self.assertTrue(event["query"] and event["timestamp"])
        self.assertIn("超时", event["message"])

    def test_reopen_does_not_dispatch_until_explicit_resume(self):
        manager = self.manager()
        identifier = self.complete(manager)
        manager.close()
        resumed = self.manager()
        self.assertFalse(resumed.pending_candidates())
        self.assertFalse(resumed.busy)
        resumed.resume(identifier)
        wait_for(lambda: not resumed.busy)
        self.assertEqual(len(resumed.pending_candidates()), 4)

    def test_observer_cannot_recover_or_stop_another_live_search(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def search(query, **kwargs):
            entered.set()
            release.wait(3)
            return [result(kwargs["category"])]
        manager = self.manager(search)
        manager.start({})
        self.assertTrue(entered.wait(1))
        observer = self.manager()
        self.assertEqual(observer.list_rounds()[0]["status"], "searching")
        with self.assertRaisesRegex(ValueError, "其他窗口"):
            observer.start({})
        observer.close()
        release.set()
        wait_for(lambda: not manager.busy)
        self.assertEqual(manager.list_rounds()[0]["status"], "completed")

    def test_observer_open_after_search_does_not_steal_completed_handoffs(self):
        manager = self.manager()
        self.complete(manager)
        observer = self.manager()
        self.assertFalse(observer.pending_candidates())
        observer.close()
        self.assertEqual(len(manager.pending_candidates()), 4)

    def test_lock_blocks_start_and_log_is_bounded(self):
        manager = self.manager()
        lock = _ProjectLock(manager.folder/".worker.lock")
        try:
            with self.assertRaisesRegex(ValueError, "其他窗口"):
                manager.start({})
        finally:
            lock.close()
        with patch("discovery_learning.MAX_EVENTS", 5), manager._db(write=True) as db:
            for number in range(12):
                manager._event(db, type="fixture", phase="fetch", message=str(number))
        self.assertEqual(len(manager.list_events(20)), 5)

    def test_export_lock_blocks_idle_handoffs_and_state_writes(self):
        manager = self.manager()
        self.complete(manager)
        candidate = manager.list_candidates()[0]
        lock = _ProjectLock(manager.folder/".worker.lock")
        try:
            self.assertFalse(manager.pending_candidates())
            self.assertFalse(manager.set_candidate_state(candidate["id"], "failed", "must not write"))
            with self.assertRaisesRegex(ValueError, "其他窗口"):
                manager.request_download([candidate["id"]])
            self.assertEqual(manager.list_candidates()[0]["state"], "discovered")
        finally:
            lock.close()
        claimed = manager.pending_candidates()[0]
        lock = _ProjectLock(manager.folder/".worker.lock")
        try:
            self.assertFalse(manager.ack_candidate(claimed["id"], "locked-job"))
        finally:
            lock.close()
        self.assertTrue(manager.ack_candidate(claimed["id"], "accepted-job"))

    def test_learning_can_clear_completed_download_progress(self):
        manager = self.manager()
        self.complete(manager)
        candidate = manager.pending_candidates()[0]
        manager.set_candidate_state(candidate["id"], "downloading", progress=100, speed="1 MB/s", eta=0)
        manager.set_candidate_state(candidate["id"], "queued", "waiting")
        saved = next(row for row in manager.list_candidates() if row["id"] == candidate["id"])
        self.assertEqual(saved["progress"], 100)
        manager.set_candidate_state(candidate["id"], "learning", "正在学习", progress=None, speed=None, eta=None)
        saved = next(row for row in manager.list_candidates() if row["id"] == candidate["id"])
        self.assertIsNone(saved["progress"])
        self.assertIsNone(saved["speed"])
        self.assertIsNone(saved["eta"])

    def test_settings_and_status_reject_invalid_values(self):
        for config in ({"animation_enabled": False, "movie_enabled": False}, {"sites": []},
                       {"sites": ["youtube"]}, {"animation_provider": "archive"},
                       {"max_total_downloads": 1000}, {"auto_download": "yes"}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                clean_config(config)
        manager = self.manager()
        self.complete(manager)
        candidate = manager.pending_candidates()[0]
        with self.assertRaises(ValueError):
            manager.set_candidate_state(candidate["id"], "downloading", progress=float("nan"))
        manager.ack_candidate(candidate["id"], "one", id="tampered", url="https://example.test/")
        saved = next(row for row in manager.list_candidates() if row["id"] == candidate["id"])
        self.assertEqual(saved["url"], candidate["url"])


if __name__ == "__main__":
    unittest.main()
