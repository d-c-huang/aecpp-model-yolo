"""Text preservation, review learning and portable storyboard contracts."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import story_pipeline as story


class StoryPipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_script_roles_scenes_and_chunking_preserve_dialogue(self):
        speech = "这是一段很长的台词，我们需要把所有文字完整放到分镜里。" * 9
        board = story.generate_storyboard("场景：办公室\n小明：" + speech + "\n旁白：夜深了。\n小红：哈哈！", "上班")
        self.assertEqual([c["name"] for c in board["characters"]], ["小明", "小红"])
        self.assertEqual("".join(s["text"] for s in board["shots"] if s["speaker"] == "character-1"), speech)
        self.assertTrue(all(len(s["text"]) <= story.CHUNK_SIZE for s in board["shots"]))
        self.assertTrue(all(s["scene"] == "办公室" for s in board["shots"]))
        self.assertEqual(board["shots"][-2]["speaker"], "")
        self.assertEqual(board["shots"][-1]["emotion"], "happy")

    def test_prose_multiple_quotes_keep_narration_and_correct_roles(self):
        text = "小明说：“今天发财了！”小红笑着说：“先交房租。”\n“算你狠。”小明说道。"
        board = story.generate_storyboard(text)
        self.assertEqual([r["name"] for r in board["characters"]], ["小明", "小红"])
        speaking = [(s["speaker"], s["text"]) for s in board["shots"] if s["speaker"]]
        self.assertEqual(speaking, [("character-1", "今天发财了！"), ("character-2", "先交房租。"), ("character-1", "算你狠。")])
        self.assertTrue(any(s["text"] == "小红笑着说：" for s in board["shots"]))
        self.assertEqual(board["source_text"], text)

    def test_explicit_script_speaker_keeps_nested_quotes_and_all_words(self):
        dialogue = "先说“你好”，再说“再见”。"
        board = story.generate_storyboard("小明：" + dialogue)
        self.assertEqual(board["characters"], [{"id": "character-1", "name": "小明"}])
        self.assertEqual(board["shots"][0]["speaker"], "character-1")
        self.assertEqual(board["shots"][0]["text"], dialogue)
        question = story.generate_storyboard("小明：你说“你好”了吗？")
        self.assertEqual(question["shots"][0]["text"], "你说“你好”了吗？")
        self.assertEqual(question["shots"][0]["speaker"], "character-1")

    def test_unresolved_pronouns_and_plain_novel_remain_narration(self):
        board = story.generate_storyboard("他问：“谁？”\n风呼呼地吹着。", source_format="novel")
        self.assertEqual(board["characters"], [])
        self.assertEqual([s["text"] for s in board["shots"]], ["他问：", "谁？", "风呼呼地吹着。"])
        auto = story.generate_storyboard("他问：“谁？”")
        self.assertEqual(auto["characters"], [])
        uncertain = story.generate_storyboard("小明走进房间，笑着说：“你好！”")
        self.assertNotIn("笑着", [c["name"] for c in uncertain["characters"]])

    def test_fountain_character_cues_and_scene_changes(self):
        board = story.generate_storyboard("INT. OFFICE - DAY\n\nBOB\nHello.\n\nEXT. STREET - NIGHT\n\n@小红\n你好！", source_format="fountain")
        self.assertEqual([c["name"] for c in board["characters"]], ["BOB", "小红"])
        self.assertEqual([s["text"] for s in board["shots"]], ["Hello.", "你好！"])
        self.assertEqual([s["scene"] for s in board["shots"]], ["OFFICE - DAY", "STREET - NIGHT"])

    def test_pure_novel_action_subjects_drive_characters_without_speaking(self):
        board = story.generate_storyboard("小明走进办公室。小王突然摔倒了。小明震惊得跳了起来。他吓得发抖。", source_format="novel")
        self.assertEqual([c["name"] for c in board["characters"]], ["小明", "小王"])
        self.assertEqual([s["actor"] for s in board["shots"]], ["character-1", "character-2", "character-1", ""])
        self.assertTrue(all(not s["speaker"] for s in board["shots"]))
        self.assertEqual([s["action"] for s in board["shots"]], ["walk", "fall", "jump", "shake"])
        self.assertEqual(story.validate_storyboard(board), board)

    def test_stage_directions_influence_labels_without_entering_spoken_text(self):
        board = story.generate_storyboard("小明（生气）：哼。")
        self.assertEqual(board["characters"][0]["name"], "小明")
        self.assertEqual(board["shots"][0]["text"], "哼。")
        self.assertEqual(board["shots"][0]["emotion"], "angry")
        fountain = story.generate_storyboard("@小红\n（开心地跳）\n哈哈。", source_format="fountain")
        self.assertEqual(len(fountain["shots"]), 1)
        self.assertEqual(fountain["shots"][0]["text"], "哈哈。")
        self.assertEqual(fountain["shots"][0]["action"], "jump")

    def test_invalid_input_and_references_are_rejected(self):
        for value in ("", "   ", "文" * (story.MAX_TEXT+1)):
            with self.subTest(value=len(value)), self.assertRaises(ValueError):
                story.generate_storyboard(value)
        board = story.generate_storyboard("小明：你好。")
        for field, value in (("duration", float("nan")), ("duration", True), ("duration", 0),
                             ("speaker", "missing"), ("action", "explode"), ("text", "")):
            broken = copy.deepcopy(board)
            broken["shots"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                story.validate_storyboard(broken)
        duplicate = copy.deepcopy(board)
        duplicate["shots"].append(duplicate["shots"][0].copy())
        with self.assertRaises(ValueError):
            story.validate_storyboard(duplicate)

    def test_round_trip_and_failed_save_preserve_existing_file(self):
        board = story.generate_storyboard("小明：你好。")
        path = self.root / "中文 分镜.json"
        story.save_storyboard(board, path)
        self.assertEqual(story.load_storyboard(path), board)
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            story.save_storyboard({}, path)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_learning_changes_directing_but_keeps_new_characters_and_dialogue(self):
        self.assertIsNone(story.load_style(self.root))
        board = story.generate_storyboard("小明：今天工资到账了。", "旧故事")
        board["shots"][0].update(action="jump", emotion="happy", camera="wide", duration=6.)
        metrics = story.train_style(self.root, [board])
        self.assertEqual(metrics["example_count"], 1)
        self.assertEqual(metrics["validation"]["kind"], "none")
        self.assertIsNone(metrics["validation"]["accuracy"])
        model = story.load_style(self.root)
        generated = story.generate_storyboard("小红：今天工资到账了。", "新故事", model=model)
        self.assertEqual(generated["characters"], [{"id": "character-1", "name": "小红"}])
        self.assertEqual(generated["shots"][0]["text"], "今天工资到账了。")
        self.assertEqual([generated["shots"][0][f] for f in ("action", "emotion", "camera", "duration")],
                         ["jump", "happy", "wide", 6.])
        unrelated = story.generate_storyboard("树叶随风落下。", model=model)
        self.assertEqual(unrelated["shots"][0]["action"], "idle")
        narrative = story.generate_storyboard("今天工资到账了。", model=model)
        self.assertEqual(narrative["shots"][0]["action"], "idle")

    def test_training_replaces_revisions_and_accumulates_other_stories(self):
        first = story.generate_storyboard("小明：今天发财了。\n小明：走吧。")
        second = story.generate_storyboard("小红：为什么作业还没写完？")
        story.train_style(self.root, [first, second])
        first["shots"] = first["shots"][:1]
        first["shots"][0]["action"] = "fall"
        result = story.train_style(self.root, [first])
        self.assertEqual(result["example_count"], 2)
        self.assertEqual(result["story_count"], 2)
        self.assertEqual(result["validation"]["kind"], "held-out-stories")
        self.assertEqual(result["validation"]["sample_count"], 1)
        self.assertEqual(set(result["validation"]["baseline_accuracy"]), {"action", "emotion", "camera"})
        model = story.load_style(self.root)
        self.assertEqual({r["text"] for r in model["examples"]}, {"今天发财了。", "为什么作业还没写完？"})
        repeated = story.train_style(self.root, [first])
        self.assertEqual(repeated["example_count"], 2)

    def test_explicit_author_direction_takes_priority_over_learned_preferences(self):
        original = story.generate_storyboard("小明：你好")
        original["shots"][0].update(action="jump", emotion="happy", camera="wide")
        story.train_style(self.root, [original])
        generated = story.generate_storyboard("小明（愤怒地摔倒）：你好", model=story.load_style(self.root))
        self.assertEqual(generated["shots"][0]["action"], "fall")
        self.assertEqual(generated["shots"][0]["emotion"], "angry")
        self.assertEqual(generated["shots"][0]["text"], "你好")

    def test_training_rejects_bad_review_without_overwriting_model(self):
        story.train_style(self.root, [story.generate_storyboard("旁白：夜深了。")])
        path = self.root / "story_learning" / "director.json"
        previous = path.read_bytes()
        with self.assertRaises(ValueError):
            story.train_style(self.root, [{}])
        self.assertEqual(path.read_bytes(), previous)
        broken = json.loads(previous)
        broken["examples"][0]["duration"] = -100
        path.write_text(json.dumps(broken), encoding="utf-8")
        with self.assertRaises(ValueError):
            story.load_style(self.root)

    def test_automatic_timing_only_changes_duration_and_reviewed_examples_win(self):
        text = "小明：今天工资到账了。"
        baseline = story.generate_storyboard(text)
        reference = {"example_count": 20, "movie_count": 2, "validation": {"kind": "held-out-films"}}
        with patch.dict("sys.modules", {"auto_learning": SimpleNamespace(predict_reference_duration=lambda model, words: 8.)}):
            automatic = story.generate_storyboard(text, reference=reference)
            self.assertEqual(automatic["shots"][0]["duration"], 8.)
            for field in ("text", "speaker", "actor", "action", "emotion", "camera"):
                self.assertEqual(automatic["shots"][0][field], baseline["shots"][0][field])
            self.assertEqual(automatic["reference_summary"]["applied_shots"], 1)
            reviewed = story.generate_storyboard(text)
            reviewed["shots"][0].update(duration=5., action="jump", emotion="happy")
            story.train_style(self.root, [reviewed])
            combined = story.generate_storyboard(text, model=story.load_style(self.root), reference=reference)
            self.assertEqual(combined["shots"][0]["duration"], 5.)
            self.assertEqual(combined["shots"][0]["action"], "jump")
            self.assertEqual(combined["reference_summary"]["applied_shots"], 0)

    def test_failed_reference_load_keeps_confirmed_director_and_reports_fallback(self):
        text = "小明：今天工资到账了。"
        reviewed = story.generate_storyboard(text)
        reviewed["shots"][0].update(duration=5., action="jump")
        story.train_style(self.root, [reviewed])
        def damaged(_):
            raise ValueError("损坏的自动参考文件")
        with patch.dict("sys.modules", {"auto_learning": SimpleNamespace(load_reference=damaged)}):
            generated = story.generate_project_storyboard(self.root, text)
        self.assertEqual(generated["shots"][0]["action"], "jump")
        self.assertEqual(generated["shots"][0]["duration"], 5.)
        self.assertIn("损坏", story.reference_description(generated))

    def test_invalid_reference_timing_is_rejected_without_changing_story(self):
        baseline = story.generate_storyboard("旁白：夜深了。")
        for duration in (float("nan"), True, -2, 10000):
            with self.subTest(duration=duration), patch.dict("sys.modules", {
                "auto_learning": SimpleNamespace(predict_reference_duration=lambda *args, value=duration: value)
            }):
                generated = story.generate_storyboard("旁白：夜深了。", reference={})
                self.assertEqual(generated["shots"], baseline["shots"])
                self.assertEqual(generated["reference_summary"]["applied_shots"], 0)
                self.assertTrue(generated["reference_summary"]["warning"])

    def test_video_reference_evidence_survives_save_and_rejects_unbounded_metadata(self):
        board = story.generate_storyboard("小明：你好。")
        board["provenance"] = {"kind": "automatic-video-reference", "movie_id": 3, "review_status": "unreviewed"}
        board["shots"][0].update(source_start_ms=1200, source_end_ms=4200, source_kind="subtitle", role_evidence="已确认剧本")
        path = self.root / "自动分镜.json"
        story.save_storyboard(board, path)
        self.assertEqual(story.load_storyboard(path), board)
        broken = copy.deepcopy(board)
        broken["shots"][0]["source_end_ms"] = 1
        with self.assertRaises(ValueError):
            story.validate_storyboard(broken)
        for bad in ({"x": float("nan")}, {"x": "字"*20000}, {"x": {"x": {"x": {"x": {"x": {"x": {"x": 1}}}}}}}):
            with self.subTest(bad=str(bad)[:50]), self.assertRaises(ValueError):
                story.validate_storyboard({**board, "provenance": bad})

    def test_reviewed_video_parts_share_validation_group_and_keep_separate_revisions(self):
        first = story.generate_storyboard("小明：今天开工了。")
        second = story.generate_storyboard("小王：明天休息吧。")
        for part, board in enumerate((first, second), 1):
            board["provenance"] = {"kind": "automatic-video-reference", "source_hash": "same-film", "part": part}
        result = story.train_style(self.root, [first, second])
        self.assertEqual(result["story_count"], 1)
        self.assertEqual(result["example_count"], 2)
        self.assertEqual(result["validation"]["kind"], "none")
        first["shots"][0]["action"] = "jump"
        result = story.train_style(self.root, [first])
        self.assertEqual(result["example_count"], 2)
        model = story.load_style(self.root)
        self.assertEqual(len({row["story"] for row in model["examples"]}), 1)
        self.assertEqual(len({row["unit"] for row in model["examples"]}), 2)
        self.assertEqual(next(row for row in model["examples"] if "开工" in row["text"])["action"], "jump")


if __name__ == "__main__":
    unittest.main()
