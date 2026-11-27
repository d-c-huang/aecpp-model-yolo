"""Continuous mode hands real persistent discovery/download/reference queues off."""
import threading
import time
import unittest
from unittest.mock import Mock, patch

from continuous_learning import ContinuousLearningManager
import test_discovery_app as fixtures
from test_online_jobs import wait_for


class ContinuousAppTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DiscoveryAppTests()
        self.fixture.setUp()
        self.s, self.root = self.fixture.s, self.fixture.root
        self.s.show_discovery = Mock()
        self.s.show_learning_dashboard = Mock()
        self.s.continuous_models_ready = False
        self.s.continuous_training_movies = set()
        self.s.continuous_training_run = None
        self.s.retired_continuous_managers = []
        self.s.temporal_learning_states = Mock(return_value={1:{"status":"completed","message":"fixture temporal complete"}})
        self.s.submit_movie_config = Mock(return_value=True)
        self.searches = []
        def search(query,category,provider,limit,**kwargs):
            self.searches.append((query,category,kwargs.get("sites")))
            return self.fixture.sources if category=="animation" else []
        self.s.discovery_manager._searcher = search
        self.clock = [1000.]
        self.prepare = Mock(return_value={"settings":{},"visual_model":"fixture-local"})
        self.manager = ContinuousLearningManager(self.root,self.s.discovery_manager,
            prepare=self.prepare,clock=lambda:self.clock[0])
        self.s.continuous_learning_manager = self.manager

    def tearDown(self):
        self.manager.close()
        wait_for(lambda:not self.manager.busy)
        self.manager.tick()
        self.fixture.tearDown()

    def tick(self):
        self.s.continuous_poll_at = self.s.discovery_poll_at = self.s.online_poll_at = 0.
        self.s.poll_continuous_learning()
        self.s.poll_discovery()
        self.s.poll_online()

    def until(self, predicate):
        def poll():
            self.tick()
            return predicate()
        wait_for(poll,timeout=10)

    def test_one_click_prepares_then_downloads_and_learns_with_durable_provenance(self):
        self.s.start_continuous_learning()
        self.until(lambda: bool(self.s.auto_learning_manager.list_jobs()) and
            self.s.auto_learning_manager.list_jobs()[0]["status"]=="completed")
        self.tick()
        self.assertTrue(self.s.continuous_models_ready)
        self.assertEqual(self.prepare.call_count,1)
        job = self.s.online_manager.list_jobs()[0]
        self.assertTrue(job["discovery"]["continuous"])
        self.assertTrue(job["learning_dispatched"])
        self.assertEqual(self.s.discovery_manager.list_candidates()[0]["state"],"completed")
        self.assertEqual(len(self.fixture.imported),1)
        self.assertIn("current_downloads",self.s.continuous_learning_status())

    def test_queues_resume_only_after_models_are_ready(self):
        entered, release = threading.Event(), threading.Event()
        def prepare(root,report,stop):
            entered.set()
            while not release.wait(.01):
                if stop():
                    raise ValueError("cancelled")
            return {"settings":{}}
        self.manager._prepare = prepare
        self.addCleanup(release.set)
        self.s.online_manager.pause()
        self.s.auto_learning_manager.pause()
        self.s.start_continuous_learning()
        self.assertTrue(entered.wait(2))
        self.tick()
        self.assertEqual(self.searches,[])
        self.assertTrue(self.s.online_manager.paused)
        self.assertTrue(self.s.auto_learning_manager.paused)
        release.set()
        self.until(lambda:self.s.continuous_models_ready)
        self.assertFalse(self.s.online_manager.paused)
        self.assertFalse(self.s.auto_learning_manager.paused)

    def test_stop_blocks_late_handoff_and_start_reuses_completed_video(self):
        self.s.start_continuous_learning()
        self.until(lambda:bool(self.s.online_manager.list_jobs()) and self.s.online_manager.list_jobs()[0]["status"]=="completed")
        self.s.stop_continuous_learning()
        row = self.s.online_manager.list_jobs()[0]
        self.assertFalse(self.s._continuous_handoff_allowed(row))
        self.assertTrue(self.s.online_manager.paused)
        self.assertTrue(self.s.auto_learning_manager.paused)
        self.assertFalse(self.manager.active)
        wait_for(lambda:not self.manager.busy)
        self.tick()
        self.s.start_continuous_learning()
        self.until(lambda:self.s.continuous_models_ready and bool(self.s.auto_learning_manager.list_jobs()) and
            self.s.auto_learning_manager.list_jobs()[0]["status"]=="completed")
        self.assertEqual(len(self.fixture.imported),1)

    def test_next_round_keeps_searching_without_redownloading_old_candidates(self):
        self.s.start_continuous_learning()
        self.until(lambda:bool(self.s.auto_learning_manager.list_jobs()) and
            self.s.auto_learning_manager.list_jobs()[0]["status"]=="completed")
        self.tick()
        self.clock[0] += 1000
        self.until(lambda:self.manager.snapshot()["round_count"]>=2 and not self.s.discovery_manager.busy)
        self.assertEqual(len(self.fixture.imported),1)
        self.assertGreaterEqual(len(self.searches),4)
        self.assertNotEqual(self.searches[0][2],self.searches[2][2])

    def test_reopening_does_not_resume_network_or_learning_handoffs(self):
        self.s.start_continuous_learning()
        self.until(lambda:self.manager.snapshot()["round_count"]==1 and not self.s.discovery_manager.busy)
        self.s.stop_continuous_learning()
        self.manager.tick()
        replacement = ContinuousLearningManager(self.root,self.s.discovery_manager,prepare=self.prepare)
        try:
            self.s.continuous_learning_manager = replacement
            before = len(self.searches)
            self.tick()
            self.assertEqual(len(self.searches),before)
            self.assertEqual(replacement.snapshot()["state"],"paused")
            self.assertFalse(replacement.active)
            for row in self.s.online_manager.list_jobs():
                self.assertFalse(self.s._continuous_handoff_allowed(row))
        finally:
            replacement.close()
            self.s.continuous_learning_manager = self.manager

    def test_stopping_only_cancels_movie_training_started_for_continuous_mode(self):
        self.s.start_continuous_learning()
        wait_for(lambda:not self.manager.busy)
        from pathlib import Path
        self.s.training = (None,1,"",Path("manual-run"),None)
        self.s.continuous_training_run = "automatic-run"
        self.s.stop_continuous_learning()
        self.assertFalse(self.s.training_stop.is_set())
        self.s.training = (None,1,"",Path("automatic-run"),None)
        self.s.stop_continuous_learning()
        self.assertTrue(self.s.training_stop.is_set())
        self.s.training = None

    def test_continuous_waiting_blocks_cache_changes_and_project_export(self):
        from cache_storage import automatic_learning_idle
        from portable_project import _idle_project
        self.s.start_continuous_learning()
        wait_for(lambda:not self.manager.busy)
        self.assertTrue(self.s.online_active())
        for factory in (lambda:automatic_learning_idle(self.root),lambda:_idle_project(self.root,None)):
            with self.assertRaisesRegex(ValueError,"持续自动学习"):
                with factory():
                    self.fail("active mode cannot permit migration")
        self.s.stop_continuous_learning()
        self.manager.tick()
        with automatic_learning_idle(self.root):
            pass
        with _idle_project(self.root,None):
            pass

    def test_close_project_cancels_setup_and_releases_retired_lock(self):
        entered = threading.Event()
        def prepare(root,report,stop):
            entered.set()
            while not stop():
                time.sleep(.005)
            raise ValueError("cancelled")
        self.manager._prepare = prepare
        self.s.start_continuous_learning()
        self.assertTrue(entered.wait(2))
        self.s.close_online_project()
        wait_for(lambda:not self.manager.busy)
        self.s.poll_continuous_learning()
        from online_jobs import _ProjectLock
        lock = _ProjectLock(self.root/".continuous-learning.lock")
        try:
            self.assertIsNotNone(lock.fd)
        finally:
            lock.close()

    def test_state_write_error_cannot_prevent_pausing_downstream_queues(self):
        with patch.object(self.manager,"stop",side_effect=OSError("fixture disk full")):
            self.assertFalse(self.s.stop_continuous_learning())
        self.assertTrue(self.s.online_manager.paused)
        self.assertTrue(self.s.auto_learning_manager.paused)
        self.assertFalse(self.s.continuous_models_ready)
        self.assertIn("fixture disk full",self.s.feed_status.set.call_args[0][0])

    def test_training_ownership_follows_submission_not_previous_movie_selection(self):
        from app import Studio
        self.s.start_continuous_learning()
        wait_for(lambda:not self.manager.busy)
        self.s.launch_training = Mock()
        self.s.latest_completed = Mock(return_value=None)
        cfg = {"weights":"","epochs":1,"device":"","sample_fps":1,"movie_ids":[1],"_continuous":True}
        with patch("movie_sequence_data.snapshot_sources",return_value={"movies":[{"id":1}]}):
            Studio.submit_movie_config(self.s,cfg,purpose="manual-train",review_profiles=False)
            self.assertFalse(self.s.launch_training.call_args.kwargs["continuous"])
            Studio.submit_movie_config(self.s,cfg,purpose="discovery-auto",review_profiles=False)
            self.assertTrue(self.s.launch_training.call_args.kwargs["continuous"])

    def test_mixed_training_batch_keeps_manual_work_when_continuous_stops(self):
        from app import Studio
        from pathlib import Path
        self.s.start_continuous_learning()
        wait_for(lambda:not self.manager.busy)
        self.s.training = (None,1,"",Path("manual-run"),None)
        self.s.feed_pending = True
        original = {"movies":[{"id":1}]}
        self.s.feed_snapshot = original
        self.s.feed_movie_settings = {"_continuous":False}
        self.s.feed = Mock()
        cfg = {"weights":"","epochs":1,"device":"","sample_fps":1,"movie_ids":[2]}
        with patch("movie_sequence_data.snapshot_sources",return_value={"movies":[{"id":2}]}), \
             patch("movie_sequence_data.fingerprint",return_value="fixture"):
            Studio.submit_movie_config(self.s,cfg,purpose="discovery-auto",review_profiles=False,merge_pending=True)
        self.assertFalse(self.s.movie_training_settings["_continuous"])
        self.s.stop_continuous_learning()
        self.assertTrue(self.s.feed_pending)
        self.assertIs(self.s.feed_snapshot,original)
        self.s.training = None

    def test_manual_model_changes_wait_until_continuous_mode_stops(self):
        self.s.start_continuous_learning()
        wait_for(lambda:not self.manager.busy)
        self.assertFalse(self.s.save_video_analysis_settings({},""))
        self.assertFalse(self.s.download_speech_model("tiny"))
        self.assertFalse(self.s.download_vision_model({}))

    def test_low_cache_space_pauses_real_download_queue_and_recovers(self):
        self.s.start_continuous_learning()
        self.until(lambda:self.s.continuous_models_ready)
        with patch("shutil.disk_usage",return_value=Mock(free=1024**3)):
            self.tick()
        self.assertTrue(self.s.online_manager.paused)
        self.assertEqual(self.s.continuous_learning_status()["state"],"disk-low")
        with patch("shutil.disk_usage",return_value=Mock(free=10*1024**3)):
            self.tick()
        self.assertFalse(self.s.online_manager.paused)
        self.assertFalse(self.s.continuous_disk_paused)

    def test_manual_pending_training_is_preserved_after_stopping_automatic_run(self):
        from pathlib import Path
        self.s.training = ("",1,"cpu",self.root/"automatic-run",None)
        self.s.continuous_training_run = "automatic-run"
        self.s.continuous_training_stop = True
        self.s.training_stop.set()
        self.s.training_dot = Mock()
        self.s.training_sample_fps = 1.
        self.s.training_movie_options = {"continuous":True}
        self.s.launch_training = Mock()
        self.s.feed_pending = True
        self.s.feed_snapshot = {"movies":[{"id":2}]}
        self.s.feed_movie_settings = {"_continuous":False}
        self.s.poll_training("train_error","自动学习已停止")
        self.s.launch_training.assert_called_once()
        self.assertFalse(self.s.launch_training.call_args.kwargs["continuous"])
        self.assertEqual(self.s.launch_training.call_args.kwargs["reviewed"]["movies"],[{"id":2}])


if __name__=="__main__":
    unittest.main()
