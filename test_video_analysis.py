"""Real movie, subtitle, HTTP model and persistent learning integration."""
from pathlib import Path
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import dataset
import subtitles
from auto_learning import AutoLearningManager, load_reference
from test_online_jobs import wait_for
from test_video_semantics import server
from video_analysis import analyse_video, load_settings, save_settings
from video_analysis_notes import list_references


@unittest.skipUnless(shutil.which("ffmpeg"),"FFmpeg generates a small local fixture")
class VideoAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)/"project"
        dataset.initialize(self.root)
        video = Path(self.temp.name)/"source.mkv"
        subprocess.run(["ffmpeg","-v","error","-f","lavfi","-i","testsrc2=size=96x64:rate=5:duration=6",
                        "-c:v","ffv1",str(video)],check=True,capture_output=True)
        self.movie,_ = dataset.import_movie(self.root,video,"time",lambda _:None)
        self.cues = [(0,1500,"欢迎来到这里"),(1700,3700,"现在我们开始说话"),(4000,5500,"一起开始吧")]
        self.managers = []

    def tearDown(self):
        for manager in self.managers:
            manager.close()
        for manager in self.managers:
            wait_for(lambda:not manager.busy)
        self.temp.cleanup()

    def manager(self):
        manager = AutoLearningManager(self.root)
        self.managers.append(manager)
        return manager

    def timed(self):
        subtitles.store_track(self.root,self.movie,"manual","人工字幕","fixture",self.cues)

    def test_embedded_model_pipeline_publishes_notes_frames_and_reviewable_board(self):
        self.timed()
        with server() as (url,calls):
            save_settings(self.root,{"semantic_enabled":True,"model":"fixture","base_url":url})
            with patch("video_transcription.transcribe_movie",side_effect=AssertionError("reuse real subtitles")):
                manager = self.manager()
                manager.enqueue([self.movie])
                wait_for(lambda:not manager.busy)
        row = manager.list_jobs()[0]
        self.assertEqual(row["status"],"completed",row["message"])
        self.assertEqual(row["example_count"],3)
        self.assertEqual(len([c for c in calls if c['path']=='/api/chat']),1)
        self.assertEqual(row["model_analysis"]["semantic_status"],"completed")
        notes = list_references(self.root,self.movie)
        self.assertEqual(len(notes),1)
        block = notes[0]["result"]["contentBlocks"][0]
        self.assertEqual(block["evidenceSegments"][0]["text"],self.cues[0][2])
        frame = block["highlights"][0]["keyframes"][0]
        self.assertTrue((self.root/frame["path"]).is_file())
        self.assertLessEqual(block["startMs"],frame["timeMs"])
        self.assertLess(frame["timeMs"],block["endMs"])
        boards = [json.loads(Path(path).read_text(encoding="utf-8")) for path in row["board_paths"]]
        summary = next(board for board in boards if board["provenance"].get("text_kind")=="analysis-summary")
        self.assertFalse(summary["provenance"]["is_original_dialogue"])
        self.assertFalse((self.root/"story_learning"/"director.json").exists())
        self.assertEqual(load_reference(self.root)["example_count"],3)

    def test_missing_subtitles_trigger_asr_and_preserve_exact_milliseconds(self):
        transcript = {"version":1,"provider":"faster-whisper","unit":"ms","durationMs":6000,"language":"zh",
            "segments":[{"startMs":start,"endMs":end,"text":text} for start,end,text in self.cues]}
        with patch("video_transcription.transcribe_movie",return_value=transcript) as transcribe:
            manager = self.manager()
            manager.enqueue([self.movie])
            wait_for(lambda:not manager.busy)
        transcribe.assert_called_once()
        self.assertEqual(manager.list_jobs()[0]["status"],"completed")
        with dataset.connect(self.root) as db:
            stored = [tuple(row) for row in db.execute("SELECT start_ms,end_ms,text FROM effective_subtitle_cues ORDER BY start_ms")]
        self.assertEqual(stored,self.cues)
        self.assertEqual(load_reference(self.root)["example_count"],3)

    def test_unavailable_model_keeps_real_timing_but_cannot_claim_full_completion(self):
        self.timed()
        with server(status=503) as (url,_):
            save_settings(self.root,{"semantic_enabled":True,"model":"fixture","base_url":url})
            manager = self.manager()
            manager.enqueue([self.movie])
            wait_for(lambda:not manager.busy)
            row = manager.list_jobs()[0]
            self.assertEqual(row["status"],"partial")
            self.assertIn("内容分析未完成",row["message"])
            self.assertEqual(load_reference(self.root)["example_count"],3)
            self.assertEqual(list_references(self.root),[])
            self.assertEqual(len(manager.retry([row["id"]])),1)
            wait_for(lambda:not manager.busy)

    def test_asr_error_is_actionable_and_not_fabricated_transcription(self):
        with patch("video_transcription.transcribe_movie",side_effect=ValueError("请下载语音模型")):
            manager = self.manager()
            manager.enqueue([self.movie])
            wait_for(lambda:not manager.busy)
        row = manager.list_jobs()[0]
        self.assertEqual(row["status"],"needs_input")
        self.assertIn("请下载语音模型",row["message"])
        self.assertEqual(row["example_count"],0)
        self.assertFalse(list_references(self.root))

    def test_pause_before_model_publication_never_saves_generated_reference(self):
        self.timed()
        stopped = threading.Event()
        class Interrupted(Exception):
            pass
        def check():
            if stopped.is_set():
                raise Interrupted()
        def report(phase,message):
            if phase=="understanding":
                stopped.set()
        with server() as (url,calls):
            save_settings(self.root,{"semantic_enabled":True,"model":"fixture","base_url":url})
            with self.assertRaises(Interrupted):
                analyse_video(self.root,self.movie,report,check)
        self.assertFalse(calls)
        self.assertEqual(list_references(self.root),[])

    def test_project_settings_never_persist_api_key_and_can_disable_model_work(self):
        value = save_settings(self.root,{"enabled":False,"api_key":"must-not-be-saved"})
        self.assertNotIn("api_key",value)
        self.assertNotIn("must-not-be-saved",(self.root/"video_analysis_settings.json").read_text(encoding="utf-8"))
        self.assertFalse(load_settings(self.root)["enabled"])
        with patch("video_transcription.transcribe_movie",side_effect=AssertionError("disabled")):
            self.assertEqual(analyse_video(self.root,self.movie)["timing_examples"],[])
        for value in ({"base_url":"http://example.com"},{"base_url":"https://user:secret@example.com"},
                      {"semantic_enabled":"true"}):
            with self.assertRaises(ValueError):
                save_settings(self.root,value)

    def test_asr_reuses_matching_settings_but_retranscribes_after_language_change(self):
        transcript = {"version":1,"provider":"faster-whisper","unit":"ms","durationMs":6000,"language":"zh",
            "segments":[{"startMs":start,"endMs":end,"text":text} for start,end,text in self.cues]}
        with patch("video_transcription.transcribe_movie",return_value=transcript) as transcribe:
            analyse_video(self.root,self.movie)
            analyse_video(self.root,self.movie)
            self.assertEqual(transcribe.call_count,1)
            save_settings(self.root,{"language":"en"})
            analyse_video(self.root,self.movie)
            self.assertEqual(transcribe.call_count,2)
            self.timed()
            save_settings(self.root,{"language":"zh","asr_model":"small"})
            analyse_video(self.root,self.movie)
            self.assertEqual(transcribe.call_count,2,"human subtitles remain preferred")

    def test_disabling_keyframes_reports_zero_actual_frames(self):
        self.timed()
        with server() as (url,_):
            save_settings(self.root,{"semantic_enabled":True,"model":"fixture","base_url":url,"keyframes":False})
            manager = self.manager()
            manager.enqueue([self.movie])
            wait_for(lambda:not manager.busy)
        row = manager.list_jobs()[0]
        self.assertEqual(row["status"],"completed")
        self.assertEqual(row["model_analysis"]["frame_count"],0)
        self.assertIn("0 张实际关键帧",row["message"])

    def test_source_change_during_keyframe_extraction_cannot_publish_notes(self):
        self.timed()
        def replace_source(source,*args,**kwargs):
            with Path(source).open("ab") as stream:
                stream.write(b"changed")
            return []
        with server() as (url,_):
            save_settings(self.root,{"semantic_enabled":True,"model":"fixture","base_url":url})
            with patch("video_semantics.extract_keyframes",side_effect=replace_source):
                with self.assertRaisesRegex(ValueError,"原片已变化"):
                    analyse_video(self.root,self.movie)
        self.assertEqual(list_references(self.root),[])

    def test_long_analysis_is_split_into_editable_content_blocks(self):
        self.timed()
        blocks = [{"blockId":f"b{i}","idx":i,"title":f"内容{i}","startMs":0,"endMs":5500,
                   "highlights":[{"highlightId":f"h{i}","idx":0,"text":"需要人工审核的分析。"*190,
                                  "startMs":0,"endMs":5500,"keyframes":[]}]} for i in range(60)]
        result = {"schemaVersion":"2026-02-06","contentBlocks":blocks}
        save_settings(self.root,{"semantic_enabled":True,"model":"fixture","keyframes":False})
        with patch("video_semantics.analyse_transcript",return_value=result):
            analysis = analyse_video(self.root,self.movie)
        self.assertEqual(analysis["model_analysis"]["semantic_status"],"completed")
        self.assertEqual(len(analysis["boards"]),61)
        from story_pipeline import validate_storyboard
        for board in analysis["boards"]:
            validate_storyboard(board)

    def test_semantic_notes_and_local_frames_survive_project_transfer(self):
        self.timed()
        with server() as (url,_):
            save_settings(self.root,{"semantic_enabled":True,"model":"fixture","base_url":url})
            analyse_video(self.root,self.movie)
        from portable_project import export_project,import_project
        archive = Path(self.temp.name)/"portable.zip"
        export_project(self.root,archive)
        restored = Path(self.temp.name)/"restored"
        import_project(archive,restored)
        note = list_references(restored)[0]
        frame = note["result"]["contentBlocks"][0]["highlights"][0]["keyframes"][0]
        self.assertTrue((restored/frame["path"]).is_file())
        self.assertEqual(note["review_status"],"unreviewed")

    def test_active_download_defers_commit_without_repeating_model_analysis(self):
        self.timed()
        from cache_storage import storage_lease
        with server() as (url,calls):
            save_settings(self.root,{"semantic_enabled":True,"model":"fixture","base_url":url})
            manager = self.manager()
            with storage_lease(self.root):
                manager.enqueue([self.movie])
                wait_for(lambda:manager.list_jobs()[0]["phase"]=="waiting-storage")
                self.assertEqual(len([c for c in calls if c['path']=='/api/chat']),1)
            wait_for(lambda:not manager.busy)
        self.assertEqual(manager.list_jobs()[0]["status"],"completed")
        self.assertEqual(len(list_references(self.root)),1)


if __name__=="__main__":
    unittest.main()
