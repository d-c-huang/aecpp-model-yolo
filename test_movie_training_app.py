"""Regression checks for routing movie resources through the existing background queue."""
import json
from pathlib import Path
import tempfile
import queue
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app import Studio


class MovieTrainingAppTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.studio = Studio.__new__(Studio)
        self.studio.page_area = Mock()
        self.studio.activate_page = Mock()
        self.studio.root = self.root
        self.studio.training = None
        self.studio.training_kind = "movie-temporal"
        self.studio.movie_training_settings = None
        self.studio.pending_movie_learning = None
        self.studio.training_cancel_version = 0
        self.studio.launch_training = Mock()
        self.studio.online_manager = None
        self.studio.online_window = None
        self.studio.retired_online_managers = []
        self.studio.online_learning_batches = {}
        self.studio.online_refresh_pending = False
        self.studio.online_poll_at = 0.
        self.studio.online_button = Mock()
        self.studio.busy = False
        self.studio.closing = False
        self.studio.movies = {"原电影":{"id":1}}
        self.studio.refresh_movies = Mock()
        self.studio.refresh = Mock()
        self.studio.feed_status = Mock()
        self.studio.status = Mock()
        self.studio.default_weight = Mock(return_value="")
        self.studio.cache_window = None
        self.studio.profile_window = None
        self.studio.profile_waiting = {}
        self.studio.feed_movie_settings = None
        self.studio.training_movie_options = {}
        self.studio.training_cancel = Mock()
        self.studio.ensure_movie_profiles = Mock(return_value=True)

    def test_first_feed_starts_from_movies_without_reviewed_screenshots(self):
        sources = {"movies":[{"id":1}, {"id":2}]}
        with patch("movie_sequence_data.snapshot_sources", return_value=sources) as collect, \
             patch("film_learning.snapshot", side_effect=AssertionError("should not require screenshots")):
            self.studio.submit_reviewed()
        collect.assert_called_once_with(self.root, None)
        args, kwargs = self.studio.launch_training.call_args
        self.assertEqual(args[:3], ("", 30, ""))
        self.assertEqual(kwargs["kind"], "movie-temporal")
        self.assertIs(kwargs["reviewed"], sources)

    def test_feed_freezes_latest_movie_resources(self):
        studio = self.studio
        studio.training = ("", 3, "cpu", self.root/"run", None)
        studio.training_stop = SimpleNamespace(is_set=lambda:False)
        studio.feed_button = Mock()
        studio.feed_status = Mock()
        studio.movie_training_settings = {"movie_ids":[3]}
        sources = {"movies":[{"id":3, "subtitles":[{"text":"纠正后的台词"}]}]}
        with patch("movie_sequence_data.snapshot_sources", return_value=sources) as collect:
            studio.feed()
        collect.assert_called_once_with(self.root, [3])
        self.assertTrue(studio.feed_pending)
        self.assertIs(studio.feed_snapshot, sources)
        studio.feed_button.configure.assert_called_with(state="disabled")

    def test_latest_model_does_not_mix_detector_and_movie_runs(self):
        for name, output in (("movie", "movie_temporal.pt"), ("detector", "fit/weights/best.pt")):
            folder = self.root/"runs"/name
            (folder/output).parent.mkdir(parents=True)
            (folder/output).touch()
            metadata = {"kind":"movie-temporal"} if name=="movie" else {}
            (folder/"completed.json").write_text(json.dumps(metadata), encoding="utf-8")
        studio = self.studio
        self.assertEqual(studio.latest_completed(self.root,kind="movie-temporal").parent.name, "movie")
        self.assertEqual(studio.latest_completed(self.root,kind="annotations").parent.name, "detector")
        self.assertEqual(studio.latest_completed(self.root,detector=True).parent.name, "detector")
        (self.root/"runs/movie/movie_temporal.pt").unlink()
        self.assertEqual(studio.latest_completed(self.root,kind="annotations").parent.name, "detector")

    def test_completed_queue_retains_movie_mode_and_sampling(self):
        studio = self.studio
        run = self.root/"runs"/"finished"
        run.mkdir(parents=True)
        (run/"completed.json").write_text("{}", encoding="utf-8")
        studio.training = ("", 7, "cpu", run, None)
        studio.training_sample_fps = .5
        studio.training_stop = SimpleNamespace(is_set=lambda:False)
        studio.training_status = Mock()
        studio.training_dot = Mock()
        studio.feed_status = Mock()
        studio.feed_pending = True
        studio.feed_snapshot = {"movies":[{"id":4}]}
        studio.closing = False
        studio.poll_training("train_done", "完成")
        args, kwargs = studio.launch_training.call_args
        self.assertEqual(args, ("", 7, "cpu", run/"completed.json"))
        self.assertEqual(kwargs, {"reviewed":{"movies":[{"id":4}]}, "kind":"movie-temporal", "sample_fps":.5,
                                  "patience":5,"learning_rate":.001,"restart":False})
        self.assertFalse(studio.feed_pending)

    def test_online_import_adds_movie_to_active_scope_without_switching_task(self):
        studio = self.studio
        studio.training = ("weights.pt",5,"cpu",self.root/"active",None)
        studio.training_stop = SimpleNamespace(is_set=lambda:False)
        studio.movie_training_settings = {"weights":"weights.pt","epochs":5,"device":"cpu",
                                          "sample_fps":1.,"movie_ids":[1]}
        studio.feed = Mock()
        frozen = {"movies":[{"id":1},{"id":4}]}
        with patch("movie_sequence_data.snapshot_sources",return_value=frozen) as collect:
            studio.learn_imported_movie(4)
        collect.assert_called_once_with(self.root,[1,4])
        self.assertEqual(studio.training_kind,"movie-temporal")
        studio.feed.assert_called_once_with(reviewed=frozen)
        studio.launch_training.assert_not_called()

    def test_online_import_queues_movie_learning_after_detector(self):
        studio = self.studio
        studio.training = ("detector.pt",5,"cpu",self.root/"active",None)
        studio.training_kind = "annotations"
        studio.training_stop = SimpleNamespace(is_set=lambda:False)
        studio.feed_status = Mock()
        studio.default_weight = Mock(return_value="detector.pt")
        frozen = {"movies":[{"id":4}]}
        with patch("movie_sequence_data.snapshot_sources",return_value=frozen):
            studio.learn_imported_movie(4)
        self.assertEqual(studio.training_kind,"annotations")
        self.assertIs(studio.pending_movie_learning[1],frozen)
        studio.launch_training.assert_not_called()
        studio.training_status = Mock()
        studio.training_dot = Mock()
        studio.feed_pending = False
        studio.feed_snapshot = None
        studio.closing = False
        studio.poll_training("train_done","截图训练完成")
        self.assertIsNone(studio.pending_movie_learning)
        self.assertEqual(studio.launch_training.call_args.kwargs["kind"],"movie-temporal")
        self.assertIs(studio.launch_training.call_args.kwargs["reviewed"],frozen)

    def test_stopping_training_clears_both_queues(self):
        studio = self.studio
        studio.feed_pending = True
        studio.feed_snapshot = {"movies":[{"id":1}]}
        studio.pending_movie_learning = ({}, {"movies":[{"id":2}]})
        for attr in ("feed_status","feed_button","training_cancel","training_stop","training_status","update_capture_more"):
            setattr(studio,attr,Mock())
        studio.stop_training()
        self.assertFalse(studio.feed_pending)
        self.assertIsNone(studio.feed_snapshot)
        self.assertIsNone(studio.pending_movie_learning)
        studio.training_stop.set.assert_called_once()
        self.assertEqual(studio.training_cancel_version,1)

    def test_finished_download_does_not_restart_training_after_stop(self):
        studio = self.studio
        studio.training_cancel_version = 2
        studio.feed_status = Mock()
        with patch("movie_sequence_data.snapshot_sources",side_effect=AssertionError("auto learning was cancelled")):
            studio.learn_imported_movie(4,request_version=1)
        studio.launch_training.assert_not_called()
        self.assertIn("自动学习已取消",studio.feed_status.set.call_args.args[0])

    def test_import_followup_failure_does_not_stop_ui_event_polling(self):
        studio = self.studio
        studio.events = queue.Queue()
        studio.events.put(("done",("已导入在线视频", {"movie_id":1})))
        studio.after_success = Mock(side_effect=OSError("无法写入训练日志"))
        studio.status = Mock()
        studio.canvas = SimpleNamespace(enabled=False)
        studio.movie_picker = Mock()
        studio.cancel = Mock()
        studio.win = Mock()
        studio.closing = False
        with patch("app.messagebox.showerror") as error:
            studio.poll()
        self.assertFalse(studio.busy)
        self.assertTrue(studio.canvas.enabled)
        studio.win.after.assert_called_once_with(120,studio.poll)
        error.assert_called_once()

    def test_online_window_can_open_during_screenshot_task(self):
        studio = self.studio
        studio.win = Mock()
        studio.busy = True
        studio.start = Mock()
        with patch("online_jobs.OnlineJobManager") as manager, patch("online_window.OnlineWindow") as window:
            studio.import_online()
        manager.assert_called_once_with(self.root)
        self.assertIs(window.call_args.args[1],manager.return_value)
        studio.start.assert_not_called()
        self.assertTrue(studio.busy)

    def setup_online_batch(self, statuses):
        rows = [{"id":i,"batch_id":"batch-one","status":status,"request_version":0,
                 "config":{"learn":True},"result":{"movie_id":i+10} if status=="completed" else {}}
                for i,status in enumerate(statuses,1)]
        manager = Mock(busy=False)
        manager.list_jobs.return_value = rows
        manager.drain_events.return_value = []
        self.studio.online_manager = manager
        self.studio.learn_imported_movies = Mock()
        return manager, rows

    def test_online_batch_feeds_once_only_after_all_downloads_settle(self):
        studio = self.studio
        manager,rows = self.setup_online_batch(["completed","running"])
        manager.drain_events.return_value = [{"type":"completed","job_id":1,"result":{"movie_id":11},
                                              "learn":True,"request_version":0}]
        studio.poll_online()
        studio.learn_imported_movies.assert_not_called()
        studio.refresh_movies.assert_called_once_with()
        studio.refresh.assert_not_called()  # Preserve the active annotation and batch.
        rows[1].update(status="completed",result={"movie_id":12})
        manager.drain_events.return_value = [{"type":"completed","job_id":2,"result":{"movie_id":12},
                                              "learn":True,"request_version":0}]
        studio.online_poll_at = 0
        studio.poll_online()
        studio.learn_imported_movies.assert_called_once_with([11,12],request_version=0)
        self.assertEqual(studio.online_learning_batches,{})

    def test_paused_online_batch_waits_and_restored_members_join_feed(self):
        studio = self.studio
        manager,rows = self.setup_online_batch(["completed","paused"])
        manager.drain_events.return_value = [{"type":"completed","job_id":1,"result":{"movie_id":11},
                                              "learn":True,"request_version":0}]
        studio.poll_online()
        studio.learn_imported_movies.assert_not_called()
        # A reopened app may have lost earlier completion events, but the queue is persistent.
        studio.online_learning_batches.clear()
        rows[1].update(status="completed",result={"movie_id":12})
        manager.drain_events.return_value = [{"type":"completed","job_id":2,"result":{"movie_id":12},
                                              "learn":True,"request_version":0}]
        studio.online_poll_at = 0
        studio.poll_online()
        studio.learn_imported_movies.assert_called_once_with([11,12],request_version=0)

    def test_online_completion_cannot_restart_stopped_training(self):
        studio = self.studio
        manager,_ = self.setup_online_batch(["completed"])
        manager.drain_events.return_value = [{"type":"completed","job_id":1,"result":{"movie_id":11},
                                              "learn":True,"request_version":0}]
        studio.training_cancel_version = 1
        studio.poll_online()
        studio.learn_imported_movies.assert_not_called()
        self.assertIn("自动学习已取消",studio.feed_status.set.call_args.args[0])

    def test_manual_online_learning_uses_exact_selection(self):
        studio = self.studio
        studio.default_weight = Mock(return_value="")
        studio.movie_training_settings = {"weights":"","epochs":5,"device":"","sample_fps":1.,"movie_ids":None}
        with patch("movie_sequence_data.snapshot_sources",return_value={"movies":[{"id":8}]}) as collect:
            studio.learn_imported_movies([8,8],selected_only=True)
        collect.assert_called_once_with(self.root,[8])
        self.assertEqual(studio.movie_training_settings["movie_ids"],[8])

    def test_project_close_keeps_worker_tracked_until_shutdown(self):
        studio = self.studio
        manager = Mock(busy=True)
        studio.online_manager = manager
        window = Mock()
        window.win.winfo_exists.return_value = True
        studio.online_window = window
        studio.close_online_project()
        window.close.assert_called_once_with()
        manager.close.assert_called_once_with()
        self.assertIsNone(studio.online_manager)
        self.assertTrue(studio.online_active())
        manager.busy = False
        studio.poll_online()
        self.assertFalse(studio.online_active())

    def test_repeated_manual_feeds_keep_both_pending_selections(self):
        studio = self.studio
        for kind in ("movie-temporal","annotations"):
            with self.subTest(kind=kind):
                studio.training_kind = kind
                studio.training = ("",5,"cpu",self.root/"active",None)
                studio.training_stop = SimpleNamespace(is_set=lambda:False)
                studio.feed_button = Mock()
                studio.feed_pending = False
                studio.feed_snapshot = None
                studio.pending_movie_learning = None
                studio.movie_training_settings = {"weights":"","epochs":5,"device":"","sample_fps":1.,"movie_ids":None}
                snapshots = [{"version":1,"movies":[{"id":11,"note":"frozen"}],"fingerprint":"old"},
                             {"version":1,"movies":[{"id":12}],"fingerprint":"new"}]
                with patch("movie_sequence_data.snapshot_sources",side_effect=snapshots):
                    self.assertTrue(studio.learn_imported_movies([11],selected_only=True))
                    self.assertTrue(studio.learn_imported_movies([12],selected_only=True))
                pending = studio.feed_snapshot if kind=="movie-temporal" else studio.pending_movie_learning[1]
                self.assertEqual(pending["movies"],[{"id":11,"note":"frozen"},{"id":12}])
                self.assertEqual(studio.movie_training_settings["movie_ids"],[11,12])
                self.assertNotEqual(pending["fingerprint"],"new")

    def test_rejected_online_callbacks_report_failure_to_workbench(self):
        studio = self.studio
        studio.busy = True
        studio.status = Mock()
        self.assertIs(studio.open_online_movie(1),False)
        studio.refresh_movies.assert_not_called()
        studio.training = ("",5,"cpu",self.root/"active",None)
        studio.training_stop = SimpleNamespace(is_set=lambda:True)
        self.assertIs(studio.learn_imported_movies([1]),False)

    def test_completed_but_cancelled_member_is_excluded_from_batch_feed(self):
        studio = self.studio
        manager,rows = self.setup_online_batch(["completed","completed"])
        rows[0]["learn"] = False
        manager.drain_events.return_value = [{"type":"completed","job_id":2,"result":{"movie_id":12},
                                              "learn":True,"request_version":0}]
        studio.poll_online()
        studio.learn_imported_movies.assert_called_once_with([12],request_version=0)

    def test_new_batch_waits_for_stopping_training_to_exit(self):
        studio = self.studio
        manager,_ = self.setup_online_batch(["completed"])
        manager.drain_events.return_value = [{"type":"completed","job_id":1,"result":{"movie_id":11},
                                              "learn":True,"request_version":0}]
        studio.training = ("",5,"cpu",self.root/"active",None)
        studio.training_stop = SimpleNamespace(is_set=lambda:True)
        studio.poll_online()
        studio.learn_imported_movies.assert_not_called()
        self.assertTrue(studio.online_learning_batches)
        studio.training = None
        studio.online_poll_at = 0
        manager.drain_events.return_value = []
        studio.poll_online()
        studio.learn_imported_movies.assert_called_once_with([11],request_version=0)

    def test_online_batch_waits_for_cache_maintenance(self):
        studio = self.studio
        manager,_ = self.setup_online_batch(["completed"])
        manager.drain_events.return_value = [{"type":"completed","job_id":1,"result":{"movie_id":11},
                                              "learn":True,"request_version":0}]
        studio.cache_window = SimpleNamespace(busy=True)
        studio.poll_online()
        studio.learn_imported_movies.assert_not_called()
        self.assertTrue(studio.online_learning_batches)
        studio.cache_window.busy = False
        studio.online_poll_at = 0
        manager.drain_events.return_value = []
        studio.poll_online()
        studio.learn_imported_movies.assert_called_once_with([11],request_version=0)

    def test_failed_queued_training_keeps_polling_and_restores_controls(self):
        studio = self.studio
        studio.events = queue.Queue()
        studio.events.put(("train_done","完成"))
        studio.poll_training = Mock(side_effect=OSError("无法创建新日志"))
        studio.training_status = Mock()
        studio.busy = False
        studio.closing = False
        studio.win = Mock()
        for attr in ("project_button","train_button","feed_button","training_cancel"):
            setattr(studio,attr,Mock())
        with patch("app.messagebox.showerror"):
            studio.poll()
        studio.win.after.assert_called_once_with(120,studio.poll)
        studio.train_button.configure.assert_called_once_with(state="normal")


if __name__ == "__main__":
    unittest.main()
