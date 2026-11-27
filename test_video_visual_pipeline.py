"""Visual observations reach persistent jobs and actual novel generation."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import dataset
from auto_learning import AutoLearningManager
from test_online_jobs import wait_for
from test_video_semantics import server
from test_video_visual import response, observation
from video_analysis import analyse_video, save_settings, load_settings


@unittest.skipUnless(shutil.which("ffmpeg"),"FFmpeg supplies local video evidence")
class VisualPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/"project"
        dataset.initialize(self.root)
        self.source=Path(self.temp.name)/"source.mkv"
        subprocess.run(["ffmpeg","-v","error","-f","lavfi","-i","testsrc2=size=96x64:rate=5:duration=6",
                        "-c:v","ffv1",str(self.source)],check=True,capture_output=True)
        self.movie,_=dataset.import_movie(self.root,self.source,"train",lambda _:None)

    def settings(self,url,**changes):
        return save_settings(self.root,{"asr_enabled":False,"semantic_enabled":False,"visual_enabled":True,
            "visual_model":"fixture-vision","visual_max_clips":1,"base_url":url,**changes})

    def test_silent_video_publishes_visual_reference_and_changes_new_story(self):
        from video_visual_learning import list_visual_references
        from story_pipeline import generate_project_storyboard
        with server(value=response) as (url,calls):
            self.settings(url)
            manager=AutoLearningManager(self.root)
            try:
                manager.enqueue([self.movie])
                wait_for(lambda:not manager.busy,timeout=15)
                job=manager.list_jobs()[0]
            finally:
                manager.close()
                wait_for(lambda:not manager.busy)
        self.assertEqual(job["status"],"completed",job["message"])
        self.assertEqual(job["model_analysis"]["visual_frame_count"],3)
        self.assertEqual(job["example_count"],0)
        notes=list_visual_references(self.root,self.movie)
        self.assertEqual(len(notes),1)
        self.assertTrue(all((self.root/f["path"]).is_file() for f in notes[0]["clips"][0]["frames"]))
        board=generate_project_storyboard(self.root,observation()["description"])
        self.assertEqual(board["shots"][0]["camera"],"medium")
        self.assertFalse((self.root/"story_learning"/"director.json").exists())

    def test_missing_visual_capability_not_reported_as_learning(self):
        from video_visual_learning import list_visual_references
        with server(value={"capabilities":["completion"]}) as (url,calls):
            self.settings(url)
            result=analyse_video(self.root,self.movie)
        self.assertEqual(result["model_analysis"]["visual_status"],"needs_setup")
        self.assertEqual(list_visual_references(self.root),[])
        self.assertEqual([c["path"] for c in calls],["/api/show"])

    def test_live_evidence_is_persisted_before_model_returns_then_gets_labels(self):
        from learning_telemetry import dashboard_snapshot,read_history
        entered,release=threading.Event(),threading.Event()
        self.addCleanup(release.set)
        def delayed(path):
            if path=='/api/chat':entered.set();release.wait(10)
            return response(path)
        with server(value=delayed) as (url,calls):
            self.settings(url)
            manager=AutoLearningManager(self.root)
            try:
                dashboard_snapshot(self.root)
                manager.enqueue([self.movie])
                self.assertTrue(entered.wait(10))
                data=dashboard_snapshot(self.root)
                self.assertEqual(data['learning']['status'],'running')
                self.assertEqual(len(data['visual']['frames']),3)
                self.assertNotIn('observation',data['visual'])
                self.assertTrue(all((self.root/f['path']).is_file() for f in data['visual']['frames']))
                release.set();wait_for(lambda:not manager.busy,timeout=15)
                data=dashboard_snapshot(self.root)
                self.assertEqual(data['visual']['observation']['action'],'walk')
                self.assertEqual(data['totals']['visual_clip_count'],1)
                history=read_history(self.root)
                self.assertEqual((history[0]['visual_clip_count'],history[-1]['visual_clip_count']),(0,1))
            finally:
                release.set();manager.close();wait_for(lambda:not manager.busy)

    def test_cancel_before_visual_request_does_not_publish(self):
        from video_visual_learning import list_visual_references
        stopped=threading.Event()
        class Interrupted(Exception): pass
        def check():
            if stopped.is_set(): raise Interrupted()
        def report(phase,message):
            if phase=="visual-understanding": stopped.set()
        with server(value=response) as (url,calls):
            self.settings(url)
            with self.assertRaises(Interrupted):
                analyse_video(self.root,self.movie,report,check)
        self.assertFalse(calls)
        self.assertEqual(list_visual_references(self.root),[])

    def test_changed_video_during_vision_never_publishes_reference(self):
        from video_visual_learning import list_visual_references
        def report(phase,message):
            if "已理解" in message:
                with self.source.open("ab") as stream: stream.write(b"changed")
        with server(value=response) as (url,calls):
            self.settings(url)
            with self.assertRaises(ValueError):
                analyse_video(self.root,self.movie,report)
        self.assertEqual(list_visual_references(self.root),[])

    def test_visual_settings_bounded_and_credentials_not_saved(self):
        for value in (0,61,True,"12",float("nan")):
            with self.assertRaises(ValueError):
                save_settings(self.root,{"visual_max_clips":value})
        save_settings(self.root,{"visual_max_clips":7,"visual_model":"vision","api_key":"secret"})
        self.assertEqual(load_settings(self.root)["visual_max_clips"],7)
        self.assertNotIn("secret",(self.root/"video_analysis_settings.json").read_text(encoding="utf-8"))


if __name__=="__main__":
    unittest.main()
