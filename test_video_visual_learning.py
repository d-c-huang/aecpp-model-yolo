"""Visual evidence integrity and its effect on real novel/script generation."""
import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from PIL import Image

from dataset import connect, initialize, register_movie
import story_pipeline as story
import video_visual_learning as visual


class VisualLearningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)/"project"
        initialize(self.root)
        self.source = Path(temporary.name)/"source.mp4"
        self.source.write_bytes(b"source content identity")
        self.movie = dict(register_movie(self.root, self.source, "time", lambda _: None))
        self.frame_dir = self.root/"video_analysis/visual_frames"
        self.frame_dir.mkdir(parents=True)
        for name, color in (("one.jpg", "red"), ("two.jpg", "blue")):
            Image.new("RGB", (64, 48), color).save(self.frame_dir/name)

    def payload(self, **overrides):
        clip = {"id": "clip-1", "start_ms": 0, "end_ms": 5000,
                "text": "人物得知薪水到账，双脚离地，两手抬起。", "context_text": "今天工资到账了。",
                "action": "jump", "emotion": "happy", "camera": "close", "confidence": .92,
                "evidence": "连续两帧显示人物双脚离地，嘴角上扬，面部占据大部分画面。",
                "frames": [{"time_ms": 200, "path": "video_analysis/visual_frames/one.jpg"},
                           {"time_ms": 1200, "path": "video_analysis/visual_frames/two.jpg"}],
                "cut_times_ms": [1500, 2500, 3500]}
        clip.update(overrides)
        return {"version": 1, "movie_id": self.movie["id"], "film_hash": self.movie["digest"],
                "provider": "ollama", "model": "vision-test", "clips": [clip],
                "rhythm_method": "luma-cut-candidates", "coverage": {"sampled_clips": 1, "duration_ms": 5000}}

    def test_round_trip_preserves_relative_evidence_and_never_writes_reviewed_model(self):
        note = visual.save_visual_reference(self.root, self.payload())
        actual = visual.load_visual_reference(self.root, note["path"])
        self.assertEqual(actual, note)
        self.assertEqual(note["review_status"], "unreviewed")
        self.assertEqual(len(note["clips"][0]["frames"][0]["sha256"]), 64)
        self.assertFalse((self.root/"story_learning/director.json").exists())
        self.assertEqual(visual.list_visual_references(self.root, self.movie["id"]), [note])
        self.assertEqual(visual.list_visual_references(self.root, 99), [])

    def test_real_project_generation_uses_visual_labels_and_bounded_cut_rhythm(self):
        visual.save_visual_reference(self.root, self.payload())
        content = "小红：今天工资到账了。"
        baseline = story.generate_storyboard(content)
        board = story.generate_project_storyboard(self.root, content)
        shot = board["shots"][0]
        self.assertEqual([shot[field] for field in ("action", "emotion", "camera")], ["jump", "happy", "close"])
        self.assertAlmostEqual(shot["duration"], round(baseline["shots"][0]["duration"]*.8, 3))
        self.assertEqual(shot["text"], "今天工资到账了。")
        self.assertEqual(board["characters"], [{"id": "character-1", "name": "小红"}])
        self.assertEqual(shot["visual_reference"]["clip_id"], "clip-1")
        self.assertEqual(shot["visual_reference"]["review_status"], "unreviewed")
        self.assertEqual(board["visual_reference_summary"]["applied_shots"], 1)
        self.assertIn("尚未人工审核", story.reference_description(board))
        path = self.root/"draft.json"
        story.save_storyboard(board, path)
        self.assertEqual(story.load_storyboard(path), board)

    def test_visual_description_without_subtitles_can_supply_matching_novel_draft(self):
        content = "一个人物得知薪水到账，双手抬起。"
        visual.save_visual_reference(self.root, self.payload(text=content, context_text=""))
        board = story.generate_project_storyboard(self.root, content, source_format="novel")
        self.assertEqual(board["shots"][0]["action"], "jump")
        self.assertEqual(board["shots"][0]["emotion"], "happy")
        self.assertEqual(board["shots"][0]["text"], content)
        self.assertEqual(board["characters"], [])

    def test_confirmed_preferences_and_explicit_author_directions_win(self):
        visual.save_visual_reference(self.root, self.payload())
        reviewed = story.generate_storyboard("小明：今天工资到账了。")
        reviewed["shots"][0].update(action="fall", emotion="sad", camera="wide", duration=5.)
        story.train_style(self.root, [reviewed])
        board = story.generate_project_storyboard(self.root, "小红：今天工资到账了。")
        self.assertEqual([board["shots"][0][field] for field in ("action", "emotion", "camera", "duration")],
                         ["fall", "sad", "wide", 5.])
        self.assertNotIn("visual_reference", board["shots"][0])
        staged = story.generate_project_storyboard(self.root, "小红（开心地跳）：今天工资到账了。")
        self.assertEqual(staged["shots"][0]["action"], "jump")
        self.assertNotIn("visual_reference", staged["shots"][0])

    def test_explicit_novel_action_and_camera_are_not_overwritten(self):
        text = "小明奔跑来到门前。"
        visual.save_visual_reference(self.root, self.payload(context_text=text))
        board = story.generate_project_storyboard(self.root, text, source_format="novel")
        self.assertEqual(board["shots"][0]["action"], "run")
        self.assertEqual(board["shots"][0]["camera"], "wide")
        self.assertNotIn("action", board["shots"][0]["visual_reference"]["applied_fields"])
        visual.save_visual_reference(self.root, self.payload(context_text="全景镜头展现今日发薪的办公室。"))
        board = story.generate_project_storyboard(self.root, "全景镜头展现今日发薪的办公室。")
        self.assertEqual(board["shots"][0]["camera"], "wide")

    def test_low_confidence_unknown_empty_evidence_and_noise_never_enter_library(self):
        for changes in ({"confidence": .74}, {"action": "unknown", "emotion": "unknown", "camera": "unknown"},
                        {"evidence": ""}, {"text": "?", "context_text": "!"}):
            with self.subTest(changes=changes):
                note = visual.save_visual_reference(self.root, self.payload(**changes))
                self.assertEqual(visual.load_visual_library(self.root)["example_count"], 0)
                self.assertTrue(visual.list_visual_references(self.root))
                (self.root/note["path"]).unlink()

    def test_validation_movie_is_visible_but_excluded_and_unrelated_text_has_no_match(self):
        visual.save_visual_reference(self.root, self.payload())
        library = visual.load_visual_library(self.root)
        self.assertIsNone(visual.predict_visual_direction(library, "浩瀚星空中的恒星持续燃烧。"))
        with connect(self.root) as db:
            db.execute("UPDATE movies SET mode='val' WHERE id=?", (self.movie["id"],))
        self.assertEqual(len(visual.list_visual_references(self.root)), 1)
        self.assertEqual(visual.load_visual_library(self.root)["example_count"], 0)
        self.assertEqual(story.generate_project_storyboard(self.root, "小红：今天工资到账了。")["shots"][0]["action"], "talk")

    def test_changed_frame_source_or_note_cannot_silently_supply_labels(self):
        note = visual.save_visual_reference(self.root, self.payload())
        frame = self.frame_dir/"one.jpg"
        frame.write_bytes(frame.read_bytes()+b"changed")
        with self.assertRaisesRegex(ValueError, "画面证据内容"):
            visual.load_visual_reference(self.root, note["path"])
        self.assertEqual(visual.load_visual_library(self.root)["example_count"], 0)
        note = visual.save_visual_reference(self.root, self.payload())
        self.source.write_bytes(b"another source")
        self.assertEqual(visual.load_visual_library(self.root)["example_count"], 0)
        path = self.root/note["path"]
        corrupt = json.loads(path.read_text(encoding="utf-8"))
        corrupt["clips"][0]["action"] = "fall"
        path.write_text(json.dumps(corrupt), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "完整性"):
            visual.load_visual_reference(self.root, path)

    def test_latest_analysis_replaces_previous_movie_examples(self):
        visual.save_visual_reference(self.root, self.payload(action="fall"))
        latest = visual.save_visual_reference(self.root, self.payload(action="jump"))
        library = visual.load_visual_library(self.root)
        self.assertEqual(library["example_count"], 1)
        result = visual.predict_visual_direction(library, "今天工资到账了。")
        self.assertEqual(result["direction"]["action"], "jump")
        self.assertEqual(result["provenance"]["reference_path"], latest["path"])

    def test_evidence_survives_project_move_and_unsafe_paths_are_rejected(self):
        note = visual.save_visual_reference(self.root, self.payload())
        moved = self.root.parent/"moved"
        shutil.copytree(self.root, moved)
        self.assertEqual(visual.load_visual_library(moved)["example_count"], 1)
        self.assertEqual(visual.load_visual_reference(moved, note["path"])["source_hash"], note["source_hash"])
        for bad in ("../source.mp4", "C:/outside.jpg", "/outside.jpg", "..\\outside.jpg"):
            payload = self.payload()
            payload["clips"][0]["frames"][0]["path"] = bad
            with self.subTest(path=bad), self.assertRaises(ValueError):
                visual.save_visual_reference(self.root, payload)

    def test_portable_archive_restores_sources_frames_and_generation_without_original(self):
        from portable_project import export_project, import_project
        note = visual.save_visual_reference(self.root, self.payload())
        before = story.generate_project_storyboard(self.root, "小红：今天工资到账了。")
        archive = self.root.parent/"portable.zip"
        export_project(self.root, archive)
        restored = self.root.parent/"restored"
        import_project(archive, restored)
        self.source.unlink()
        with connect(restored) as db:
            movie = dict(db.execute("SELECT * FROM movies WHERE id=?", (self.movie["id"],)).fetchone())
        self.assertTrue(Path(movie["source_path"]).is_relative_to(restored))
        self.assertTrue(Path(movie["source_path"]).is_file())
        self.assertEqual(movie["digest"], self.movie["digest"])
        transferred = visual.load_visual_reference(restored, note["path"])
        self.assertEqual(transferred["source_hash"], note["source_hash"])
        self.assertEqual(transferred["clips"][0]["frames"], note["clips"][0]["frames"])
        self.assertEqual(visual.load_visual_library(restored)["example_count"], 1)
        after = story.generate_project_storyboard(restored, "小红：今天工资到账了。")
        self.assertEqual(after, before)
        self.assertEqual(after["shots"][0]["action"], "jump")
        self.assertFalse((restored/"story_learning/director.json").exists())

    def test_invalid_ranges_confidence_and_duplicate_frames_are_rejected(self):
        for changes in ({"end_ms": 0}, {"confidence": float("nan")}, {"confidence": True},
                        {"action": "invented"}, {"cut_times_ms": [1000, 1000]}, {"cut_times_ms": [5000]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                visual.save_visual_reference(self.root, self.payload(**changes))
        for kind in ("one_frame", "same_time", "same_path"):
            payload = self.payload()
            frames = payload["clips"][0]["frames"]
            if kind == "one_frame":
                frames.pop()
            elif kind == "same_time":
                frames[1]["time_ms"] = frames[0]["time_ms"]
            else:
                frames[1]["path"] = frames[0]["path"]
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                visual.save_visual_reference(self.root, payload)


if __name__ == "__main__":
    unittest.main()
