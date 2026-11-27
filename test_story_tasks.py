"""Text-to-project CLI and application integration, using temporary projects."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class StoryCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder / "打工奇遇.txt"
        self.source.write_text("场景：办公室\n小王：老板，我的工资呢？\n老板：已经到账了！\n小王：到账的是工作量吧！", encoding="utf-8")

    def call_cli(self, *args):
        return subprocess.run([sys.executable, "-X", "utf8", str(Path(__file__).with_name("story_tasks.py")), *map(str,args)],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
            env=dict(os.environ, PYTHONUTF8="1"))

    def test_text_to_editable_project_and_reviewed_learning_roundtrip(self):
        output, board_path = self.folder/"动画.aecpp", self.folder/"分镜.json"
        result = self.call_cli("generate",self.source,"--output",output,"--board",board_path)
        self.assertEqual(result.returncode,0,result.stderr)
        summary = json.loads(result.stdout)
        self.assertGreaterEqual(summary["shots"],3)
        project = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(project["format"],"aecpp-project")
        self.assertTrue(project["scene"]["layers"])
        board = json.loads(board_path.read_text(encoding="utf-8"))
        self.assertIn("工资",str(board))
        project_root = self.folder/"学习工程"
        result = self.call_cli("learn",board_path,"--project",project_root)
        self.assertEqual(result.returncode,0,result.stderr)
        from story_pipeline import load_style
        self.assertIsNotNone(load_style(project_root))
        result = self.call_cli("generate",self.source,"--output",self.folder/"续作.aecpp","--project",project_root)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_existing_outputs_and_source_collisions_preserve_files(self):
        output = self.folder/"已有.aecpp"
        output.write_text("keep",encoding="utf-8")
        original = self.source.read_bytes()
        result = self.call_cli("generate",self.source,"--output",output)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(output.read_text(encoding="utf-8"),"keep")
        result = self.call_cli("generate",self.source,"--output",self.source)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.source.read_bytes(),original)
        result = self.call_cli("generate",self.source,"--output",self.folder/"new.aecpp","--board",self.source)
        self.assertNotEqual(result.returncode,0)
        self.assertFalse((self.folder/"new.aecpp").exists())

    def test_story_draft_and_learned_direction_survive_project_transfer(self):
        import dataset
        from story_pipeline import generate_storyboard, train_style, load_style, save_storyboard, load_storyboard
        from portable_project import export_project, import_project
        source_root, restored = self.folder/"旧电脑", self.folder/"新电脑"
        dataset.initialize(source_root)
        text = "小王：今天开工！"
        board = generate_storyboard(text)
        board["shots"][0].update(action="jump", emotion="happy", camera="wide")
        save_storyboard(board, source_root/"creations/current_story.json")
        train_style(source_root,[board])
        archive = self.folder/"迁移.zip"
        export_project(source_root,archive,include_videos=False)
        import_project(archive,restored)
        self.assertEqual(load_storyboard(restored/"creations/current_story.json"),board)
        result = generate_storyboard(text,model=load_style(restored))
        self.assertEqual(result["shots"][0]["action"],"jump")

    def test_native_actor_script_export(self):
        output = self.folder/"角色剧本.json"
        result = self.call_cli("generate",self.source,"--output",output,"--actor-script")
        self.assertEqual(result.returncode,0,result.stderr)
        script = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(script["version"],1)
        self.assertEqual(len(script["characters"]),2)
        self.assertTrue(script["scenes"][0]["dialogue"])

    def test_cli_consumes_actual_reference_model_and_recovers_from_damaged_reference(self):
        from auto_learning import build_reference, predict_reference_duration
        from story_pipeline import generate_storyboard
        project_root = self.folder / "参考工程"
        reference_path = project_root / "auto_learning/reference.json"
        reference_path.parent.mkdir(parents=True)
        texts = ["老板我的工资呢", "已经到账了", "到账的是工作量吧"]
        reference = build_reference([{"version": 1, "film_hash": "source-film", "movie_id": 1, "mode": "train",
            "timing_examples": [{"text": text, "duration": 1.5+i*.2, "start_ms": i*3000, "end_ms": i*3000+1500+i*200}
                                for i, text in enumerate(texts)]}])
        self.assertTrue(reference["usable"])
        reference_path.write_text(json.dumps(reference, ensure_ascii=False), encoding="utf-8")
        output, board_path = self.folder / "自动时长.aecpp", self.folder / "自动时长.json"
        result = self.call_cli("generate", self.source, "--output", output, "--board", board_path, "--project", project_root)
        self.assertEqual(result.returncode, 0, result.stderr)
        board = json.loads(board_path.read_text(encoding="utf-8"))
        self.assertEqual(board["shots"][0]["duration"], predict_reference_duration(reference, board["shots"][0]["text"]))
        self.assertEqual(json.loads(result.stdout)["reference_summary"]["applied_shots"], len(board["shots"]))
        self.assertIn("不代表动作理解", result.stderr)
        reference_path.write_text("not json", encoding="utf-8")
        recovered_board = self.folder / "回退.json"
        result = self.call_cli("generate", self.source, "--output", self.folder / "回退.aecpp", "--board", recovered_board,
                               "--project", project_root)
        self.assertEqual(result.returncode, 0, result.stderr)
        recovered = json.loads(recovered_board.read_text(encoding="utf-8"))
        baseline = generate_storyboard(self.source.read_text(encoding="utf-8"), self.source.stem)
        self.assertEqual(recovered["shots"], baseline["shots"])
        self.assertIn("参考未能读取", result.stderr)


if __name__ == "__main__":
    unittest.main()
