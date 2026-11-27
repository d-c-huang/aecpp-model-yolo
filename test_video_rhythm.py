"""Local video cuts, cancellation and actual learning-to-native publication."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from video_rhythm import analyse_rhythm
import video_visual_learning as visual
import test_video_visual_learning as visual_fixture
import test_aecpp_resources as resource_fixture
from story_automation import generate_animation
from story_pipeline import generate_project_storyboard


class LearnedResourcePipelineTests(unittest.TestCase):
    def setUp(self):
        self.v = visual_fixture.VisualLearningTests()
        self.v.setUp()
        self.addCleanup(self.v.doCleanups)
        self.r = resource_fixture.ResourceTests()
        self.r.setUp()
        self.addCleanup(self.r.doCleanups)

    def test_short_new_dialogue_matches_sentence_and_native_assets_and_rhythm(self):
        payload = self.v.payload(context_text="早上开会，大家确认了今天的安排。\n今天工资到账了。\n午饭以后还要去整理仓库。",
                                 cut_times_ms=[])
        payload["rhythm"] = {"method":"ffmpeg-scene-difference", "duration_ms":5000,
            "cut_times_ms":[1000,2000,3500], "sample_fps":4, "threshold":.28}
        visual.save_visual_reference(self.v.root,payload)
        result = generate_animation(self.v.root,"场景：教室\n小红：工资到账了。",resource_root=self.r.library)
        shot = result["board"]["shots"][0]
        self.assertEqual((shot["action"],shot["emotion"],shot["camera"]),("jump","happy","close"))
        self.assertEqual(shot["visual_reference"]["rhythm_scope"],"source-film")
        self.assertLess(shot["visual_reference"]["changes"]["duration"]["after"],
                        shot["visual_reference"]["changes"]["duration"]["before"])
        report = json.loads(result["report_path"].read_text(encoding="utf-8"))
        decision = report["shot_matches"][0]
        self.assertEqual(decision["action"]["asset_id"],"jump")
        self.assertEqual(decision["expression"]["asset_id"],"happy")
        self.assertEqual(decision["visual_reference"],shot["visual_reference"])
        native = json.loads(result["output"].read_text(encoding="utf-8"))
        actor = next(layer["actor"] for layer in native["scene"]["layers"] if layer["contentKind"]==3)
        self.assertEqual(actor["actions"][0]["actionId"],decision["action"]["asset_id"])
        self.assertEqual(actor["expressions"][0]["expressionId"],decision["expression"]["asset_id"])
        self.assertEqual(actor["actions"][0]["duration"],shot["duration"])
        self.assertEqual(native["scene"]["timelineRegions"][0]["end"],shot["duration"])

    def test_explicit_bindings_override_learned_intent_with_honest_mapping_report(self):
        visual.save_visual_reference(self.v.root,self.v.payload())
        result=generate_animation(self.v.root,"小红：今天工资到账了。",resource_root=self.r.library,
            bindings={"actions":{"character-1":{"jump":"idle"}},
                      "emotions":{"character-1":{"happy":"neutral"}}})
        row=result["shot_matches"][0]
        self.assertEqual(row["action"]["requested"],"jump")
        self.assertEqual(row["action"]["asset_id"],"idle")
        self.assertEqual(row["action"]["method"],"manual")
        self.assertTrue(row["action"]["substituted"])
        self.assertEqual(row["expression"]["asset_id"],"neutral")

    def test_invalid_whole_film_rhythm_is_rejected_and_unrelated_text_stays_unmatched(self):
        payload=self.v.payload()
        payload["rhythm"]={"method":"ffmpeg-scene-difference","duration_ms":5000,
            "cut_times_ms":[1000,1100],"sample_fps":4,"threshold":.28}
        with self.assertRaisesRegex(ValueError,"切镜"):
            visual.save_visual_reference(self.v.root,payload)
        payload.pop("rhythm")
        visual.save_visual_reference(self.v.root,payload)
        board=generate_project_storyboard(self.v.root,"宇航员在木星附近研究引力波。")
        self.assertNotIn("visual_reference",board["shots"][0])


@unittest.skipUnless(shutil.which("ffmpeg"),"FFmpeg required for actual cut detection")
class RhythmVideoTests(unittest.TestCase):
    def test_real_cuts_and_static_video_and_cancellation(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"cuts.mp4"
            subprocess.run(["ffmpeg","-v","error","-f","lavfi","-i",
                "color=black:s=96x64:r=8:d=1[a];color=white:s=96x64:r=8:d=1[b];color=black:s=96x64:r=8:d=1[c];[a][b][c]concat=n=3:v=1:a=0",
                "-c:v","libx264",str(path)],check=True,capture_output=True)
            result=analyse_rhythm(path,duration_ms=3000)
            self.assertEqual(result["cut_times_ms"],[1000,2000])
            calls=[]
            def cancel():
                calls.append(1)
                if len(calls)>1: raise RuntimeError("cancelled")
            with self.assertRaisesRegex(RuntimeError,"cancelled"):
                analyse_rhythm(path,duration_ms=3000,check=cancel)
            # A cancelled detector must release its child and allow the next run.
            self.assertEqual(analyse_rhythm(path,duration_ms=3000)["cut_times_ms"],[1000,2000])

    def test_one_click_upgrades_old_matched_reference_once_without_new_visual_inference(self):
        fixture=LearnedResourcePipelineTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        path=fixture.v.root/'real-cuts.mp4'
        subprocess.run(["ffmpeg","-v","error","-f","lavfi","-i",
            "color=black:s=96x64:r=8:d=1[a];color=white:s=96x64:r=8:d=1[b];color=black:s=96x64:r=8:d=3[c];[a][b][c]concat=n=3:v=1:a=0",
            "-c:v","libx264",str(path)],check=True,capture_output=True)
        from dataset import register_movie
        movie=register_movie(fixture.v.root,path,"time",lambda _:None)
        payload=fixture.v.payload(cut_times_ms=[])
        payload.update(movie_id=movie["id"],film_hash=movie["digest"])
        old=visual.save_visual_reference(fixture.v.root,payload)
        result=generate_animation(fixture.v.root,"小红：今天工资到账了。",resource_root=fixture.r.library)
        shot=result["board"]["shots"][0]
        self.assertIn("duration",shot["visual_reference"]["applied_fields"])
        new=visual.list_visual_references(fixture.v.root,movie["id"])[0]
        self.assertEqual(new["rhythm"]["cut_times_ms"],[1000,2000])
        self.assertEqual(new["clips"],old["clips"])
        self.assertTrue((fixture.v.root/old["path"]).is_file())
        with patch("video_rhythm.analyse_rhythm",side_effect=AssertionError("cached rhythm expected")):
            repeat=generate_animation(fixture.v.root,"小红：今天工资到账了。",resource_root=fixture.r.library)
        self.assertEqual(repeat["board"],result["board"])
