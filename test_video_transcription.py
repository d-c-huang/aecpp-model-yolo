"""Local speech extraction, actual timeline and explicit model download contracts."""
from io import BytesIO
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

import dataset
import video_transcription as transcription


def make_model(folder):
    folder.mkdir(parents=True,exist_ok=True)
    for name in ("config.json","model.bin","tokenizer.json","vocabulary.txt"):
        (folder/name).write_bytes(b"test model contract")
    return folder


class ModelAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.payloads={name:(b"small fixture "+name.encode()) for name in
                       ("config.json","model.bin","tokenizer.json","vocabulary.txt")}
        self.items=[{"type":"file","path":name,"size":len(data),
                     "oid":hashlib.sha1(b"blob "+str(len(data)).encode()+b"\0"+data).hexdigest()}
                    for name,data in self.payloads.items()]
        self.calls=[]

    def response(self,request,**kwargs):
        self.calls.append(request.full_url)
        if "/api/models/" in request.full_url:
            return BytesIO(json.dumps(self.items).encode())
        return BytesIO(self.payloads[request.full_url.rsplit("/",1)[-1]])

    def test_models_require_local_files_and_never_trigger_network(self):
        with patch.object(transcription,"urlopen",side_effect=AssertionError("no implicit network")):
            with self.assertRaisesRegex(ValueError,"不会自动下载"):
                transcription.validate_model("tiny")
            folder=make_model(self.root/"tiny")
            self.assertEqual(transcription.validate_model(folder),folder.resolve())
            (folder/"tokenizer.json").unlink()
            with self.assertRaisesRegex(ValueError,"tokenizer.json"):
                transcription.validate_model(folder)

    def test_explicit_download_reports_real_files_hashes_and_reuses_verified_cache(self):
        events=[]
        with patch.object(transcription,"urlopen",side_effect=self.response):
            result=transcription.download_model("tiny",self.root,report=events.append)
            self.assertEqual(result,self.root/"tiny")
            self.assertEqual((result/"model.bin").read_bytes(),self.payloads["model.bin"])
            self.assertEqual(events[-1]["phase"],"model_ready")
            self.assertEqual(events[-1]["downloaded_bytes"],sum(map(len,self.payloads.values())))
            self.calls=[]
            transcription.download_model("tiny",self.root)
        self.assertEqual(len(self.calls),1)
        self.assertTrue(all(event.get("url","").startswith("https://huggingface.co/") for event in events[:-1]))

    def test_bad_download_never_publishes_unverified_model(self):
        self.items[1]["oid"]="0"*40
        with patch.object(transcription,"urlopen",side_effect=self.response):
            with self.assertRaisesRegex(ValueError,"校验失败"):
                transcription.download_model("base",self.root)
        self.assertFalse((self.root/"base/model.bin").exists())
        self.assertFalse(list((self.root/"base").glob("*.part")))

    def test_cancelled_download_cleans_partial_and_releases_lock(self):
        stop=threading.Event()
        def event(row):
            if row["phase"]=="model_download" and row.get("downloaded_bytes",0)>0:
                stop.set()
        with patch.object(transcription,"urlopen",side_effect=self.response):
            with self.assertRaises(transcription.TranscriptionCancelled):
                transcription.download_model("tiny",self.root,report=event,stop=stop)
            self.assertFalse(list((self.root/"tiny").glob("*.part")))
            self.assertEqual(transcription.download_model("tiny",self.root),self.root/"tiny")

    def test_model_selection_is_bounded_before_network(self):
        with patch.object(transcription,"urlopen",side_effect=AssertionError("no request")):
            for value in ("large","../tiny","https://other/model"):
                with self.subTest(value=value),self.assertRaises(ValueError):
                    transcription.download_model(value,self.root)


@unittest.skipUnless(shutil.which("ffmpeg"),"FFmpeg supplies real local audio/video fixtures")
class LocalTranscriptionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)
        self.root=self.folder/"project"
        dataset.initialize(self.root)
        self.video=self.folder/"fixture.mkv"
        subprocess.run(["ffmpeg","-v","error","-f","lavfi","-i","color=size=64x48:rate=5:duration=3",
                        "-f","lavfi","-i","sine=frequency=440:sample_rate=8000:duration=3",
                        "-c:v","ffv1","-c:a","pcm_s16le",str(self.video)],check=True,capture_output=True)
        self.movie,_=dataset.import_movie(self.root,self.video,"time",lambda _:None)
        self.model=make_model(self.folder/"model")
        self.paths=[]
        def transcribe(path,**kwargs):
            self.paths.append(Path(path))
            with wave.open(path,"rb") as audio:
                self.assertEqual((audio.getnchannels(),audio.getframerate()),(1,16000))
            return iter([SimpleNamespace(start=0.,end=.7,text=" 一句真实转写 ")]),SimpleNamespace(language="zh")
        self.engine=SimpleNamespace(transcribe=Mock(side_effect=transcribe))
        self.factory=Mock(return_value=self.engine)
        patcher=patch.dict("sys.modules",{"faster_whisper":SimpleNamespace(WhisperModel=self.factory)})
        patcher.start()
        self.addCleanup(patcher.stop)
        availability=patch.object(transcription,"dependencies_available",return_value=True)
        availability.start()
        self.addCleanup(availability.stop)

    def test_audio_chunks_keep_timeline_and_load_only_local_model(self):
        events=[]
        with patch.object(transcription,"CHUNK_SECONDS",1):
            result=transcription.transcribe_movie(self.root,self.movie,model_path=self.model,report=events.append)
        self.assertEqual([row["startMs"] for row in result["segments"]],[0,1000,2000])
        self.assertEqual(result["language"],"zh")
        self.assertEqual(result["durationMs"],3000)
        self.assertTrue(self.factory.call_args.kwargs["local_files_only"])
        self.assertEqual(self.factory.call_args.kwargs["compute_type"],"int8")
        self.assertFalse(any(path.exists() for path in self.paths))
        self.assertIn("extract_audio",{row["phase"] for row in events})
        self.assertEqual(events[-1]["segment_count"],3)

    def test_cancellation_removes_audio_and_next_task_can_start(self):
        stop=threading.Event()
        original=self.engine.transcribe.side_effect
        def cancel(path,**kwargs):
            stream,info=original(path,**kwargs)
            stop.set()
            return stream,info
        self.engine.transcribe.side_effect=cancel
        with self.assertRaises(transcription.TranscriptionCancelled):
            transcription.transcribe_movie(self.root,self.movie,model_path=self.model,stop=stop)
        self.assertFalse(any(path.exists() for path in self.paths))
        self.engine.transcribe.side_effect=original
        self.assertTrue(transcription.transcribe_movie(self.root,self.movie,model_path=self.model)["segments"])

    def test_changed_source_and_missing_audio_are_actionable(self):
        with self.video.open("ab") as stream:
            stream.write(b"changed")
        with self.assertRaisesRegex(ValueError,"原片已变化"):
            transcription.transcribe_movie(self.root,self.movie,model_path=self.model)
        silent=self.folder/"silent.mkv"
        subprocess.run(["ffmpeg","-v","error","-f","lavfi","-i","color=size=64x48:rate=5:duration=1",
                        "-c:v","ffv1",str(silent)],check=True,capture_output=True)
        movie,_=dataset.import_movie(self.root,silent,"time",lambda _:None)
        with self.assertRaisesRegex(ValueError,"没有音轨"):
            transcription.transcribe_movie(self.root,movie,model_path=self.model)
        self.factory.assert_not_called()

    def test_no_detected_speech_does_not_invent_dialogue(self):
        self.engine.transcribe.side_effect=lambda *a,**kw:(iter([]),SimpleNamespace(language="en"))
        result=transcription.transcribe_movie(self.root,self.movie,model_path=self.model)
        self.assertEqual(result["segments"],[])

    def test_malformed_engine_output_is_not_saved_as_dialogue(self):
        self.engine.transcribe.side_effect=lambda *a,**kw:(iter([SimpleNamespace(start=0.,end=.5,text=None)]),SimpleNamespace(language="zh"))
        with self.assertRaisesRegex(ValueError,"无效对白文本"):
            transcription.transcribe_movie(self.root,self.movie,model_path=self.model)

    def test_delayed_audio_is_written_after_leading_silence(self):
        delayed=self.folder/"delayed.mkv"
        subprocess.run(["ffmpeg","-v","error","-f","lavfi","-i","color=size=64x48:rate=5:duration=3",
                        "-itsoffset","1","-f","lavfi","-i","sine=frequency=440:sample_rate=16000:duration=1",
                        "-c:v","ffv1","-c:a","pcm_s16le",str(delayed)],check=True,capture_output=True)
        output=self.folder/"audio.wav"
        transcription._extract_audio(delayed,output,origin=0,duration=3,report=lambda _:None,stop=None)
        with wave.open(str(output),"rb") as source:
            self.assertEqual(source.readframes(16000),b"\0\0"*16000)
            self.assertNotEqual(source.readframes(8000),b"\0\0"*8000)


if __name__=="__main__":
    unittest.main()
