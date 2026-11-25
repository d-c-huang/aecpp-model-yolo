import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile

import dataset
from subtitles import candidates, export_srt, list_tracks


@unittest.skipUnless(shutil.which("ffmpeg"),"FFmpeg generates the subtitle fixtures")
class SubtitleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.root = self.folder/"中文 工程"
        dataset.initialize(self.root)
        self.script = self.folder/"独立源.srt"
        self.script.write_text("1\n00:00:00,500 --> 00:00:01,250\n谁在敲门？\n\n"
                               "2\n00:00:01,250 --> 00:00:02,000\n是我。\n",encoding="utf-8")

    def movie(self,codec,ext):
        path = self.folder/f"电影.{ext}"
        subprocess.run(["ffmpeg","-v","error","-f","lavfi","-i","testsrc2=size=96x64:duration=3",
                        "-i",str(self.script),"-c:v","mpeg4","-c:s",codec,str(path)],check=True)
        return path

    def test_embedded_timing_and_multiple_formats(self):
        for codec,ext in [("srt","mkv"),("mov_text","mp4"),("ass","mkv")]:
            with self.subTest(codec=codec):
                path = self.movie(codec,ext)
                dataset.extract_subtitles(self.root,path,lambda _:None)
                with dataset.connect(self.root) as db:
                    movie = db.execute("SELECT MAX(id) FROM movies").fetchone()[0]
                self.assertFalse(candidates(self.root,movie,499))
                self.assertEqual(candidates(self.root,movie,500)[0]["text"],"谁在敲门？")
                self.assertEqual(candidates(self.root,movie,1250)[0]["text"],"是我。")
                self.assertFalse(candidates(self.root,movie,2000))
                path.unlink()

    def test_sidecars_refresh_and_unbound_speech_export(self):
        path = self.movie("srt","mkv")
        vtt = self.folder/"电影.en.vtt"
        vtt.write_text("WEBVTT\n\n00:00:00.500 --> 00:00:01.250\nWho is there?\n",encoding="utf-8")
        self.folder.joinpath("电影.zh.srt").write_text(self.script.read_text(encoding="utf-8"),encoding="gb18030")
        dataset.extract(self.root,path,1,"train",lambda _:None)
        row = dataset.frames(self.root)[0]
        movie = row["movie"]
        self.assertEqual(len(candidates(self.root,movie,600)),3)
        vtt.write_text(vtt.read_text(encoding="utf-8").replace("Who is there?","Knock knock"),encoding="utf-8")
        for _ in range(2):
            dataset.extract_subtitles(self.root,path,lambda _:None)
        cues = candidates(self.root,movie,600)
        self.assertEqual(len(cues),3)
        cue = next(c for c in cues if c["text"]=="Knock knock")
        target = self.folder/"output.srt"
        export_srt(self.root,next(t["id"] for t in list_tracks(self.root) if t["label"]==vtt.name),target)
        self.assertIn("00:00:00,500 --> 00:00:01,250\nKnock knock",target.read_text(encoding="utf-8-sig"))
        voice = {"kind":"offscreen","role":"门外的甲","lines":[cue["text"]],"subtitle_refs":[cue]}
        narrator = {"kind":"narration","role":"","lines":["那天，他们第一次相遇。"]}
        dataset.save_boxes(self.root,row["id"],[],"draft",voices=[voice,narrator])
        dataset.save_boxes(self.root,row["id"],[],"reviewed")
        dataset.initialize(self.root)
        self.assertEqual(dataset.role_names(self.root,movie),["门外的甲"])
        out = self.folder/"素材.zip"
        dataset.export_samples(self.root,out,lambda _:None)
        with zipfile.ZipFile(out) as archive:
            frame = json.loads(archive.read("samples.jsonl"))
            self.assertFalse(frame["objects"])
            speech,aside = frame["frame_dialogue"]
            self.assertEqual(speech["subtitle_refs"][0]["end_ms"],1250)
            self.assertEqual(speech["source"],"offscreen")
            self.assertIsNone(aside["character"])
            self.assertEqual(aside["source"],"narration")
            self.assertEqual(len(archive.read("subtitles.jsonl").splitlines()),5)
