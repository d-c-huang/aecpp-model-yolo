"""Exercise the real text/catalogue/binding/export pipeline and publication edges."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

import aecpp_resources as resources
import story_automation as automation
import test_aecpp_resources as resource_fixtures
from story_pipeline import generate_storyboard, save_storyboard


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = resource_fixtures.ResourceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root/"my-project"
        self.root.mkdir()
        self.text = "场景：教室\n小明：哈哈，今天开工！\n小红：什么？\n场景：月宫\n小明：（摔倒）哎呀！"

    def test_discovery_complete_run_and_repeated_run_never_overwrite(self):
        messages = []
        original = {path: path.read_bytes() for path in self.fixture.library.rglob("*") if path.is_file()}
        first = automation.generate_animation(self.root, self.text, "../奇遇:动画", report=messages.append)
        second = automation.generate_animation(self.root, self.text, "../奇遇:动画")
        self.assertNotEqual(first["run_dir"], second["run_dir"])
        self.assertTrue(first["output"].is_file())
        self.assertTrue(first["plan"]["ready"])
        self.assertTrue(first["run_dir"].resolve().is_relative_to(self.root.resolve()))
        project = json.loads(first["output"].read_text(encoding="utf-8"))
        self.assertTrue(any(row["contentKind"] == 3 for row in project["scene"]["layers"]))
        self.assertTrue((first["run_dir"]/"story.assets"/"assembly-manifest.json").is_file())
        self.assertTrue((first["run_dir"]/"storyboard.json").is_file())
        self.assertTrue(first["warnings"])
        self.assertEqual(json.loads(first["report_path"].read_text(encoding="utf-8"))["matches"], first["matches"])
        self.assertTrue(any("匹配" in message for message in messages))
        saved = resources.load_bindings(self.root)
        self.assertEqual(saved["resource_root"], str(self.fixture.assets.resolve()))
        self.assertEqual(saved["characters"], {})  # auto selections do not become manual overrides
        self.assertEqual(original, {path: path.read_bytes() for path in self.fixture.library.rglob("*") if path.is_file()})

    def test_cancellation_during_copy_removes_only_own_run_and_preserves_draft(self):
        current = self.root/"creations"/"current_story.json"
        old = generate_storyboard("老师：旧草稿。")
        save_storyboard(old, current)
        previous = self.root/"creations"/"animations"/"earlier"
        previous.mkdir(parents=True)
        (previous/"keep.txt").write_text("keep", encoding="utf-8")
        stop = threading.Event()
        def report(message):
            if "复制现有角色" in message:
                stop.set()
        with self.assertRaisesRegex(ValueError, "取消"):
            automation.generate_animation(self.root, self.text, stop=stop, report=report)
        self.assertEqual(json.loads(current.read_text(encoding="utf-8")), old)
        self.assertEqual(list(previous.parent.iterdir()), [previous])
        self.assertEqual((previous/"keep.txt").read_text(encoding="utf-8"), "keep")

    def test_last_moment_cancel_after_publication_is_success(self):
        stop = threading.Event()
        def report(message):
            if message.startswith("素材组装完成"):
                stop.set()
        result = automation.generate_animation(self.root, self.text, stop=stop, report=report)
        self.assertTrue(stop.is_set())
        self.assertTrue(result["output"].is_file())

    def test_invalid_explicit_directory_does_not_silently_choose_another(self):
        with self.assertRaisesRegex(ValueError, "素材目录不存在"):
            automation.generate_animation(self.root, self.text, resource_root=self.root/"missing")
        self.assertFalse((self.root/"creations"/"animations").exists())

    def test_saved_missing_directory_falls_back_to_installed_sibling(self):
        resources.save_bindings(self.root, {"resource_root": str(self.root/"old-machine")})
        result = automation.generate_animation(self.root, self.text)
        self.assertEqual(result["catalog"]["root"], str(self.fixture.assets.resolve()))
        self.assertTrue(any("不可用" in row for row in result["warnings"]))

    def test_stale_catalog_is_rescanned_before_assembly(self):
        catalog = resources.scan_resources(self.fixture.assets)
        document = copy.deepcopy(self.fixture.document)
        document["name"] = "更新后的沙雕角色"
        self.fixture.write(self.fixture.actor_file, document)
        messages = []
        result = automation.generate_animation(self.root, self.text, catalog=catalog, report=messages.append)
        self.assertTrue(any("重新扫描" in row for row in messages))
        self.assertNotEqual(result["catalog"]["fingerprint"], catalog["fingerprint"])
        self.assertTrue(result["output"].is_file())

    def test_old_story_numeric_id_does_not_override_new_person(self):
        old = generate_storyboard("老师：旧故事。")
        save_storyboard(old, self.root/"creations"/"current_story.json")
        resources.save_bindings(self.root, {"characters": {"character-1": "deleted-old-person.actor"}})
        result = automation.generate_animation(self.root, self.text)
        self.assertTrue(result["plan"]["ready"])
        self.assertNotEqual(result["bindings"]["characters"]["character-1"], "deleted-old-person.actor")

    def test_invalid_explicit_nonempty_binding_is_visible_not_silently_replaced(self):
        with self.assertRaisesRegex(ValueError, "素材尚不能完成组装"):
            automation.generate_animation(self.root, self.text,
                bindings={"characters": {"character-1": "missing.actor"}})
        self.assertFalse((self.root/"creations"/"animations").exists())

    def test_export_failure_keeps_original_draft_and_cleans_run(self):
        with patch.object(automation, "export_resource_project", side_effect=ValueError("fixture export failure")):
            with self.assertRaisesRegex(ValueError, "fixture export failure"):
                automation.generate_animation(self.root, self.text)
        self.assertEqual(list((self.root/"creations"/"animations").iterdir()), [])

    def test_cli_oneclick_has_no_mapping_or_save_path_requirement(self):
        source = self.root/"novel.txt"
        source.write_text(self.text, encoding="utf-8")
        result = subprocess.run([sys.executable, "-X", "utf8", str(Path(__file__).with_name("story_tasks.py")),
            "oneclick", str(source), "--project", str(self.root)], capture_output=True,
            text=True, encoding="utf-8", timeout=30, env=dict(os.environ, PYTHONUTF8="1"))
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertTrue(Path(summary["project"]).is_file())
        self.assertTrue(Path(summary["report"]).is_file())

    def test_preferences_save_failure_is_recoverable_and_recorded_in_report(self):
        with patch.object(automation, "save_bindings", side_effect=OSError("fixture settings failure")):
            result = automation.generate_animation(self.root, self.text)
        self.assertTrue(result["output"].is_file())
        self.assertTrue(any("偏好未能保存" in warning for warning in result["warnings"]))
        self.assertEqual(json.loads(result["report_path"].read_text(encoding="utf-8"))["warnings"], result["warnings"])


if __name__ == "__main__":
    unittest.main()
