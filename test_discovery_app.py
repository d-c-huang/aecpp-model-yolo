"""Discovery handoffs use real persistent queues and deterministic local analysis."""
import json
import tempfile
import threading
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import dataset
from app import Studio
from auto_learning import AutoLearningManager, load_reference
from discovery_learning import DiscoveryManager
from discovery_status import candidate_status
from movie_usage import learning_history
from online_jobs import OnlineJobManager
from test_auto_learning import analysis
from test_online_jobs import wait_for


class DiscoveryAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        dataset.initialize(self.root)
        s = self.s = Studio.__new__(Studio)
        s.root = self.root
        s.busy = s.closing = s.project_transfer = False
        s.training = s.cache_window = s.online_window = s.auto_learning_window = s.discovery_window = None
        s.discovery_usage = None
        s.training_kind = "movie-temporal"
        s.training_stop = threading.Event()
        s.training_cancel_version = 0
        s.movie_training_settings = s.pending_movie_learning = s.feed_snapshot = None
        s.feed_pending = False
        s.online_learning_batches = {}
        s.retired_online_managers = []
        s.online_refresh_pending = False
        s.online_poll_at = s.discovery_poll_at = 0.
        s.movies = {"fixture":{"id":1}}
        for key in ("online_button","feed_status","status","refresh_movies","refresh","training_status"):
            setattr(s,key,Mock())
        self.sources = [{"url":"https://www.bilibili.com/video/BV1234567890/","title":"动画样例",
                         "source_url":"https://search.bilibili.com/all?keyword=test","provider":"web","category":"animation"}]
        s.discovery_manager = DiscoveryManager(self.root,searcher=lambda *a,**kw:self.sources)
        self.imported = []
        def importer(root,url,mode,report,**kwargs):
            self.imported.append(url)
            path = root/"fixture.dat"
            path.write_bytes(b"deterministic video identity")
            movie = dataset.register_movie(root,path,mode,report)
            return {"movie_id":movie["id"],"title":"动画样例","path":str(path),"source_url":url}
        s.online_manager = OnlineJobManager(self.root,importer=importer)
        def analyser(root,movie,report,check):
            value = analysis(movie)
            with dataset.connect(root) as db:
                value["film_hash"] = db.execute("SELECT digest FROM movies WHERE id=?",(movie,)).fetchone()[0]
            return value
        s.auto_learning_manager = AutoLearningManager(self.root,analyser=analyser)

    def tearDown(self):
        managers = [self.s.discovery_manager,self.s.online_manager,self.s.auto_learning_manager]
        self.s.close_online_project()
        for manager in managers:
            if manager:
                wait_for(lambda:not manager.busy)
        self.temp.cleanup()

    def discover(self,**config):
        self.s.discovery_manager.start({"movie_enabled":False,**config})
        wait_for(lambda:not self.s.discovery_manager.busy)

    def tick(self):
        self.s.discovery_poll_at = self.s.online_poll_at = 0.
        self.s.poll_discovery()
        self.s.poll_online()

    def test_discovery_download_reference_completion_and_provenance(self):
        self.discover()
        self.tick()
        wait_for(lambda:not self.s.online_manager.busy)
        self.tick()
        wait_for(lambda:not self.s.auto_learning_manager.busy)
        self.tick()
        candidate = self.s.discovery_manager.list_candidates()[0]
        self.assertEqual(candidate["state"],"completed")
        self.assertEqual(candidate["learning"]["reference"]["status"],"completed")
        self.assertEqual(len(self.imported),1)
        self.assertTrue(load_reference(self.root)["usable"])
        job = self.s.online_manager.list_jobs()[0]
        self.assertEqual(job["discovery"]["source_url"],self.sources[0]["source_url"])
        self.assertTrue(job["learning_dispatched"])
        self.tick()
        self.assertEqual(len(self.imported),1)

    def test_storage_operation_defers_download_and_stop_revokes_pending(self):
        self.discover()
        self.s.project_transfer = True
        self.tick()
        self.assertFalse(self.s.online_manager.list_jobs())
        self.s.discovery_manager.stop()
        self.s.project_transfer = False
        self.tick()
        self.assertFalse(self.s.online_manager.list_jobs())

    def test_stop_during_handoff_cancels_new_unstarted_download(self):
        self.discover()
        original = self.s.discovery_manager.ack_candidate
        def stop_first(*args,**kwargs):
            self.s.discovery_manager.stop()
            return original(*args,**kwargs)
        with patch.object(self.s.discovery_manager,"ack_candidate",side_effect=stop_first):
            self.tick()
        self.assertEqual(self.imported,[])
        self.assertEqual(self.s.online_manager.list_jobs()[0]["status"],"cancelled")

    def test_explicit_retry_reuses_failed_download_record(self):
        original = self.s.online_manager._importer
        self.s.online_manager._importer = Mock(side_effect=ValueError("站点解析失败"))
        self.discover()
        self.tick()
        wait_for(lambda:not self.s.online_manager.busy)
        self.tick()
        row = self.s.discovery_manager.list_candidates()[0]
        self.assertEqual(row["state"],"failed")
        self.s.online_manager._importer = original
        self.s.queue_discovered([row["id"]])
        wait_for(lambda:not self.s.online_manager.busy)
        self.assertEqual(len(self.s.online_manager.list_jobs()),1)
        self.assertEqual(self.s.online_manager.list_jobs()[0]["status"],"completed")

    def test_movie_route_submits_both_learners_without_fabricated_profiles(self):
        self.sources[0]["category"] = "movie"
        self.discover(animation_enabled=False,movie_enabled=True)
        s = self.s
        s.temporal_learning_states = Mock(return_value={})
        s.movie_defaults = Mock(return_value={"weights":"","epochs":2,"device":"cpu","sample_fps":1})
        s.submit_movie_config = Mock(return_value=True)
        self.tick()
        wait_for(lambda:not s.online_manager.busy)
        self.tick()
        wait_for(lambda:not s.auto_learning_manager.busy)
        cfg = s.submit_movie_config.call_args.args[0]
        self.assertEqual(cfg["movie_ids"],[1])
        self.assertFalse(s.submit_movie_config.call_args.kwargs["review_profiles"])
        self.assertTrue(s.online_manager.list_jobs()[0]["learning_dispatched"])
        self.assertEqual(s.online_manager.list_jobs()[0]["learning_kind"],"story-and-movie")

    def test_combined_retry_does_not_refeed_active_movie(self):
        s = self.s
        s.learn_auto_movies = Mock(return_value=True)
        s.temporal_learning_states = Mock(return_value={1:{"status":"running"}})
        s.submit_movie_config = Mock()
        self.assertTrue(s.learn_discovered_movies([1],request_version=0))
        s.submit_movie_config.assert_not_called()

    def completed_movie_history(self):
        run = self.root/"runs"/"20261127-completed-movie"
        run.mkdir(parents=True,exist_ok=True)
        (self.root/"logs").mkdir(exist_ok=True)
        (run/"completed.json").write_text(json.dumps({"kind":"movie-temporal"}),encoding="utf-8")
        (self.root/"logs"/(run.name+"-train.json")).write_text(json.dumps({
            "kind":"movie-temporal","movie_ids":[1],"status":"completed"}),encoding="utf-8")
        self.s.learn_auto_movies = Mock(return_value=True)
        self.s.movie_defaults = Mock(return_value={})
        self.s.submit_movie_config = Mock(return_value=True)

    def test_movie_handoff_waits_for_first_history_scan(self):
        self.completed_movie_history()
        release = threading.Event()
        def delayed(*args,**kwargs):
            if not release.wait(2):
                raise RuntimeError("history fixture was not released")
            return learning_history(*args,**kwargs)
        try:
            with patch("movie_usage.learning_history",side_effect=delayed):
                self.assertFalse(self.s.learn_discovered_movies([1],request_version=0))
                self.assertFalse(self.s.discovery_usage.ready)
                self.s.submit_movie_config.assert_not_called()
                self.assertIn("读取已有电影学习记录",self.s.feed_status.set.call_args.args[0])
        finally:
            release.set()
            if self.s.discovery_usage is not None and self.s.discovery_usage.worker:
                self.s.discovery_usage.worker.join(2)

    def test_ready_history_does_not_refeed_completed_movie(self):
        self.completed_movie_history()
        self.s.temporal_learning_states()
        wait_for(lambda:self.s.discovery_usage.ready)
        self.assertEqual(self.s.temporal_learning_states()[1]["status"],"completed")
        self.assertTrue(self.s.learn_discovered_movies([1],request_version=0))
        self.s.submit_movie_config.assert_not_called()

    def test_first_history_scan_finishing_after_snapshot_does_not_refeed_movie(self):
        self.completed_movie_history()
        release = threading.Event()
        first = True
        original_states = self.s.temporal_learning_states
        def delayed(*args,**kwargs):
            if not release.wait(2):
                raise RuntimeError("history fixture was not released")
            return learning_history(*args,**kwargs)
        def finish_scan_after_old_snapshot():
            nonlocal first
            states = original_states()
            if first:
                first = False
                self.assertEqual(states,{})
                release.set()
                self.s.discovery_usage.worker.join(2)
                self.assertTrue(self.s.discovery_usage.ready)
            return states
        self.s.temporal_learning_states = finish_scan_after_old_snapshot
        try:
            with patch("movie_usage.learning_history",side_effect=delayed):
                self.assertTrue(self.s.learn_discovered_movies([1],request_version=0))
                self.s.submit_movie_config.assert_not_called()
        finally:
            release.set()
            if self.s.discovery_usage is not None and self.s.discovery_usage.worker:
                self.s.discovery_usage.worker.join(2)

    def test_completed_download_can_explicitly_reauthorize_paused_handoff(self):
        original = self.s.online_manager._importer
        def paused_result(*args,**kwargs):
            result = original(*args,**kwargs)
            self.s.online_manager.pause()
            return result
        self.s.online_manager._importer = paused_result
        self.discover()
        self.tick()
        wait_for(lambda:not self.s.online_manager.busy)
        job = self.s.online_manager.list_jobs()[0]
        self.assertEqual(job["status"],"completed")
        self.assertFalse(job["learn"])
        self.s.online_manager.request_learning([job["id"]],request_version=0)
        self.assertTrue(self.s.online_manager.list_jobs()[0]["learn"])
        self.s.auto_learning_recovery_pending = True
        self.tick()
        wait_for(lambda:not self.s.auto_learning_manager.busy)
        self.assertTrue(load_reference(self.root)["usable"])


class DiscoveryStatusTests(unittest.TestCase):
    def test_movie_needs_both_outcomes_and_missing_subtitles_stay_visible(self):
        candidate = {"learning_kind":"story-and-movie"}
        download = {"status":"completed","result":{"movie_id":1},"learning_dispatched":True,
                    "progress":100,"downloaded_bytes":1000,"total_bytes":1000}
        ref = {"status":"needs_input","message":"请补充字幕"}
        state = candidate_status(candidate,download,ref,{"status":"running","message":"第1轮训练"})
        self.assertEqual(state["state"],"learning")
        self.assertIn("请补充字幕",state["detail"])
        self.assertIsNone(state["progress"])
        self.assertIsNone(state["downloaded_bytes"])
        state = candidate_status(candidate,download,ref,{"status":"completed"})
        self.assertEqual(state["state"],"needs_input")
        self.assertEqual(candidate_status(candidate,download,{"status":"completed"},{"status":"completed"})["state"],"completed")
        self.assertEqual(candidate_status(candidate,download,{"status":"completed"})["state"],"paused")

    def test_progress_is_measured_and_learning_cancel_survives_download(self):
        job = {"status":"running","phase":"downloading","progress":23.5,"downloaded_bytes":235,"total_bytes":1000}
        state = candidate_status({},job)
        self.assertEqual(state["progress"],23.5)
        self.assertEqual(state["state"],"downloading")
        self.assertNotIn("progress",candidate_status({}, {"status":"running"}))
        job.update(status="completed",learning_cancelled=True)
        self.assertEqual(candidate_status({},job)["state"],"cancelled")


if __name__ == "__main__":
    unittest.main()
