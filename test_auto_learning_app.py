"""Automatic video learning routes independently of legacy movie training."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from app import Studio


class AutoLearningAppTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.studio = s = Studio.__new__(Studio)
        s.root = Path(self.temporary.name)
        s.busy = s.closing = s.project_transfer = False
        s.training = None
        s.training_cancel_version = 0
        s.cache_window = None
        s.auto_learning_manager = None
        s.auto_learning_window = None
        s.retired_auto_learning_managers = []
        s.online_window = None
        s.retired_online_managers = []
        s.online_learning_batches = {}
        s.online_refresh_pending = False
        s.online_poll_at = 0
        s.movies = {"fixture": {"id": 1}}
        for name in ("online_button", "feed_status", "status", "refresh_movies", "refresh",
                     "learn_imported_movies", "show_auto_learning"):
            setattr(s, name, Mock())
        s.online_manager = Mock(busy=False)
        s.online_manager.list_jobs.return_value = []
        s.online_manager.drain_events.return_value = []

    def batch(self, kind="story-reference"):
        row = {"id": "download", "batch_id": "batch", "status": "completed",
               "request_version": 0, "config": {"learn": True, "learning_kind": kind},
               "result": {"movie_id": 7}}
        s = self.studio
        s.online_manager.list_jobs.return_value = [row]
        s.online_manager.drain_events.return_value = [{"type": "completed", "job_id": "download",
            "learn": True, "request_version": 0, "result": {"movie_id": 7}}]
        return row

    def test_new_mode_dispatches_to_automatic_queue_not_gru(self):
        self.batch()
        s = self.studio
        s.learn_auto_movies = Mock(return_value=True)
        s.poll_online()
        s.learn_auto_movies.assert_called_once_with([7], request_version=0)
        s.learn_imported_movies.assert_not_called()

    def test_old_downloads_keep_their_original_task_kind(self):
        row = self.batch()
        del row["config"]["learning_kind"]
        s = self.studio
        s.learn_auto_movies = Mock()
        s.poll_online()
        s.learn_imported_movies.assert_called_once_with([7], request_version=0)
        s.learn_auto_movies.assert_not_called()

    def test_restart_recovers_only_unacknowledged_automatic_downloads(self):
        row = self.batch()
        s = self.studio
        s.online_manager.drain_events.return_value = []
        s.auto_learning_recovery_pending = True
        s.learn_auto_movies = Mock(return_value=True)
        s.poll_online()
        s.learn_auto_movies.assert_called_once_with([7], request_version=0)
        s.online_manager.acknowledge_learning.assert_called_once_with(["download"])
        for field in ("learning_dispatched", "learning_cancelled"):
            s.learn_auto_movies.reset_mock()
            row[field] = True
            s.auto_learning_recovery_pending = True
            s.online_poll_at = 0
            s.poll_online()
            s.learn_auto_movies.assert_not_called()
            row.pop(field)

    def test_transient_handoff_failure_is_retained_and_retried(self):
        self.batch()
        s = self.studio
        s.learn_auto_movies = Mock(side_effect=[OSError("temporary database failure"), True])
        s.poll_online()
        self.assertTrue(s.online_learning_batches)
        self.assertTrue(s.auto_handoff_retries)
        s.online_manager.drain_events.return_value = []
        s.online_poll_at = 0
        s.poll_online()
        self.assertEqual(s.learn_auto_movies.call_count, 1)
        for retry in s.auto_handoff_retries.values():
            retry["after"] = 0
        s.online_poll_at = 0
        s.poll_online()
        self.assertEqual(s.learn_auto_movies.call_count, 2)
        self.assertFalse(s.online_learning_batches)
        self.assertFalse(s.auto_handoff_retries)

    def test_storage_work_defers_automatic_handoff(self):
        self.batch()
        s = self.studio
        s.project_transfer = True
        s.learn_auto_movies = Mock(return_value=True)
        s.poll_online()
        s.learn_auto_movies.assert_not_called()
        self.assertTrue(s.online_learning_batches)
        s.project_transfer = False
        s.online_poll_at = 0
        s.online_manager.drain_events.return_value = []
        s.poll_online()
        s.learn_auto_movies.assert_called_once_with([7], request_version=0)

    def test_stopped_request_never_starts_auto_analysis(self):
        self.batch()
        s = self.studio
        s.training_cancel_version = 1
        s.learn_auto_movies = Mock()
        s.poll_online()
        s.learn_auto_movies.assert_not_called()

    def test_manual_enqueue_deduplicates_ids_and_does_not_require_movie_profiles(self):
        s = self.studio
        manager = Mock(busy=False)
        manager.enqueue.return_value = ["job"]
        s.auto_learning_manager = manager
        self.assertTrue(s.learn_auto_movies([7, 7], show_panel=True))
        manager.enqueue.assert_called_once_with([7])
        s.show_auto_learning.assert_called_once_with()
        s.learn_imported_movies.assert_not_called()

    def test_close_tracks_worker_until_it_has_really_stopped(self):
        s = self.studio
        manager = Mock(busy=True)
        s.auto_learning_manager = manager
        self.assertTrue(s.online_active())
        s.close_online_project()
        manager.close.assert_called_once_with()
        self.assertIsNone(s.auto_learning_manager)
        self.assertTrue(s.online_active())
        manager.busy = False
        s.poll_auto_learning()
        self.assertFalse(s.online_active())

    def test_automatic_progress_and_errors_surface_without_touching_tk_on_worker(self):
        s = self.studio
        s.auto_learning_manager = Mock(busy=False)
        s.auto_learning_manager.drain_events.return_value = [{"status": "failed", "message": "请补充字幕"}]
        s.poll_auto_learning()
        self.assertIn("请补充字幕", s.feed_status.set.call_args.args[0])

    def test_open_reference_rejects_another_projects_path(self):
        s = self.studio
        s.show_story = Mock()
        with self.assertRaisesRegex(ValueError, "当前工程"):
            s.open_auto_learning_board(s.root.parent/"other.json")
        s.show_story.assert_not_called()
        s.story_window = Mock()
        s.story_window.open_board.return_value = True
        self.assertTrue(s.open_auto_learning_board("auto_learning/draft.json"))
        s.story_window.open_board.assert_called_once_with(s.root/"auto_learning/draft.json")


if __name__ == "__main__":
    unittest.main()
