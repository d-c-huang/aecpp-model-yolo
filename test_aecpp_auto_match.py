"""Automatic matching must stay inside the real, editable resource catalogue."""
import copy
import unittest

import aecpp_resources as resources
from aecpp_auto_match import auto_bind_resources
from story_pipeline import generate_storyboard
import test_aecpp_resources as fixtures


class AutoMatchTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ResourceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.board = self.fixture.board

    def scan(self):
        return resources.scan_resources(self.fixture.library)

    def actor(self, name, *, actions=None, tags=None):
        document = copy.deepcopy(self.fixture.document)
        document["name"] = name
        if actions is not None:
            document["motions"] = [{"id": key, "name": label, "durationFrames": 60, "loop": True}
                                   for key, label in actions]
        if tags:
            document["tags"] = tags
        path = self.fixture.assets/"actors"/name/(name+".actor")
        self.fixture.write(path, document)
        return path.relative_to(self.fixture.assets).as_posix()

    def background(self, name):
        path = self.fixture.assets/"scenes"/(name+".jpg")
        self.fixture.image(path)
        return path.relative_to(self.fixture.assets).as_posix()

    def test_fall_shake_and_unknown_background_use_real_fallbacks(self):
        self.board["shots"][0].update(action="fall", scene="海底宫殿")
        self.board["shots"][1].update(action="shake", scene="海底宫殿")
        catalog = self.scan()
        before = copy.deepcopy((self.board, catalog))
        result = auto_bind_resources(self.board, catalog)
        self.assertTrue(result["plan"]["ready"], result["plan"]["issues"])
        self.assertEqual(result["bindings"]["actions"]["character-1"]["fall"], "idle")
        self.assertEqual(result["bindings"]["actions"]["character-2"]["shake"], "idle")
        substituted = [row for row in result["matches"] if row["substituted"]]
        self.assertEqual({row["requested"] for row in substituted}, {"fall", "shake", "海底宫殿"})
        self.assertTrue(any("站立" in row for row in result["warnings"]))
        self.assertTrue(resources.verify_binding_plan(result["plan"], catalog))
        self.assertEqual((self.board, catalog), before)

    def test_available_similar_actions_are_disclosed_instead_of_claimed_exact(self):
        self.actor("小明", actions=[("idle", "站立"), ("sit_down", "坐下"), ("shake_head", "摇头")])
        board = generate_storyboard("场景：教室\n小明：倒地了。\n小明：瑟瑟发抖。")
        board["shots"][0]["action"] = "fall"
        board["shots"][1]["action"] = "shake"
        result = auto_bind_resources(board, self.scan())
        self.assertTrue(result["plan"]["ready"])
        mapped = result["bindings"]["actions"]["character-1"]
        self.assertEqual((mapped["fall"], mapped["shake"]), ("sit_down", "shake_head"))
        rows = [row for row in result["matches"] if row["requested"] in {"fall", "shake"}]
        self.assertTrue(all(row["method"] == "similar" and row["substituted"] for row in rows))

    def test_real_named_synonyms_and_expression_synonyms(self):
        self.actor("小明", actions=[("custom-standing", "站立"), ("custom-speaking", "讲话")])
        catalog = self.scan()
        catalog["expressions"] = [row for row in catalog["expressions"] if row["id"] in {"neutral", "surprised"}]
        board = generate_storyboard("场景：教室\n小明：什么？！")
        result = auto_bind_resources(board, catalog)
        self.assertTrue(result["plan"]["ready"], result["plan"]["issues"])
        self.assertEqual(result["bindings"]["actions"]["character-1"]["talk"], "custom-speaking")
        self.assertEqual(result["bindings"]["emotions"]["character-1"]["shocked"], "surprised")
        self.assertTrue(any(row["method"] == "synonym" for row in result["matches"]))
        self.assertTrue(any("shocked" in warning for warning in result["warnings"]))

    def test_long_and_short_character_names_get_distinct_exact_actors(self):
        ming = self.actor("小明")
        uncle = self.actor("小明叔叔")
        board = generate_storyboard("场景：教室\n明：来了。\n小明：你好。\n小明叔叔：你好。\n小明：又见面了。")
        result = auto_bind_resources(board, self.scan())
        roles = {row["name"]: result["bindings"]["characters"][row["id"]] for row in board["characters"]}
        self.assertEqual(roles["小明"], ming)
        self.assertEqual(roles["小明叔叔"], uncle)
        self.assertEqual(len(set(roles.values())), 3)
        shot_actors = [shot["actor"]["asset_id"] for shot in result["plan"]["shots"] if shot["character_id"] == "character-2"]
        self.assertEqual(shot_actors, [ming, ming])

    def test_character_coverage_beats_generic_front_facing_actor(self):
        equipped = self.actor("人物2", actions=[("idle", "站立"), ("run", "奔跑")])
        board = generate_storyboard("场景：教室\n小明：快跑！")
        result = auto_bind_resources(board, self.scan())
        self.assertEqual(result["bindings"]["characters"]["character-1"], equipped)

    def test_front_view_is_preferred_when_capabilities_equal(self):
        self.actor("沙雕背面")
        board = generate_storyboard("场景：教室\n小明：你好。")
        result = auto_bind_resources(board, self.scan())
        self.assertEqual(result["bindings"]["characters"]["character-1"], "actors/沙雕正面/沙雕正面.actor")

    def test_explicit_manual_choices_win_and_invalid_ids_remain_blocking(self):
        manual = self.actor("人物2")
        background = self.background("海边")
        config = {"characters": {"character-1": manual}, "backgrounds": {"教室": background},
                  "actions": {"character-1": {"talk": "jump"}}, "emotions": {"character-1": {"neutral": "custom:big-laugh"}}}
        original = copy.deepcopy(config)
        result = auto_bind_resources(self.board, self.scan(), config)
        self.assertTrue(result["plan"]["ready"])
        self.assertEqual(result["bindings"]["characters"]["character-1"], manual)
        self.assertEqual(result["bindings"]["backgrounds"]["教室"], background)
        self.assertEqual(result["bindings"]["actions"]["character-1"]["talk"], "jump")
        self.assertEqual(config, original)
        for field, mapping in (("characters", {"character-1": "invented"}),
                               ("actions", {"character-1": {"talk": "invented"}}),
                               ("emotions", {"character-1": {"neutral": "invented"}}),
                               ("backgrounds", {"教室": "invented"})):
            with self.subTest(field=field):
                result = auto_bind_resources(self.board, self.scan(), {field: mapping})
                self.assertFalse(result["plan"]["ready"])
                for key, value in mapping.items():
                    if isinstance(value, dict):
                        for label, selected in value.items():
                            self.assertEqual(result["bindings"][field][key][label], selected)
                    else:
                        self.assertEqual(result["bindings"][field][key], value)
                self.assertTrue(any("invented" in message for message in result["warnings"]))

    def test_empty_placeholders_are_filled_only_in_automatic_route(self):
        config = {"characters": {"character-1": ""}, "backgrounds": {"教室": ""}, "actions": {"character-1": {"talk": ""}}}
        self.assertFalse(resources.build_binding_plan(self.board, self.scan(), config)["ready"])
        result = auto_bind_resources(self.board, self.scan(), config)
        self.assertTrue(result["plan"]["ready"])

    def test_repeated_generation_preserves_substitution_disclosure(self):
        self.board["shots"][0].update(action="fall", scene="海底宫殿")
        self.board["shots"][1].update(action="shake", scene="海底宫殿")
        catalog = self.scan()
        first = auto_bind_resources(self.board, catalog)
        second = auto_bind_resources(self.board, catalog, first["bindings"])
        self.assertEqual(second["bindings"], first["bindings"])
        expected = {(row["kind"], row["subject"], row["requested"]) for row in first["matches"] if row["substituted"]}
        actual = {(row["kind"], row["subject"], row["requested"]) for row in second["matches"] if row["substituted"]}
        self.assertEqual(actual, expected)
        self.assertTrue(all(row["method"] == "manual" for row in second["matches"]))
        self.assertTrue(any("fall" in warning for warning in second["warnings"]))
        self.assertTrue(any("shake" in warning for warning in second["warnings"]))

    def test_repeated_generation_keeps_synonyms_as_equivalent(self):
        self.actor("小明", actions=[("custom-standing", "站立"), ("custom-speaking", "讲话")])
        board = generate_storyboard("场景：教室\n小明：你好。")
        catalog = self.scan()
        first = auto_bind_resources(board, catalog)
        second = auto_bind_resources(board, catalog, first["bindings"])
        actions = [row for row in second["matches"] if row["kind"] == "action"]
        self.assertTrue(actions)
        self.assertTrue(all(not row["substituted"] for row in actions))

    def test_named_memory_handles_changed_ids_and_does_not_bind_new_person_to_old_id(self):
        old = self.actor("人物2")
        new = self.actor("小丽")
        board = generate_storyboard("场景：教室\n小丽：你好。\n小明：你好。")
        config = {"characters": {"character-1": old}, "character_names": {"character-1": "小明"},
                  "actions": {"character-1": {"talk": "jump"}}}
        result = auto_bind_resources(board, self.scan(), config)
        self.assertEqual(result["bindings"]["characters"]["character-1"], new)
        self.assertEqual(result["bindings"]["characters"]["character-2"], old)
        self.assertEqual(result["bindings"]["actions"]["character-2"]["talk"], "jump")
        self.assertEqual(result["bindings"]["actions"]["character-1"]["talk"], "talk")
        self.assertEqual(result["bindings"]["character_names"], {"character-1": "小丽", "character-2": "小明"})
        self.assertTrue(any("重新选角" in row for row in result["warnings"]))

    def test_identity_memory_is_validated_and_survives_settings_roundtrip(self):
        config = {"character_names": {"character-1": "小明"}}
        clean = resources.save_bindings(self.fixture.root, config)
        self.assertEqual(resources.load_bindings(self.fixture.root), clean)
        for bad in ([], {"character-1": []}, {"character-1": ""}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                resources.clean_bindings({"character_names": bad})

    def test_background_uses_scene_location_day_night_and_context(self):
        day = self.background("教室-白天")
        night = self.background("教室-夜晚")
        self.background("街道-夜晚")
        board = generate_storyboard("场景：教室夜晚\n小明：街上的声音真吵。")
        result = auto_bind_resources(board, self.scan())
        self.assertEqual(next(iter(result["bindings"]["backgrounds"].values())), night)
        board = generate_storyboard("小明：白天在教室等你。")
        result = auto_bind_resources(board, self.scan())
        self.assertEqual(next(iter(result["bindings"]["backgrounds"].values())), day)

    def test_manifest_labels_help_opaque_asset_without_inventing_visual_description(self):
        actor = self.actor("人物9", tags=["老教授", "老师"])
        self.fixture.write(self.fixture.assets/"scenes"/"学校教室.json", {"tags": ["实验室", "室内"]})
        board = generate_storyboard("场景：实验室\n老教授：你好。")
        result = auto_bind_resources(board, self.scan())
        self.assertEqual(result["bindings"]["characters"]["character-1"], actor)
        self.assertTrue(result["plan"]["ready"])
        background = next(row for row in result["matches"] if row["kind"] == "background")
        self.assertEqual(background["method"], "context")

    def test_available_scene_with_wrong_time_reports_day_night_substitution(self):
        (self.fixture.assets/"scenes"/"学校教室.jpg").unlink()
        self.background("教室-白天")
        board = generate_storyboard("场景：教室夜晚\n小明：你好。")
        result = auto_bind_resources(board, self.scan())
        background = next(row for row in result["matches"] if row["kind"] == "background")
        self.assertTrue(background["substituted"])
        self.assertTrue(any("昼夜条件" in row for row in result["warnings"]))
        self.assertTrue(result["plan"]["ready"])

    def test_exact_character_identity_is_not_overridden_by_action_coverage(self):
        named = self.actor("小明", actions=[("idle", "站立")])
        self.actor("动作齐全", actions=[(action, action) for action in ("idle", "talk", "walk", "run", "jump", "fall", "shake")])
        board = generate_storyboard("场景：教室\n"+"\n".join("小明：你好。" for _ in range(7)))
        for shot, action in zip(board["shots"], ("idle", "talk", "walk", "run", "jump", "fall", "shake")):
            shot["action"] = action
        result = auto_bind_resources(board, self.scan())
        self.assertEqual(result["bindings"]["characters"]["character-1"], named)
        self.assertTrue(result["plan"]["ready"])

    def test_explicit_scene_beats_dialogue_mention_of_another_place(self):
        living = self.background("温馨客厅-白天")
        board = generate_storyboard("场景：客厅\n小明：教室的事情真多，教室里还有同学，学校的教室要打扫。")
        result = auto_bind_resources(board, self.scan())
        self.assertEqual(result["bindings"]["backgrounds"]["客厅"], living)
        board = generate_storyboard("场景：海底宫殿\n小明：想起教室的事情。")
        result = auto_bind_resources(board, self.scan())
        background = next(row for row in result["matches"] if row["kind"] == "background")
        self.assertTrue(background["substituted"])

    def test_learned_board_action_is_honored_before_dialogue_keywords(self):
        actor = self.actor("小明", actions=[("idle", "站立"), ("walk", "走路"), ("fall", "摔倒")])
        board = generate_storyboard("场景：教室\n小明：我摔倒了。")
        board["shots"][0].update(action="walk", source_kind="film", visual_reference={"action": "walk"})
        result = auto_bind_resources(board, self.scan())
        self.assertEqual(result["bindings"]["characters"]["character-1"], actor)
        self.assertEqual(result["plan"]["shots"][0]["action"]["id"], "walk")

    def test_no_assets_or_missing_category_remains_actionable_not_fake_ready(self):
        for missing in ("actors", "backgrounds", "expressions", "all"):
            with self.subTest(missing=missing):
                catalog = self.scan()
                for field in ("actors", "backgrounds", "expressions") if missing == "all" else (missing,):
                    catalog[field] = []
                result = auto_bind_resources(self.board, catalog)
                self.assertFalse(result["plan"]["ready"])
                self.assertTrue(result["plan"]["issues"])


if __name__ == "__main__":
    unittest.main()
