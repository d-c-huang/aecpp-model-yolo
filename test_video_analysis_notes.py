"""Semantic references stay separate from original dialogue and training data."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import dataset
import video_analysis_notes as notes
from story_pipeline import validate_storyboard


def result_fixture():
    return {"schemaVersion": "2026-02-06", "resultId": "result-1", "projectId": "project-1",
            "pipelineVersion": "test", "createdAtMs": 1770000000000,
            "contentBlocks": [
                {"blockId": "b1", "idx": 0, "title": "铺垫与反转", "startMs": 0, "endMs": 12000,
                 "highlights": [{"highlightId": "h1", "idx": 0, "text": "主角先作出承诺，随后用出乎意料的做法收尾。",
                                 "startMs": 1000, "endMs": 10000,
                                 "keyframes": [{"assetId": "asset-1", "contentUrl": "/api/v1/assets/asset-1/content",
                                                "timeMs": 11000, "caption": "反转后的反应"}]}]},
                {"blockId": "b2", "idx": 1, "title": "收尾", "startMs": 12000, "endMs": 20000,
                 "highlights": [{"highlightId": "h2", "idx": 0, "text": "最后保留一小段反应时间。",
                                 "startMs": 13000, "endMs": 18000}]}],
            "mindmap": {"nodes": [
                {"id": "n0", "type": "root", "level": 0, "label": "内容结构", "data": {}},
                {"id": "n1", "type": "topic", "level": 1, "label": "铺垫", "data": {"targetBlockId": "b1"}},
                {"id": "n2", "type": "detail", "level": 2, "label": "反转", "data": {"targetBlockId": "b1", "targetHighlightId": "h1"}}],
                "edges": [{"id": "e1", "source": "n0", "target": "n1"}, {"id": "e2", "source": "n1", "target": "n2"}]},
            "assetRefs": [{"assetId": "asset-1", "kind": "image", "contentUrl": "/api/v1/assets/asset-1/content"}]}


class NormalizeNotesTests(unittest.TestCase):
    def test_upstream_api_contract_keeps_time_navigation_and_keyframe_provenance(self):
        payload = result_fixture()
        original = copy.deepcopy(payload)
        result = notes.normalize_result(payload)
        self.assertEqual(payload, original)
        self.assertEqual(result["projectId"], "project-1")
        block = result["contentBlocks"][0]
        self.assertEqual((block["startMs"], block["endMs"]), (0, 12000))
        self.assertEqual(block["highlights"][0]["keyframes"][0]["timeMs"], 11000)
        self.assertEqual(result["mindmap"]["nodes"][2]["data"]["targetHighlightId"], "h1")
        self.assertEqual(notes.normalize_result(result), result)

    def test_local_content_blocks_and_singular_keyframe_have_explicit_support(self):
        payload = result_fixture()
        highlight = payload["contentBlocks"][0]["highlights"][0]
        highlight["keyframe"] = highlight["keyframes"][0]
        local = {"schemaVersion": payload["schemaVersion"], "contentBlocks": payload["contentBlocks"]}
        clean = notes.normalize_result(local)
        self.assertEqual(len(clean["contentBlocks"][0]["highlights"][0]["keyframes"]), 1)
        self.assertEqual(clean["mindmap"], {"nodes": [], "edges": []})
        self.assertNotIn("projectId", clean)
        with self.assertRaises(ValueError):
            notes.normalize_result({"data": local})

    def test_builtin_evidence_is_preserved_but_never_made_into_reviewed_labels(self):
        payload = result_fixture()
        payload["analysis"] = {"generated": True, "review_status": "unreviewed", "transcript_only": True,
                               "provider": "local", "model": "fixture", "transcript_hash": "a"*64}
        payload["contentBlocks"][0]["evidenceSegments"] = [{"startMs": 1000, "endMs": 2000, "text": "真正的原对白"}]
        payload["contentBlocks"][0]["highlights"][0]["keyframes"][0]["path"] = "video_analysis/frames/one.jpg"
        clean = notes.normalize_result(payload)
        self.assertTrue(clean["analysis"]["transcript_only"])
        self.assertEqual(clean["contentBlocks"][0]["evidenceSegments"][0]["text"], "真正的原对白")
        payload["analysis"]["review_status"] = "reviewed"
        with self.assertRaisesRegex(ValueError, "尚待审核"):
            notes.normalize_result(payload)

    def test_bad_timestamps_duplicate_ids_and_invalid_graph_are_rejected(self):
        alterations = [
            lambda x: x["contentBlocks"][0].update(startMs=True),
            lambda x: x["contentBlocks"][0].update(endMs=0),
            lambda x: x["contentBlocks"][1].update(blockId="b1"),
            lambda x: x["contentBlocks"][1]["highlights"][0].update(highlightId="h1"),
            lambda x: x["contentBlocks"][0]["highlights"][0].update(endMs=12001),
            lambda x: x["contentBlocks"][0]["highlights"][0].update(startMs=1.5),
            lambda x: x["mindmap"]["nodes"][1].update(id="n0"),
            lambda x: x["mindmap"]["nodes"][2]["data"].update(targetHighlightId="h2"),
            lambda x: x["mindmap"]["edges"][0].update(target="missing"),
            lambda x: x["mindmap"]["edges"][1].update(id="e1"),
            lambda x: x["assetRefs"].append(x["assetRefs"][0]),
            lambda x: x["contentBlocks"][0]["highlights"][0]["keyframes"][0].update(timeMs=12000),
            lambda x: x["contentBlocks"][0]["highlights"][0]["keyframes"][0].update(path="../escape.jpg"),
            lambda x: x["contentBlocks"][0]["highlights"][0]["keyframes"][0].update(path="https://host/image.jpg"),
            lambda x: x["contentBlocks"][0]["highlights"][0].update(text="invalid\ud800"),
            lambda x: x["contentBlocks"][0]["highlights"][0].update(keyframes=False),
        ]
        for modify in alterations:
            payload = result_fixture()
            modify(payload)
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    notes.normalize_result(payload)

    def test_size_and_record_limits_apply_before_saving(self):
        with patch.object(notes, "MAX_BYTES", 200):
            with self.assertRaisesRegex(ValueError, "16 MiB"):
                notes.normalize_result(result_fixture())
        with patch.object(notes, "MAX_BLOCKS", 1):
            with self.assertRaises(ValueError):
                notes.normalize_result(result_fixture())
        with patch.object(notes, "MAX_HIGHLIGHTS", 1):
            with self.assertRaises(ValueError):
                notes.normalize_result(result_fixture())


class StoredReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/"project"
        dataset.initialize(self.root)
        with dataset.connect(self.root) as db:
            db.execute("INSERT INTO movies(id,digest,name,mode) VALUES(1,'digest-one','对应影片','time')")

    def save(self, payload=None):
        return notes.save_reference(self.root, 1, payload or result_fixture(),
                                    source={"kind": "built-in-analysis", "model": "test"})

    def test_save_load_binding_integrity_and_portable_paths_without_training(self):
        with dataset.connect(self.root) as db:
            before = db.execute("SELECT count(*) FROM subtitle_tracks").fetchone()[0]
        result = self.save()
        path = self.root/result["path"]
        self.assertTrue(path.is_file())
        self.assertFalse(Path(result["path"]).is_absolute())
        self.assertEqual(result["review_status"], "unreviewed")
        self.assertEqual(result["movie_digest"], "digest-one")
        self.assertEqual(notes.load_reference(self.root, path), result)
        self.assertEqual(notes.list_references(self.root, 1), [result])
        self.assertFalse(notes.list_references(self.root, 2))
        with dataset.connect(self.root) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM subtitle_tracks").fetchone()[0], before)
        for name in ("auto_learning.sqlite", "story_learning", "runs"):
            self.assertFalse((self.root/name).exists())

    def test_repeated_result_updates_same_record_and_new_result_is_preserved(self):
        one, again = self.save(), self.save()
        self.assertEqual(one["path"], again["path"])
        changed = result_fixture()
        changed["contentBlocks"][0]["highlights"][0]["text"] = "修正后的分析"
        second = self.save(changed)
        self.assertNotEqual(one["path"], second["path"])
        self.assertEqual(len(notes.list_references(self.root)), 2)

    def test_no_movie_unsafe_paths_and_tampered_records_fail(self):
        with self.assertRaisesRegex(ValueError, "目标影片"):
            notes.save_reference(self.root, 999, result_fixture())
        result = self.save()
        for path in ("../elsewhere.json", self.root.parent/"outside.json", "annotations.sqlite"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                notes.load_reference(self.root, path)
        stored = self.root/result["path"]
        payload = json.loads(stored.read_text("utf-8"))
        payload["result"]["contentBlocks"][0]["title"] = "篡改"
        stored.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "校验"):
            notes.load_reference(self.root, stored)

    def test_storage_transfer_blocks_reference_save(self):
        from cache_storage import storage_lease
        with storage_lease(self.root):
            with self.assertRaises(ValueError):
                self.save()
        self.assertFalse((self.root/"video_analysis/references").exists())

    def test_selected_local_json_does_not_open_embedded_file_references(self):
        source = self.root/"content_blocks.json"
        payload = result_fixture()
        payload["manifest"] = {"file": "../../somewhere"}
        source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(notes.read_result(source)["contentBlocks"][0]["blockId"], "b1")
        with patch.object(notes, "MAX_BYTES", 100):
            with self.assertRaisesRegex(ValueError, "16 MiB"):
                notes.read_result(source)

    def test_draft_keeps_summaries_distinct_and_refuses_wrong_selection(self):
        note = self.save()
        board = notes.to_storyboard(note, block_ids=["b1"])
        self.assertEqual(validate_storyboard(board), board)
        self.assertEqual(board["characters"], [])
        self.assertEqual(len(board["shots"]), 1)
        shot = board["shots"][0]
        self.assertEqual(shot["source_kind"], "analysis-summary")
        self.assertEqual((shot["source_start_ms"], shot["source_end_ms"]), (1000, 10000))
        self.assertEqual(shot["speaker"], "")
        self.assertIn("非原台词", shot["text"])
        self.assertFalse(board["provenance"]["is_original_dialogue"])
        self.assertEqual(board["provenance"]["review_status"], "unreviewed")
        for selected in ([], ["missing"]):
            with self.assertRaises(ValueError):
                notes.to_storyboard(note, block_ids=selected)

    def test_long_summary_splits_without_truncating_content_or_guessing_roles(self):
        payload = result_fixture()
        original = "长摘要内容。"*600
        payload["contentBlocks"][0]["highlights"][0]["text"] = original
        board = notes.to_storyboard(self.save(payload), block_ids=["b1"])
        self.assertGreater(len(board["shots"]), 1)
        self.assertEqual("".join(shot["text"].removeprefix("【分析摘要，非原台词】") for shot in board["shots"]), original)
        self.assertTrue(all(not shot["actor"] and not shot["speaker"] for shot in board["shots"]))


if __name__ == "__main__":
    unittest.main()
