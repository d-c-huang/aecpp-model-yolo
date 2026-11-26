"""Real media checks for direct film learning and frozen corrections."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

import dataset
import movie_sequence_data as data
import subtitles


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg generates the movie fixtures")
class MovieSequenceDataTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.root = self.folder/"中文电影工程"
        dataset.initialize(self.root)
        self.quiet = lambda _: None

    def film(self, name="电影", *, audio=False, mode="time", duration=5, color=None):
        path = self.folder/(name+".mkv")
        source = (f"color={color}:size=96x64:rate=10:duration={duration}" if color else
                  f"testsrc2=size=96x64:rate=10:duration={duration}")
        command = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", source]
        if audio:
            command.extend(["-f", "lavfi", "-i", f"sine=frequency=400:sample_rate=8000:duration={duration}"])
        command.extend(["-c:v", "ffv1"])
        if audio:
            command.extend(["-c:a", "pcm_s16le"])
        subprocess.run(command+[str(path)], check=True, capture_output=True)
        movie_id, _ = dataset.import_movie(self.root, path, mode, self.quiet)
        return movie_id, path

    def prepare(self, snapshot=None, run="run", fps=1):
        snapshot = snapshot or data.snapshot_sources(self.root)
        folder = self.root/run
        manifest = data.prepare_corpus(self.root, snapshot, folder, fps, self.quiet)
        with np.load(folder/"movie_corpus.npz", allow_pickle=False) as stored:
            return manifest, stored["features"], stored["times_ms"]

    def test_whole_movie_without_annotations_and_without_audio(self):
        self.film()
        manifest, features, times = self.prepare()
        self.assertEqual(times.tolist(), [0, 1000, 2000, 3000, 4000])
        self.assertEqual(features.shape, (5, len(data.FEATURE_NAMES)))
        self.assertTrue(np.isfinite(features).all())
        self.assertEqual(manifest["train_frames"], 4)
        self.assertEqual(manifest["val_frames"], 1)
        self.assertFalse(manifest["independent_validation"])
        self.assertFalse(features[:, data.INDEX["audio_available"]].any())
        self.assertFalse(features[:, data.INDEX["people_available"]].any())
        self.assertFalse(features[:, data.INDEX["annotation_available"]].any())
        self.assertEqual([(row["split"], row["offset"], row["length"]) for row in manifest["sequences"]],
                         [("train", 0, 4), ("val", 4, 1)])

    def test_audio_is_measured_and_frames_are_not_duplicated_at_low_fps(self):
        self.film(audio=True)
        manifest, features, times = self.prepare(fps=.25)
        self.assertEqual(times.tolist(), [0, 4000])
        self.assertTrue((features[:, data.INDEX["audio_available"]]==1).all())
        self.assertTrue((features[:, data.INDEX["audio_rms"]]>.01).all())
        self.assertTrue((features[:, data.INDEX["audio_peak"]]>.05).all())
        self.assertTrue(manifest["movies"][0]["audio_available"])

    def test_sparse_audio_bin_cannot_include_validation_sound(self):
        self.film(audio=True, duration=6)
        manifest, features, times = self.prepare(fps=.25)
        self.assertEqual(times.tolist(), [0, 4000])
        self.assertEqual(manifest["sequences"][0]["end_ms"], 5000)
        self.assertEqual(features[0, data.INDEX["audio_available"]], 1)
        # The second bin would cover 4–6 s, across the 5 s split. It must not
        # expose held-out sound in its training feature or prediction target.
        self.assertEqual(features[1, data.INDEX["audio_available"]], 0)
        self.assertEqual(features[1, data.INDEX["audio_rms"]], 0)
        self.assertEqual(features[1, data.INDEX["audio_peak"]], 0)

    def test_three_roles_corrected_subtitles_and_frozen_snapshot_reuse_raw_cache(self):
        movie, _ = self.film(audio=True)
        subtitles.store_track(self.root, movie, "manual", "中文", "first", [(0, 3000, "原句"), (3000, 4000, "原句")])
        cue = subtitles.candidates(self.root, movie, 500)[0]
        legacy_ref = {key: cue[key] for key in ("source", "start_ms", "end_ms", "text")}
        with dataset.connect(self.root) as db:
            voices = [{"kind": "offscreen", "role": role, "lines": ["原句"], "subtitle_refs": [legacy_ref]}
                      for role in ("甲", "乙", "丙")]
            db.execute("""INSERT INTO frames(movie,slot,ms,path,split,state,voices)
                          VALUES(?,0,0,'frames/not-required.jpg','train','reviewed',?)""",
                       (movie, json.dumps(voices, ensure_ascii=False)))
        frozen = data.snapshot_sources(self.root)
        first, before, times = self.prepare(frozen, run="first")
        self.assertEqual(before[0, data.INDEX["confirmed_role_count"]], 3)
        self.assertEqual(before[1, data.INDEX["confirmed_role_count"]], 3)
        self.assertEqual(before[2, data.INDEX["confirmed_role_count"]], 3)
        self.assertEqual(before[3, data.INDEX["confirmed_role_count"]], 0)
        subtitles.correct_cue(self.root, movie, cue, "修正后的完整字幕")
        newer = data.snapshot_sources(self.root)
        self.assertNotEqual(frozen["fingerprint"], newer["fingerprint"])
        with patch.object(data, "open_movie", side_effect=AssertionError("raw film cache must be reused")):
            _, old_again, _ = self.prepare(frozen, run="old")
            _, after, _ = self.prepare(newer, run="new")
        np.testing.assert_array_equal(before, old_again)
        self.assertEqual(before[0, data.INDEX["subtitle_chars"]], 2)
        self.assertEqual(after[0, data.INDEX["subtitle_chars"]], len("修正后的完整字幕"))
        self.assertEqual(after[1, data.INDEX["confirmed_role_count"]], 3)
        self.assertEqual(len(list((self.root/"movie_features").glob("*.json"))), 1)
        self.assertEqual(first["snapshot"], frozen["fingerprint"])

    def test_independent_movies_keep_splits_and_source_changes_are_rejected(self):
        first, source = self.film("训练片", mode="train", color="red")
        self.film("验证片", mode="val", color="blue")
        snapshot = data.snapshot_sources(self.root)
        manifest, features, times = self.prepare(snapshot)
        self.assertTrue(manifest["independent_validation"])
        self.assertEqual((manifest["train_frames"], manifest["val_frames"]), (5, 5))
        self.assertEqual(len(data.snapshot_sources(self.root, [first])["movies"]), 1)
        with source.open("ab") as stream:
            stream.write(b"changed")
        with self.assertRaisesRegex(ValueError, "原片已变化"):
            self.prepare(snapshot, run="changed")
        with self.assertRaisesRegex(ValueError, "不存在"):
            data.snapshot_sources(self.root, [999])

    def test_sequences_never_cross_120_seconds_or_split_boundary(self):
        movie, _ = self.film(duration=1)
        snapshot = data.snapshot_sources(self.root)
        times = np.arange(301, dtype=np.int64)*1000
        raw = np.zeros((len(times), len(data.FEATURE_NAMES)), dtype=np.float32)
        with patch.object(data, "_extract_raw", return_value=(raw, times, {"duration_ms": 301000})):
            manifest, _, _ = self.prepare(snapshot)
        sequences = manifest["sequences"]
        self.assertEqual(sum(row["length"] for row in sequences), len(times))
        for row in sequences:
            self.assertLessEqual(row["end_ms"]-row["start_ms"], 120000)
            subset = times[row["offset"]:row["offset"]+row["length"]]
            if row["split"]=="train":
                self.assertTrue((subset<241000).all())
            else:
                self.assertTrue((subset>=241000).all())

    def test_confirmed_profiles_freeze_context_and_normalize_aliases_without_redecoding(self):
        import movie_profiles
        import movie_context
        movie, _ = self.film()
        profile = {"title": "测试影片", "cast": [{"role": "至尊宝", "actor": "周星驰", "aliases": ["阿宝"]}],
                   "synopsis": "旅人在途中遇见朋友。", "events": [{"start_ms": 2000, "end_ms": 3000,
                                                        "text": "与朋友相遇", "roles": ["至尊宝"]}]}
        movie_profiles.save(self.root, movie, profile, confirm=True)
        with dataset.connect(self.root) as db:
            voices = [{"kind": "offscreen", "role": role, "lines": []} for role in ("至尊宝", "阿宝")]
            db.execute("INSERT INTO frames(movie,slot,ms,path,split,state,voices) VALUES(?,0,0,'unused.jpg','train','reviewed',?)",
                       (movie, json.dumps(voices, ensure_ascii=False)))
        frozen = data.snapshot_sources(self.root)
        first, features, _ = self.prepare(frozen, run="profile-first")
        self.assertEqual(features[0, data.INDEX["confirmed_role_count"]], 1)
        self.assertEqual(first["profile_count"], 1)
        self.assertEqual(first["profile_context"]["names"], movie_context.NAMES)
        with np.load(self.root/"profile-first/movie_corpus.npz", allow_pickle=False) as stored:
            before = stored["profile_context"].copy()
        self.assertEqual(before.shape, (5, 99))
        self.assertFalse(before[:2, 64:96].any())
        self.assertTrue(before[2, 64:96].any())
        profile["synopsis"] = "一个完全不同的剧情资料。"
        movie_profiles.save(self.root, movie, profile, confirm=True)
        newer = data.snapshot_sources(self.root)
        self.assertNotEqual(frozen["fingerprint"], newer["fingerprint"])
        with patch.object(data, "open_movie", side_effect=AssertionError("raw video should remain cached")):
            self.prepare(frozen, run="profile-frozen")
            self.prepare(newer, run="profile-newer")
        with np.load(self.root/"profile-frozen/movie_corpus.npz", allow_pickle=False) as stored:
            np.testing.assert_array_equal(stored["profile_context"], before)
        with np.load(self.root/"profile-newer/movie_corpus.npz", allow_pickle=False) as stored:
            self.assertFalse(np.array_equal(stored["profile_context"][:, 32:64], before[:, 32:64]))


if __name__ == "__main__":
    unittest.main()
