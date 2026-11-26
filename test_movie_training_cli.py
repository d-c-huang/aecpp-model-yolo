"""Exercise the actual worker, decoder, optimizer and exported model together."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

import numpy as np

import dataset
import subtitles
import movie_profiles
from movie_sequence_data import snapshot_sources
from movie_sequence_model import export_bundle, predict_next


class MovieTrainingCliTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg generates the integration fixture")
    def test_whole_movie_worker_without_screenshot_annotations(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            root = folder/"电影学习工程"
            dataset.initialize(root)
            for name, split, video in (("训练片", "train", "testsrc2=size=96x64:rate=8:duration=10"),
                                       ("验证片", "val", "color=blue:size=96x64:rate=8:duration=6")):
                path = folder/(name+".mkv")
                args = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", video]
                if split=="train":
                    args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=10",
                             "-c:a", "pcm_s16le"]
                subprocess.run([*args, "-c:v", "ffv1", str(path)], check=True, capture_output=True)
                movie_id, _ = dataset.import_movie(root,path,split,lambda _:None)
                movie_profiles.save(root, movie_id, {"title": name,
                    "cast": [{"role": "旅人", "actor": "演员甲", "aliases": ["主角"]}],
                    "synopsis": "人物在旅途中经历变化。"}, confirm=True)
            subtitles.store_track(root,1,"zh","中文","first",[(1000,4000,"电影里的原句")])
            frozen = snapshot_sources(root)
            snapshot = folder/"冻结输入.json"
            snapshot.write_text(json.dumps(frozen,ensure_ascii=False),encoding="utf-8")
            cue = subtitles.candidates(root,1,2000)[0]
            subtitles.correct_cue(root,1,cue,"训练期间纠正的新台词")
            run = root/"runs"/"integration"
            worker = Path(__file__).with_name("model_tasks.py")
            result = subprocess.run([sys.executable,"-X","utf8",str(worker),"train-movie",str(root),"",
                                     "--epochs","2","--device","cpu","--sample-fps","1", "--run",str(run),
                                     "--patience","3","--learning-rate","0.0007","--restart",
                                     "--snapshot",str(snapshot)],capture_output=True,text=True,encoding="utf-8",
                                    env=dict(os.environ,PYTHONUTF8="1"),timeout=90)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertIn("第 2/2 轮",result.stdout)
            completed = json.loads((run/"completed.json").read_text(encoding="utf-8"))
            self.assertEqual(completed["kind"],"movie-temporal")
            self.assertEqual(completed["validation_kind"],"independent-films")
            self.assertTrue(completed["profile_conditioned"])
            self.assertEqual(completed["profile_context_count"],99)
            self.assertEqual(completed["initial_learning_rate"],.0007)
            self.assertEqual(completed["patience"],3)
            self.assertTrue(completed["restart"])
            self.assertGreater(completed["train_windows"],0)
            self.assertGreater(completed["val_windows"],0)
            self.assertTrue(np.isfinite(completed["val_loss"]))
            self.assertEqual(dataset.frames(root),[])
            saved = json.loads((run/"movie_sources.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["movies"][0]["subtitles"][0]["text"],"电影里的原句")
            self.assertEqual(subtitles.candidates(root,1,2000)[0]["text"],"训练期间纠正的新台词")
            with np.load(run/"movie_corpus.npz",allow_pickle=False) as corpus:
                self.assertEqual(corpus["profile_context"].shape[1],99)
                prediction = predict_next(run,corpus["features"][:3],steps=2,profile=frozen["movies"][0]["profile"])
            self.assertEqual(np.asarray(prediction["values"]).shape,(2,32))
            self.assertTrue(np.isfinite(prediction["values"]).all())
            bundle = export_bundle(run,folder/"电影模型.zip")
            with zipfile.ZipFile(bundle) as archive:
                self.assertIn("movie_temporal.pt",archive.namelist())
                self.assertNotIn("movie_sources.json",archive.namelist())
                self.assertFalse(json.loads(archive.read("bundle.json"))["aecpp_direct_import"])
                profiles = json.loads(archive.read("movie_profiles.json"))
                self.assertEqual(len(profiles["profiles"]),2)
                self.assertEqual(profiles["profiles"][0]["profile"]["synopsis"],"人物在旅途中经历变化。")
                self.assertNotIn("source_path",archive.read("movie_profiles.json").decode("utf-8"))
                self.assertNotIn(str(root),archive.read("movie_profiles.json").decode("utf-8"))
                self.assertNotIn("research",profiles["profiles"][0]["profile"])


if __name__=="__main__":
    unittest.main()
