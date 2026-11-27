"""Real durable queues: restriction -> replacement search -> learned results."""
import unittest
from unittest.mock import patch, Mock

from app import Studio
import test_continuous_app as fixtures
from test_online_jobs import wait_for


class ContinuousSkipRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.base = fixtures.ContinuousAppTests()
        self.base.setUp()
        self.addCleanup(self.base.tearDown)
        self.s, self.controller = self.base.s, self.base.manager
        self.good = dict(self.base.fixture.sources[0])
        self.bad = {"url":"https://v.youku.com/v_show/id_XN123456==.html",
                    "title":"优酷受限样例", "source_url":"https://so.youku.com/search_video/q_test",
                    "provider":"web", "category":"animation"}
        self.base.fixture.sources = [self.bad]
        self.attempts = []
        original = self.s.online_manager._importer
        def importer(root,url,mode,report,**kwargs):
            self.attempts.append(url)
            if "youku.com" in url:
                raise ValueError("[youku] Youku server reported error -6004: restricted playback")
            return original(root,url,mode,report,**kwargs)
        self.s.online_manager._importer = importer

    def skipped(self):
        return any(r["state"]=="skipped" for r in self.s.discovery_manager.list_candidates())

    def test_restricted_video_releases_queue_and_new_search_learns_once(self):
        self.s.start_continuous_learning()
        self.base.until(self.skipped)
        self.base.tick()
        self.assertTrue(self.controller.active)
        self.assertFalse(self.s.online_manager.paused)
        self.assertTrue(self.controller.snapshot()["replacement_pending"])
        self.assertEqual(self.s.auto_learning_manager.list_jobs(),[])
        self.base.fixture.sources = [self.bad,self.good]
        first = self.controller.snapshot()["round_count"]
        self.base.clock[0] += 9
        self.base.tick()
        self.assertEqual(self.controller.snapshot()["round_count"],first)
        self.base.clock[0] += 1
        self.base.until(lambda:any(r["state"]=="completed" for r in self.s.discovery_manager.list_candidates()))
        self.assertEqual(self.attempts.count(self.bad["url"]),1)
        self.assertEqual(self.attempts.count(self.good["url"]),1)
        self.assertEqual(self.base.searches[2][2][-1],"youku")
        totals = self.s._learning_totals(force=True)
        self.assertEqual(totals["skipped_count"],1)
        self.assertEqual(totals["video_count"],1)
        self.assertEqual(totals["subtitle_reference_count"],3)
        self.assertIn("6004",totals["latest_skip"]["reason"])
        self.controller.notify_download_success = Mock()
        automatic = self.s.auto_learning_manager
        with automatic._mutex, automatic._db(write=True) as db:
            row = automatic._get(db,automatic.list_jobs()[0]["id"])
            row["message"] = "已发布参考的显示信息更新"
            automatic._save(db,row)
        self.base.tick()
        self.controller.notify_download_success.assert_not_called()
        # Reopening the statistics is read-only and retains the same counts.
        reopened = Studio.__new__(Studio)
        reopened.root = self.base.root
        self.assertEqual(reopened._learning_totals(force=True)["skipped_count"],1)
        self.assertEqual(reopened._learning_totals(force=True)["video_count"],1)

    def test_skip_releases_current_round_slot_before_next_search(self):
        self.base.fixture.sources = [self.bad,self.good]
        self.controller.start({"max_total_downloads":1})
        self.base.until(lambda:any(r["state"]=="completed" for r in self.s.discovery_manager.list_candidates()))
        self.assertEqual(self.controller.snapshot()["round_count"],1)
        self.assertEqual(self.attempts,[self.bad["url"],self.good["url"]])
        skipped = next(r for r in self.s.discovery_manager.list_candidates() if r["state"]=="skipped")
        self.assertFalse(skipped["budget_reserved"])

    def test_stop_cancels_pending_replacement_without_losing_reason(self):
        self.s.start_continuous_learning()
        self.base.until(self.skipped)
        self.s.stop_continuous_learning()
        count = len(self.base.searches)
        self.base.clock[0] += 1000
        self.base.tick()
        self.assertEqual(len(self.base.searches),count)
        self.assertFalse(self.controller.snapshot()["replacement_pending"])
        self.assertEqual(self.s._learning_totals(force=True)["skipped_count"],1)

    def test_replacement_respects_backpressure_and_is_not_scheduled_per_poll(self):
        self.s.start_continuous_learning()
        self.base.until(self.skipped)
        job = self.s.online_manager.list_jobs()[0]
        self.assertFalse(self.controller.notify_skipped(job["id"],site="youku"))
        self.base.clock[0] += 10
        count = self.controller.snapshot()["round_count"]
        self.assertEqual(self.controller.tick({"pending_learning":8})["state"],"backpressure")
        self.assertEqual(self.controller.snapshot()["round_count"],count)
        self.assertEqual(self.controller.tick({"free_bytes":0})["state"],"disk-low")
        self.assertEqual(self.controller.snapshot()["round_count"],count)
        self.controller.tick({"free_bytes":10*1024**3})
        wait_for(lambda:not self.s.discovery_manager.busy)
        self.assertEqual(self.controller.snapshot()["round_count"],count+1)

    def test_all_windows_share_cached_read_only_totals(self):
        self.s._learning_totals(force=True)
        with patch("learning_totals.summarize_learning",side_effect=AssertionError("cached snapshot expected")):
            for _ in range(5):
                self.assertTrue(self.s.continuous_learning_status()["totals"]["ready"])
        with patch("sqlite3.connect",side_effect=__import__('sqlite3').OperationalError("database is locked")):
            totals = self.s._learning_totals(force=True)
        self.assertFalse(totals["ready"])
        self.assertIn("暂未刷新",totals["message"])
        self.assertTrue(self.s._learning_totals(force=True)["ready"])
