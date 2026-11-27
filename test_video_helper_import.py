"""Video Helper data-contract and subtitle preservation checks."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import dataset
import subtitles
import video_helper_import as helper


class VideoHelperImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="helper-import-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "project"
        dataset.initialize(self.root)
        self.video = self.root / "video.mp4"
        self.video.write_bytes(b"source movie fixture")
        with dataset.connect(self.root) as db:
            db.execute("INSERT INTO movies(id,digest,name,mode,source_path) VALUES(1,'one','明确选中的影片','train',?)", (str(self.video),))
        self.project_id = "e3f50010-3f2f-4b20-92e8-c591e5dcf4de"
        self.job_id = "9ace1379-a6a9-4890-bb75-d1f6f33b3773"
        self.source = Path(self.temporary.name) / "helper-data" / self.project_id / "artifacts" / self.job_id / "transcript.json"
        self.source.parent.mkdir(parents=True)
        self.payload = {"version": 1, "provider": "faster-whisper", "language": "zh", "unit": "ms", "durationMs": 5000,
                        "segments": [{"startMs": 500, "endMs": 2000, "text": "这是原始对白"},
                                     {"startMs": 3000, "endMs": 5000, "text": "第二句对白"}]}
        self.write()

    def write(self, payload=None):
        self.source.write_text(json.dumps(self.payload if payload is None else payload, ensure_ascii=False), encoding="utf-8")

    def tracks(self):
        with dataset.connect(self.root) as db:
            return [dict(row) for row in db.execute("SELECT * FROM subtitle_tracks ORDER BY id")]

    def test_imports_real_millisecond_cues_and_srt_with_provenance(self):
        report = Mock()
        before = self.source.read_bytes()
        result = helper.import_transcript(self.root, 1, self.source, report)
        self.assertEqual(result["cue_count"], 2)
        self.assertEqual((result["project_id"], result["job_id"]), (self.project_id, self.job_id))
        self.assertEqual(result["sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(result["provider"], "faster-whisper")
        self.assertEqual(result["source_path"], str(self.source.resolve()))
        self.assertEqual(result["status"], "imported")
        self.assertEqual(self.tracks()[0]["id"], result["track"])
        output = Path(result["import_path"])
        self.assertTrue(output.is_relative_to(self.root / "auto_learning" / "imports"))
        self.assertEqual(subtitles.file_cues(output, output.read_bytes(), lambda _: None),
                         [(500, 2000, "这是原始对白"), (3000, 5000, "第二句对白")])
        self.assertEqual(json.loads(Path(result["record_path"]).read_text("utf-8")), result)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertIn("真实时间码", report.call_args.args[0])

    def test_reimport_preserves_other_tracks_and_matching_manual_corrections(self):
        subtitles.store_track(self.root, 1, "manual:existing", "人工字幕", "manual", [(0, 1000, "人工文字")])
        first = helper.import_transcript(self.root, 1, self.source)
        cue = next(row for row in subtitles.candidates(self.root, 1, 600) if row["source"].startswith("video-helper:"))
        subtitles.correct_cue(self.root, 1, cue, "这是人工修正对白")
        second = helper.import_transcript(self.root, 1, self.source)
        self.assertEqual(first["track"], second["track"])
        self.assertEqual(len(self.tracks()), 2)
        self.assertIn("人工文字", [row["text"] for row in subtitles.candidates(self.root, 1, 600)])
        self.assertIn("这是人工修正对白", Path(second["import_path"]).read_text("utf-8-sig"))
        with dataset.connect(self.root) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM subtitle_corrections").fetchone()[0], 1)

    def test_rejects_analysis_summaries_and_invalid_timestamps_before_writing(self):
        bad_values = [{"contentBlocks": [{"startMs": 0, "endMs": 2000, "title": "剧情概括"}]}]
        for key, value in (("startMs", -1), ("startMs", True), ("startMs", 0.5), ("endMs", 2**63),
                           ("endMs", 500), ("text", ""), ("text", ["对白"]), ("text", "a\x00b")):
            payload = json.loads(json.dumps(self.payload))
            payload["segments"][0][key] = value
            bad_values.append(payload)
        bad_values += [{**self.payload, "unit": "seconds"}, {**self.payload, "version": True},
                       {**self.payload, "segments": []}, {**self.payload, "durationMs": 100}]
        for payload in bad_values:
            with self.subTest(payload=payload):
                self.write(payload)
                with self.assertRaises(ValueError):
                    helper.import_transcript(self.root, 1, self.source)
                self.assertEqual(self.tracks(), [])
        self.assertFalse((self.root / "auto_learning" / "imports").exists())

    def test_bounds_json_size_and_segment_count(self):
        with patch.object(helper, "MAX_BYTES", 64):
            with self.assertRaisesRegex(ValueError, "16 MiB"):
                helper.import_transcript(self.root, 1, self.source)
        with patch.object(helper, "MAX_SEGMENTS", 1):
            with self.assertRaisesRegex(ValueError, "100000"):
                helper.import_transcript(self.root, 1, self.source)
        self.assertEqual(self.tracks(), [])

    def test_rejects_invalid_unicode_before_touching_tracks(self):
        self.payload["segments"][0]["text"] = "invalid \ud800"
        self.source.write_text(json.dumps(self.payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "有效字幕文字"):
            helper.import_transcript(self.root, 1, self.source)
        self.assertEqual(self.tracks(), [])

    def test_requires_existing_movie_and_local_video_without_guessing_names(self):
        for movie in (0, True, 999):
            with self.subTest(movie=movie):
                with self.assertRaises(ValueError):
                    helper.import_transcript(self.root, movie, self.source)
        self.video.unlink()
        with self.assertRaisesRegex(ValueError, "原片缺失"):
            helper.import_transcript(self.root, 1, self.source)
        self.assertEqual(self.tracks(), [])

    def test_only_reads_explicit_json_and_does_not_trust_embedded_paths(self):
        other = self.root / "reference.json"
        other.write_text("not valid JSON and must not be read", encoding="utf-8")
        self.payload.update({"projectId": "invented", "jobId": "invented", "contentBlocksFile": str(other),
                             "sourceFilePath": str(other)})
        self.write()
        result = helper.import_transcript(self.root, 1, self.source)
        self.assertEqual(result["project_id"], self.project_id)
        self.assertEqual(other.read_text("utf-8"), "not valid JSON and must not be read")
        copied = self.root / "transcript-copy.json"
        copied.write_bytes(self.source.read_bytes())
        result = helper.import_transcript(self.root, 1, copied)
        self.assertNotIn("project_id", result)
        self.assertNotIn("job_id", result)

    def test_deduplicates_and_sorts_real_cues(self):
        self.payload["segments"] = [self.payload["segments"][1], self.payload["segments"][0], self.payload["segments"][0]]
        self.write()
        self.assertEqual(helper.import_transcript(self.root, 1, self.source)["cue_count"], 2)

    def test_migration_lease_blocks_import_before_index_changes(self):
        from cache_storage import storage_lease
        with storage_lease(self.root):
            with self.assertRaises(ValueError):
                helper.import_transcript(self.root,1,self.source)
        self.assertEqual(self.tracks(),[])


if __name__ == "__main__":
    unittest.main()
