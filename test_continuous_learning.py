"""Continuous scheduling tests with real durable discovery and no network."""
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from continuous_learning import ContinuousLearningManager, clean_config
from discovery_learning import DiscoveryManager


def wait_for(predicate, timeout=4):
    until = time.monotonic()+timeout
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("worker did not settle")


class Clock:
    def __init__(self):
        self.value = 1000.

    def __call__(self):
        return self.value

    def advance(self, value=1000):
        self.value += value


class ContinuousLearningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)/"project"
        self.clock = Clock()
        self.searches, self.preparations, self.managers, self.discoveries, self.releases = [], [], [], [], []

    def tearDown(self):
        for event in self.releases:
            event.set()
        for manager in self.managers:
            manager.close()
        for discovery in self.discoveries:
            discovery.close()
        for manager in self.managers:
            wait_for(lambda: not manager.busy)
            manager.tick()
        self.temporary.cleanup()

    def manager(self, *, searcher=None, prepare=None):
        def search(query, **kwargs):
            self.searches.append((query, kwargs["sites"], kwargs["category"]))
            index = len(self.searches)
            kwargs["report"]({"phase": "fetch", "url": "https://www.bing.com/search?q=fixture", "message": "实际来源测试"})
            return [{"title": f"实际候选 {index}", "url": f"https://www.bilibili.com/video/BV{index:010d}"}]
        discovery = DiscoveryManager(self.root, searcher=searcher or search)
        self.discoveries.append(discovery)
        def setup(root, *, report, stop):
            self.preparations.append(root)
            report({"phase": "vision-download", "model": "fixture", "message": "准备现有模型", "url": "http://127.0.0.1:11434/api/pull",
                    "completed": 10, "total": 100, "percent": 10})
            return {"visual_model": "fixture", "visual_enabled": True}
        manager = ContinuousLearningManager(self.root, discovery, prepare=prepare or setup, clock=self.clock,
                                            disk_usage=lambda _: SimpleNamespace(free=100*1024**3))
        self.managers.append(manager)
        return manager

    def prepared(self, manager, **config):
        manager.start(config or None)
        wait_for(lambda: not manager.busy)
        return manager.tick({"paused": True})

    def finish_round(self, manager):
        wait_for(lambda: not manager.discovery.busy)
        return manager.tick()

    def test_start_configures_once_then_continues_without_user_intervention(self):
        manager = self.manager()
        state = self.prepared(manager)
        self.assertTrue(state["models_ready"])
        events = manager.drain_events()
        self.assertEqual(len([row for row in events if row["type"] == "models-ready"]), 1)
        self.assertEqual(len(self.preparations), 1)
        self.assertFalse(manager.start())
        state = manager.tick()
        self.assertEqual(state["round_count"], 1)
        first = state["round_id"]
        state = self.finish_round(manager)
        self.assertEqual(state["next_run_in"], 60)
        for _ in range(10):
            manager.tick()
        self.assertEqual(manager.snapshot()["round_count"], 1)
        self.clock.advance(60)
        state = manager.tick()
        self.assertEqual(state["round_count"], 2)
        self.assertIn(first, state["owned_round_ids"])
        self.assertEqual(len(self.preparations), 1)

    def test_rotates_site_priority_and_keywords_and_keeps_three_sites(self):
        manager = self.manager()
        self.prepared(manager)
        for _ in range(4):
            manager.tick()
            self.finish_round(manager)
            self.clock.advance(60)
        animations = [row for row in self.searches if row[2] == "animation"]
        self.assertEqual([row[1][0] for row in animations], ["bilibili", "youku", "iqiyi", "bilibili"])
        self.assertTrue(all(set(row[1]) == {"bilibili", "youku", "iqiyi"} for row in animations))
        self.assertNotEqual(animations[0][0], animations[-1][0])

    def test_workload_and_space_pause_new_rounds_then_recover(self):
        manager = self.manager()
        self.prepared(manager)
        self.assertEqual(manager.tick({"pending_downloads": 4, "pending_learning": 4})["state"], "backpressure")
        self.assertEqual(manager.tick({"training_busy": True})["state"], "backpressure")
        self.assertEqual(manager.tick({"free_bytes": 1024})["state"], "disk-low")
        self.assertEqual(manager.snapshot()["round_count"], 0)
        self.assertEqual(manager.tick({"free_bytes": 10*1024**3})["round_count"], 1)

    def test_remaining_queue_capacity_limits_new_round_download_budget(self):
        manager = self.manager()
        self.prepared(manager)
        state = manager.tick({"pending_downloads": 7})
        row = manager.discovery.get_round(state["round_id"])
        self.assertEqual(row["config"]["max_total_downloads"], 1)

    def test_empty_and_failed_rounds_back_off_without_busy_loop(self):
        manager = self.manager(searcher=lambda *args, **kwargs: [])
        self.prepared(manager)
        for expected in (120, 240, 480, 900, 900):
            manager.tick()
            state = self.finish_round(manager)
            self.assertEqual(state["next_run_in"], expected)
            old = state["round_count"]
            manager.tick()
            self.assertEqual(manager.snapshot()["round_count"], old)
            self.clock.advance(expected)

    def test_setup_failure_does_not_start_discovery_and_can_retry(self):
        attempts = []
        def prepare(*args, **kwargs):
            attempts.append(True)
            if len(attempts) == 1:
                raise ValueError("模型下载尚未完成")
            return {"visual_model": "fixture"}
        manager = self.manager(prepare=prepare)
        state = self.prepared(manager)
        self.assertEqual(state["state"], "failed")
        self.assertFalse(manager.active)
        self.assertFalse(self.searches)
        state = self.prepared(manager)
        self.assertTrue(state["models_ready"])
        manager.tick()
        self.assertEqual(manager.snapshot()["round_count"], 1)

    def test_stop_during_preparation_discards_late_result_and_releases_lock(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def prepare(root, *, report, stop):
            entered.set()
            release.wait(3)
            report({"message": "不应恢复准备状态"})
            return {"visual_model": "late"}
        manager = self.manager(prepare=prepare)
        manager.start()
        self.assertTrue(entered.wait(1))
        manager.stop()
        release.set()
        wait_for(lambda: not manager.busy)
        state = manager.tick()
        self.assertFalse(state["models_ready"])
        self.assertFalse(self.searches)
        self.assertFalse(any(row["type"] == "models-ready" for row in manager.drain_events()))
        replacement = self.manager()
        self.prepared(replacement)
        self.assertTrue(replacement.active)

    def test_stop_blocks_future_rounds_and_pending_handoffs(self):
        manager = self.manager()
        self.prepared(manager)
        manager.tick()
        self.finish_round(manager)
        manager.stop()
        self.assertFalse(manager.discovery.pending_candidates())
        self.clock.advance(10000)
        manager.tick()
        self.assertEqual(manager.snapshot()["round_count"], 1)

    def test_reopen_does_not_auto_start_and_restores_round_ownership(self):
        manager = self.manager()
        self.prepared(manager)
        manager.tick()
        self.finish_round(manager)
        ids = manager.owned_round_ids
        manager.close()
        manager.tick()
        reopened = self.manager()
        self.assertEqual(reopened.snapshot()["state"], "paused")
        self.assertEqual(reopened.owned_round_ids, ids)
        self.assertFalse(reopened.active)
        self.assertEqual(len(self.preparations), 1)
        reopened.tick()
        self.assertEqual(len(self.searches), 2)
        self.prepared(reopened)
        self.assertTrue(reopened.discovery.pending_candidates())

    def test_cross_instance_owner_is_exclusive_and_observer_cannot_stop_it(self):
        owner, observer = self.manager(), self.manager()
        self.prepared(owner)
        with self.assertRaisesRegex(ValueError, "另一个窗口"):
            observer.start()
        observer.stop()
        self.assertTrue(owner.active)
        owner.tick()
        self.assertEqual(owner.snapshot()["round_count"], 1)

    def test_setup_progress_has_real_bytes_and_url_but_no_fake_overall_percent(self):
        manager = self.manager()
        self.prepared(manager)
        progress = next(row for row in manager.drain_events() if row["type"] == "preparation")
        self.assertEqual(progress["progress"], {"completed": 10, "total": 100, "percent": 10})
        self.assertTrue(progress["current_url"].endswith("/api/pull"))
        self.assertNotIn("percent", manager.snapshot())

    def test_search_progress_shows_actual_current_source_and_stop_keeps_late_results_out(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        url = "https://search.bilibili.com/all?keyword=fixture"
        def search(query, **kwargs):
            kwargs["report"]({"phase": "fetch", "url": url, "message": "读取当前公开来源"})
            entered.set()
            release.wait(3)
            return [{"title": "迟到结果", "url": "https://www.bilibili.com/video/BV0000000001"}]
        manager = self.manager(searcher=search)
        self.prepared(manager)
        manager.tick()
        self.assertTrue(entered.wait(1))
        state = manager.tick()
        self.assertEqual(state["current_url"], url)
        self.assertEqual(state["phase"], "fetch")
        manager.stop()
        release.set()
        wait_for(lambda: not manager.busy)
        manager.tick()
        self.assertFalse(manager.discovery.list_candidates())

    def test_history_restore_lock_failure_does_not_pretend_models_are_ready(self):
        manager = self.manager()
        with patch.object(manager.discovery, "resume_pending", side_effect=ValueError("fixture lock")):
            state = self.prepared(manager)
        self.assertFalse(state["models_ready"])
        self.assertEqual(state["state"], "failed")
        self.assertFalse(any(row["type"] == "models-ready" for row in manager.drain_events()))
        self.assertFalse(self.searches)

    def test_stop_and_close_release_idle_owner_even_if_state_cannot_be_saved(self):
        manager = self.manager()
        self.prepared(manager)
        with patch.object(manager, "_persist", side_effect=OSError("disk full")):
            state = manager.stop()
            manager.close()
        self.assertFalse(manager.active)
        self.assertIsNone(manager._lock)
        self.assertTrue(manager._closed)
        self.assertEqual(state["state"], "paused")
        self.assertIn("disk full", state["last_error"])
        replacement = self.manager()
        self.prepared(replacement)
        self.assertTrue(replacement.active)

    def test_discovery_database_stop_failure_does_not_keep_continuous_owner_active(self):
        manager = self.manager()
        self.prepared(manager)
        with patch.object(manager.discovery, "stop", side_effect=sqlite3.OperationalError("fixture sqlite full")):
            state = manager.stop()
        self.assertFalse(manager.active)
        self.assertIsNone(manager._lock)
        self.assertIn("sqlite full", state["last_error"])

    def test_invalid_config_is_rejected_and_start_always_targets_three_sites(self):
        for config in ({"interval_seconds": 0}, {"max_pending": True}, {"min_free_gib": -1}):
            with self.assertRaises(ValueError):
                clean_config(config)
        config = clean_config({"sites": ["youku"], "auto_download": False, "movie_enabled": False})
        self.assertEqual(config["sites"], ["bilibili", "youku", "iqiyi"])
        self.assertTrue(config["auto_download"])
        self.assertTrue(config["movie_enabled"])


if __name__ == "__main__":
    unittest.main()
