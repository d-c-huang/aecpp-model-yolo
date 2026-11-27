"""Existing-resource catalogues, actual action mapping and export verification."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image

import aecpp_resources as resources
from story_pipeline import generate_storyboard


class ResourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.library = self.root/"AECPP"
        self.assets = self.library/"assets"
        self.actor_dir = self.assets/"actors"/"沙雕正面"
        self.actor_dir.mkdir(parents=True)
        self.image(self.actor_dir/"body.png")
        self.document = {"version": 1, "resourceKind": "actor", "name": "沙雕正面", "actorKind": "human",
            "skeleton": {"bones": [{"id": "Root", "x": 0, "y": 0, "length": 1}]},
            "boneContours": [{"id": "body", "image": {"path": "actors/沙雕正面/body.png"}}],
            "motions": [{"id": key, "name": label, "loop": loop, "basic": True, "durationFrames": 60}
                        for key, label, loop in (("idle", "站立", True), ("talk", "说话", True), ("jump", "跳跃", False))]}
        self.actor_file = self.actor_dir/"沙雕正面.actor"
        self.write(self.actor_file, self.document)
        self.image(self.assets/"scenes"/"学校教室.jpg")
        for sheet in range(1, 5):
            self.image(self.library/"resources"/"expressions"/f"emotions-{sheet:02d}.png", (40, 30))
        self.expression_dir = self.assets/"accessories"/"expressions"
        self.image(self.expression_dir/"images"/"laugh.png")
        self.image(self.expression_dir/"images"/"mouth.png")
        self.write(self.expression_dir/"expression-catalog.json", {"version": 1, "assets": [{
            "id": "custom:big-laugh", "name": "现有大笑", "path": "images/laugh.png", "builtIn": False,
            "sha256": self.digest(self.expression_dir/"images"/"laugh.png"), "speakingEnabled": True,
            "variants": [{"role": "open", "path": "images/mouth.png", "sha256": self.digest(self.expression_dir/"images"/"mouth.png")}]}]})
        self.board = generate_storyboard("场景：教室\n小明：老师来了。\n小红：哈哈。")

    def image(self, path, size=(20, 30)):
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, "red").save(path)

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def digest(self, path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_scan_reads_actual_actors_motions_images_and_existing_atlases(self):
        before = {path: path.read_bytes() for path in self.library.rglob("*") if path.is_file()}
        catalog = resources.scan_resources(self.library)
        self.assertEqual(catalog["root"], str(self.assets.resolve()))
        self.assertEqual(len(catalog["actors"]), 1)
        actor = catalog["actors"][0]
        self.assertEqual([row["id"] for row in actor["actions"]], ["idle", "talk", "jump"])
        self.assertNotIn("fall", [row["id"] for row in actor["actions"]])
        self.assertEqual(actor["dependencies"][0]["reference"], "actors/沙雕正面/body.png")
        self.assertEqual(actor["dependencies"][0]["sha256"], self.digest(self.actor_dir/"body.png"))
        self.assertEqual(len(catalog["backgrounds"]), 1)
        self.assertEqual(len(catalog["expressions"]), 49)
        shocked = next(row for row in catalog["expressions"] if row["id"] == "shocked")
        self.assertEqual(shocked["kind"], "builtin")
        self.assertEqual(shocked["atlas_cell"], 2)
        self.assertTrue(shocked["path"].endswith("emotions-02.png"))
        custom = next(row for row in catalog["expressions"] if row["id"].startswith("custom:"))
        self.assertEqual(custom["variants"][0]["role"], "open")
        self.assertEqual(catalog["issues"], [])
        self.assertEqual(before, {path: path.read_bytes() for path in self.library.rglob("*") if path.is_file()})

    def test_plan_binds_real_action_and_expression_and_does_not_change_story(self):
        catalog = resources.scan_resources(self.assets)
        before = copy.deepcopy(self.board)
        plan = resources.build_binding_plan(self.board, catalog)
        self.assertTrue(plan["ready"], plan["issues"])
        self.assertEqual(plan["shots"][0]["action"]["id"], "talk")
        self.assertEqual(plan["shots"][1]["expression"]["id"], "happy")
        self.assertEqual(plan["shots"][0]["background"]["path"], str(self.assets/"scenes"/"学校教室.jpg"))
        self.assertEqual(plan["actors"][0]["asset_id"], "actors/沙雕正面/沙雕正面.actor")
        self.assertEqual(self.board, before)
        self.assertTrue(resources.verify_binding_plan(plan, catalog))

    def test_missing_action_requires_explicit_existing_substitution(self):
        catalog = resources.scan_resources(self.assets)
        self.board["shots"][0]["action"] = "fall"
        plan = resources.build_binding_plan(self.board, catalog)
        self.assertFalse(plan["ready"])
        self.assertTrue(any("fall" in issue for issue in plan["issues"]))
        with self.assertRaises(ValueError):
            resources.verify_binding_plan(plan, catalog)
        config = plan["config"]
        config["actions"]["character-1"]["fall"] = "jump"
        config["emotions"]["character-1"]["neutral"] = "custom:big-laugh"
        plan = resources.build_binding_plan(self.board, catalog, config)
        self.assertTrue(plan["ready"])
        self.assertEqual(plan["shots"][0]["action"]["id"], "jump")
        self.assertEqual(plan["shots"][0]["expression"]["kind"], "custom")
        self.assertTrue(resources.verify_binding_plan(plan, catalog))

    def test_explicit_clear_is_not_silently_recommended_again(self):
        catalog = resources.scan_resources(self.assets)
        config = resources.build_binding_plan(self.board, catalog)["config"]
        config["characters"]["character-1"] = ""
        config["backgrounds"]["教室"] = ""
        plan = resources.build_binding_plan(self.board, catalog, config)
        self.assertFalse(plan["ready"])
        self.assertEqual(plan["config"]["characters"]["character-1"], "")
        self.assertEqual(plan["config"]["backgrounds"]["教室"], "")

    def test_unmatched_scene_and_foreign_action_are_reported(self):
        catalog = resources.scan_resources(self.assets)
        config = {"actions": {"character-1": {"talk": "invented"}}}
        self.board["shots"][0]["scene"] = "海底宫殿"
        plan = resources.build_binding_plan(self.board, catalog, config)
        self.assertFalse(plan["ready"])
        self.assertTrue(any("海底宫殿" in issue for issue in plan["issues"]))
        self.assertIsNone(plan["shots"][0]["action"])

    def test_changed_texture_actor_or_catalog_prevents_export(self):
        for target in (self.actor_dir/"body.png", self.actor_file):
            with self.subTest(target=target):
                catalog = resources.scan_resources(self.assets)
                plan = resources.build_binding_plan(self.board, catalog)
                original = target.read_bytes()
                target.write_bytes(original+b" ")
                with self.assertRaisesRegex(ValueError, "素材已变化"):
                    resources.verify_binding_plan(plan, catalog)
                target.write_bytes(original)
        catalog["actors"][0]["name"] = "tampered"
        with self.assertRaisesRegex(ValueError, "记录已被修改"):
            resources.verify_binding_plan(plan, catalog)

    def test_missing_or_escaping_actor_images_are_not_valid_actors(self):
        self.document["boneContours"][0]["image"]["path"] = "../../outside.png"
        self.write(self.actor_file, self.document)
        catalog = resources.scan_resources(self.assets)
        self.assertEqual(catalog["actors"], [])
        self.assertTrue(catalog["issues"])
        self.document["boneContours"][0]["image"]["path"] = "missing.png"
        self.write(self.actor_file, self.document)
        self.assertEqual(resources.scan_resources(self.assets)["actors"], [])

    def test_character_local_image_reference_is_resolved_like_engine(self):
        self.document["boneContours"][0]["image"]["path"] = "body.png"
        self.write(self.actor_file, self.document)
        catalog = resources.scan_resources(self.assets)
        self.assertEqual(catalog["actors"][0]["dependencies"][0]["path"], str(self.actor_dir/"body.png"))

    def test_actor_image_path_precedence_matches_native_parser(self):
        self.document["boneContours"][0]["imagePath"] = "body.png"
        self.document["boneContours"][0]["image"]["path"] = "nonexistent-ignored.png"
        self.write(self.actor_file, self.document)
        catalog = resources.scan_resources(self.assets)
        self.assertEqual(catalog["actors"][0]["dependencies"][0]["reference"], "body.png")
        del self.document["boneContours"][0]["imagePath"]
        self.document["boneContours"][0]["image"] = {"relativePath": "body.png"}
        self.write(self.actor_file, self.document)
        catalog = resources.scan_resources(self.assets)
        self.assertEqual(catalog["actors"][0]["dependencies"][0]["reference"], "body.png")

    def test_empty_motion_actor_is_visible_but_not_bound_as_capable(self):
        self.document["motions"] = []
        self.write(self.actor_file, self.document)
        catalog = resources.scan_resources(self.assets)
        self.assertFalse(catalog["actors"][0]["valid"])
        plan = resources.build_binding_plan(self.board, catalog)
        self.assertFalse(plan["ready"])
        self.assertEqual(plan["actors"], [])

    def test_hidden_or_hash_mismatched_custom_expressions_are_not_selected(self):
        path = self.expression_dir/"expression-catalog.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["assets"][0]["hidden"] = True
        self.write(path, data)
        self.assertEqual(len(resources.scan_resources(self.assets)["expressions"]), 48)
        data["assets"][0]["hidden"] = False
        data["assets"][0]["sha256"] = "0"*64
        self.write(path, data)
        catalog = resources.scan_resources(self.assets)
        self.assertEqual(len(catalog["expressions"]), 48)
        self.assertTrue(any("摘要不符" in issue for issue in catalog["issues"]))

    def test_pack_background_is_real_and_sprite_roles_are_not_fabricated_actors(self):
        pack = self.root/"resource"/"packs"/"demo"
        self.image(pack/"assets"/"scene.jpg")
        self.image(pack/"assets"/"role.png")
        self.write(pack/"catalog.json", {"version": 1, "entries": [
            {"id": "bg", "name": "教室", "operation": "background", "path": "assets/scene.jpg", "status": "ready"},
            {"id": "actor", "name": "图片角色", "operation": "image", "sourceKind": "role", "path": "assets/role.png"}]})
        catalog = resources.scan_resources(self.root/"resource")
        self.assertEqual(len(catalog["backgrounds"]), 1)
        self.assertEqual(catalog["actors"], [])
        self.assertTrue(any("GIF/Spine" in issue for issue in catalog["issues"]))

    def test_settings_roundtrip_is_bounded_and_scan_cancellable(self):
        project = self.root/"project"
        project.mkdir()
        config = resources.load_bindings(project)
        config.update(resource_root=str(self.assets), characters={"character-1": "actors/沙雕正面/沙雕正面.actor"})
        resources.save_bindings(project, config)
        self.assertEqual(resources.load_bindings(project), config)
        for invalid in ({"characters": []}, {"actions": {"x": []}}, {"resource_root": "bad\x00path"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                resources.save_bindings(project, invalid)
        self.assertEqual(resources.load_bindings(project), config)
        with self.assertRaises(InterruptedError):
            resources.scan_resources(self.assets, stop=lambda: True)


if __name__ == "__main__":
    unittest.main()
