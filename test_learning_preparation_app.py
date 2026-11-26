from pathlib import Path
import queue
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

import dataset
import movie_profiles
from app import Studio


class PreparationAppTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        dataset.initialize(self.root)
        with dataset.connect(self.root) as db:
            db.execute("INSERT INTO movies(id,digest,name,mode) VALUES(1,'film-one','测试电影','time')")
        s = self.studio = Studio.__new__(Studio)
        s.root,s.closing,s.busy = self.root,False,False
        s.training,s.training_kind,s.training_cancel_version = None,"movie-temporal",0
        s.training_stop = threading.Event()
        s.cache_window,s.profile_window,s.profile_waiting = None,None,{}
        s.online_learning_batches,s.online_manager,s.retired_online_managers = {},None,[]
        s.movie_training_settings = {"weights":"","epochs":30,"device":"cpu","sample_fps":1.,"movie_ids":[1],
                                     "patience":7,"learning_rate":.002,"restart":False}
        s.pending_movie_learning,s.feed_movie_settings = None,None
        s.feed_pending,s.feed_snapshot = False,None
        s.events = queue.Queue()
        for name in ("show_movie_profiles","launch_training","feed_status","status","training_cancel","feed_button",
                     "update_capture_more","training_status","training_dot","project_button","train_button"):
            setattr(s,name,Mock())

    def confirm(self):
        return movie_profiles.save(self.root,1,{"title":"测试电影","cast":[{"role":"甲","actor":"演员甲"}],
                                                 "synopsis":"两位朋友寻找失物。"},confirm=True)

    def test_missing_profile_defers_freezing_and_confirm_resumes_once(self):
        s = self.studio
        self.assertEqual(s.submit_reviewed(),"waiting-profiles")
        s.show_movie_profiles.assert_called_once_with([1])
        s.launch_training.assert_not_called()
        self.confirm()
        s.resume_profile_learning()
        s.resume_profile_learning()
        s.launch_training.assert_called_once()
        call = s.launch_training.call_args
        self.assertEqual(call.kwargs["reviewed"]["movies"][0]["profile"]["status"],"confirmed")
        self.assertEqual(call.kwargs["patience"],7)
        self.assertEqual(call.kwargs["learning_rate"],.002)
        self.assertFalse(s.profile_waiting)

    def test_stop_cancels_preparation_without_losing_profile(self):
        s = self.studio
        s.submit_reviewed()
        s.stop_training()
        self.confirm()
        s.resume_profile_learning()
        s.launch_training.assert_not_called()
        self.assertTrue(movie_profiles.is_ready(movie_profiles.get(self.root,1)))

    def test_waiting_movie_request_keeps_configuration_when_other_task_starts(self):
        s = self.studio
        s.submit_reviewed()
        s.movie_training_settings = {**s.movie_training_settings,"epochs":80}
        s.training = ("detector.pt",5,"cpu",self.root/"active",None)
        s.training_kind = "annotations"
        self.confirm()
        s.resume_profile_learning()
        s.launch_training.assert_not_called()
        self.assertEqual(s.training_kind,"annotations")
        self.assertEqual(s.pending_movie_learning[0]["epochs"],30)
        self.assertEqual(s.pending_movie_learning[1]["movies"][0]["profile"]["status"],"confirmed")

    def test_cache_operation_keeps_ready_request_pending(self):
        s = self.studio
        s.submit_reviewed()
        self.confirm()
        s.cache_window = SimpleNamespace(busy=True)
        s.resume_profile_learning()
        self.assertTrue(s.profile_waiting)
        s.launch_training.assert_not_called()
        s.cache_window.busy = False
        s.resume_profile_learning()
        s.launch_training.assert_called_once()

    def test_multiple_auto_batches_waiting_for_profiles_are_all_fed(self):
        s = self.studio
        self.confirm()
        with dataset.connect(self.root) as db:
            db.execute("INSERT INTO movies(id,digest,name,mode) VALUES(2,'two','电影二','train')")
            db.execute("INSERT INTO movies(id,digest,name,mode) VALUES(3,'three','电影三','train')")
        s.training = ("",30,"cpu",self.root/"active",None)
        self.assertEqual(s.learn_imported_movies([2],request_version=0),"waiting-profiles")
        self.assertEqual(s.learn_imported_movies([3],request_version=0),"waiting-profiles")
        for identifier in (2,3):
            movie_profiles.save(self.root,identifier,{"title":f"电影{identifier}"},confirm=True,allow_missing=True)
        s.resume_profile_learning()
        self.assertFalse(s.profile_waiting)
        self.assertEqual([m["id"] for m in s.feed_snapshot["movies"]],[1,2,3])
        self.assertEqual(s.movie_training_settings["movie_ids"],[1,2,3])

    def test_launch_forwards_training_options_and_restart_drops_previous(self):
        s = self.studio
        self.confirm()
        from movie_sequence_data import snapshot_sources
        frozen = snapshot_sources(self.root,[1])
        s.run_worker = Mock(return_value="完成")
        def immediate(*,target,**kwargs):
            return SimpleNamespace(start=target)
        with patch("app.threading.Thread",side_effect=immediate):
            Studio.launch_training(s,"",80,"cpu",self.root/"old.pt",reviewed=frozen,kind="movie-temporal",
                                   sample_fps=.5,patience=9,learning_rate=.003,restart=True)
        args = s.run_worker.call_args.args[1]
        self.assertEqual(args[args.index("--epochs")+1],"80")
        self.assertEqual(args[args.index("--patience")+1],"9")
        self.assertEqual(args[args.index("--learning-rate")+1],"0.003")
        self.assertIn("--restart",args)
        self.assertNotIn("--previous",args)
        self.assertFalse(s.movie_training_settings["restart"])


if __name__=="__main__":
    unittest.main()
