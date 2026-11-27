"""Check the explicit movie binding and the real transcript-to-learning path."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from app import Studio
from auto_learning import AutoLearningManager, load_reference
import dataset
from test_online_jobs import wait_for
from video_helper_import import import_transcript


class VideoHelperAppTests(unittest.TestCase):
    def setUp(self):
        s = self.s = Studio.__new__(Studio)
        s.root = Path("fixture-project")
        s.win = Mock()
        s.closing = s.busy = False
        s.storage_busy = Mock(return_value=False)
        s.training_cancel_version = 2
        s.status = Mock()
        s.progress = Mock()
        s.start = Mock()
        self.manager = Mock()
        self.manager.list_jobs.return_value = [{"movie_id":7,"status":"needs_input","title":"所选影片"}]
        s.get_auto_learning_manager = Mock(return_value=self.manager)

    def test_explicit_import_runs_off_ui_thread_then_reanalyses_only_selected_movie(self):
        with patch("app.filedialog.askopenfilename",return_value="transcript.json") as choose, \
             patch("video_helper_import.import_transcript",return_value={"cue_count":3}) as importer:
            self.assertTrue(self.s.import_helper_transcript(7))
            importer.assert_not_called()
            task,completed = self.s.start.call_args.args
            completed(task())
        self.assertIn("所选影片",choose.call_args.kwargs["title"])
        importer.assert_called_once_with(self.s.root,7,"transcript.json",report=self.s.progress)
        self.manager.enqueue.assert_called_once_with([7],force=True)

    def test_cancelled_selection_and_active_jobs_do_not_import(self):
        with patch("app.filedialog.askopenfilename",return_value=""):
            self.assertFalse(self.s.import_helper_transcript(7))
        self.s.start.assert_not_called()
        self.manager.list_jobs.return_value[0]["status"] = "running"
        with self.assertRaisesRegex(ValueError,"完成或取消"):
            self.s.import_helper_transcript(7)

    def test_stop_while_importing_keeps_import_but_does_not_start_new_learning(self):
        with patch("app.filedialog.askopenfilename",return_value="transcript.json"):
            self.s.import_helper_transcript(7)
        self.s.training_cancel_version += 1
        self.s.start.call_args.args[1]({"cue_count":3})
        self.manager.enqueue.assert_not_called()

    @unittest.skipUnless(shutil.which("ffmpeg"),"FFmpeg supplies a tiny real video fixture")
    def test_real_helper_transcript_changes_needs_input_to_learned_reference(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)/"project"
            dataset.initialize(root)
            video = Path(temporary)/"fixture.mkv"
            subprocess.run(["ffmpeg","-v","error","-f","lavfi","-i","testsrc2=size=96x64:rate=5:duration=6",
                            "-c:v","ffv1",str(video)],check=True,capture_output=True)
            movie,_ = dataset.import_movie(root,video,"time",lambda _:None)
            manager = AutoLearningManager(root)
            try:
                manager.enqueue([movie])
                wait_for(lambda:not manager.busy)
                self.assertEqual(manager.list_jobs()[0]["status"],"needs_input")
                source = Path(temporary)/"transcript.json"
                source.write_text(json.dumps({"version":1,"unit":"ms","provider":"faster-whisper","language":"zh",
                    "durationMs":6000,"segments":[
                        {"startMs":0,"endMs":1500,"text":"欢迎来到这里"},
                        {"startMs":1700,"endMs":3700,"text":"现在我们开始说话"},
                        {"startMs":4000,"endMs":5500,"text":"一起开始吧"}]},ensure_ascii=False),encoding="utf-8")
                import_transcript(root,movie,source)
                manager.enqueue([movie],force=True)
                wait_for(lambda:not manager.busy)
                self.assertEqual(manager.list_jobs()[0]["status"],"completed")
                self.assertEqual(load_reference(root)["example_count"],3)
                self.assertTrue(Path(manager.list_jobs()[0]["board_path"]).is_file())
            finally:
                manager.close()
                wait_for(lambda:not manager.busy)


if __name__ == "__main__":
    unittest.main()
