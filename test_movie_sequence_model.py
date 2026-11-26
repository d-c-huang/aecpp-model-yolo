import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import numpy as np
import torch

import movie_sequence_model as learning
import movie_sequence_data
import movie_context


class MovieSequenceModelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.names = ["luma", "motion", "people_count", "subtitle_active"]
        self.extractor_version = 1
        self.contexts = None
        phases = np.arange(144, dtype=np.float32) / 12
        self.features = np.column_stack((np.sin(phases), np.cos(phases),
                                         np.full(144, 7), np.zeros(144))).astype(np.float32)
        self.sequences = [self.sequence(0, 96, "train", "film-a"), self.sequence(96, 48, "val", "film-b")]

    @staticmethod
    def sequence(offset, length, split, film, start_ms=0):
        return {"offset": offset, "length": length, "split": split, "film_id": film,
                "movie_id": 1 if film == "film-a" else 2, "name": film,
                "start_ms": start_ms, "end_ms": start_ms + length * 1000}

    def prepare(self, root, snapshot, run, sample_fps, report, weights=None, device=None):
        manifest = {"version": 1, "extractor_version": self.extractor_version,
                    "feature_names": self.names, "sequences": self.sequences}
        if self.contexts is not None:
            manifest["profile_context"] = {"version": 1, "names": movie_context.NAMES, "note": movie_context.NOTE}
        (run / "movie_corpus.json").write_text(json.dumps(manifest), encoding="utf-8")
        times = np.concatenate([np.arange(row["length"]) * 1000 + row["start_ms"] for row in self.sequences])
        arrays = {} if self.contexts is None else {"profile_context": self.contexts}
        np.savez(run / "movie_corpus.npz", features=self.features, times_ms=times, **arrays)
        return manifest

    def train(self, label, epochs=12, previous=None, sample_fps=1., patience=5, **kwargs):
        run = self.root / label
        logs = []
        with patch.object(movie_sequence_data, "prepare_corpus", self.prepare):
            completed = learning.train_movie(self.root, {"movies": ["film-a", "film-b"]}, epochs,
                                             "cpu", logs.append, run=run, previous=previous, sample_fps=sample_fps,
                                             patience=patience, **kwargs)
        self.assertEqual(completed, run / "completed.json")
        self.assertEqual(json.loads((run / "movie_sources.json").read_text(encoding="utf-8")),
                         {"movies": ["film-a", "film-b"]})
        return run, logs

    def test_real_training_round_trip_and_portable_export(self):
        run, _ = self.train("电影 学习", epochs=24)
        metrics = json.loads((run / "movie_metrics.json").read_text(encoding="utf-8"))
        self.assertEqual((metrics["train_windows"], metrics["val_windows"]), (95, 47))
        self.assertEqual(metrics["validation_kind"], "independent-films")
        self.assertLess(min(row["train_loss"] for row in metrics["epochs"]), metrics["train_baseline_loss"])
        checkpoint = torch.load(run / "movie_temporal.pt", weights_only=True)
        self.assertGreater(float(checkpoint["state_dict"]["output.weight"].abs().sum()), 0)
        prediction = learning.predict_next(run, self.features[:16], steps=3)
        self.assertEqual(prediction["feature_names"], self.names)
        self.assertEqual(np.asarray(prediction["values"]).shape, (3, 4))
        self.assertTrue(np.isfinite(prediction["values"]).all())
        self.assertGreater(prediction["values"][0][2], 2)
        # A silent scene with seven people is a valid training input and forecast.
        target = self.root / "导出 模型.zip"
        self.assertEqual(learning.export_bundle(run, target), target)
        with zipfile.ZipFile(target) as archive:
            self.assertNotIn("movie_corpus.npz", archive.namelist())
            self.assertNotIn("movie_sources.json", archive.namelist())
            self.assertFalse(json.loads(archive.read("bundle.json"))["aecpp_direct_import"])
            self.assertIn("movie_evaluation.json", archive.namelist())
            exported = self.root / "portable"
            archive.extractall(exported)
        self.assertEqual(learning.predict_next(exported, self.features[:16], 3), prediction)

    def test_validation_never_changes_statistics_or_crosses_windows(self):
        self.features[96:] = 1000
        run, _ = self.train("holdout", epochs=1)
        checkpoint = torch.load(run / "movie_temporal.pt", weights_only=True)
        np.testing.assert_allclose(checkpoint["mean"], self.features[:96].astype(np.float64).mean(axis=0), rtol=1e-6)
        _, _, _, windows = learning._corpus(run)
        self.assertTrue(all(start < target < 96 for start, target in windows["train"]))
        self.assertTrue(all(96 <= start < target < 144 for start, target in windows["val"]))
        # Two consecutive training clips remain separate even with matching films.
        self.sequences = [self.sequence(0, 48, "train", "film-a"),
                          self.sequence(48, 48, "train", "film-a", 48000),
                          self.sequence(96, 48, "val", "film-a", 96000)]
        run, _ = self.train("within-film", epochs=1)
        manifest = json.loads((run / "completed.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["validation_kind"], "within-film")
        self.assertEqual(manifest["train_windows"], 94)
        _, _, _, windows = learning._corpus(run)
        self.assertFalse(any(start < 48 <= target for start, target in windows["train"]))

    def test_warm_start_preserves_scaling_when_training_data_changes(self):
        first, _ = self.train("first", epochs=1)
        second, _ = self.train("second", epochs=1, previous=first / "completed.json")
        self.assertTrue(json.loads((second / "completed.json").read_text(encoding="utf-8"))["warm_start"])
        self.features[:96, 2] += 5
        third, logs = self.train("third", epochs=1, previous=second)
        self.assertTrue(json.loads((third / "completed.json").read_text(encoding="utf-8"))["warm_start"])
        first_weights = torch.load(first / "movie_temporal.pt", weights_only=True)
        third_weights = torch.load(third / "movie_temporal.pt", weights_only=True)
        self.assertEqual(first_weights["mean"], third_weights["mean"])
        self.assertEqual(first_weights["scale"], third_weights["scale"])
        self.assertTrue(any("继续学习" in line for line in logs))
        fourth, _ = self.train("fourth", epochs=1, previous=third, sample_fps=2.)
        self.assertFalse(json.loads((fourth / "completed.json").read_text(encoding="utf-8"))["warm_start"])
        self.extractor_version += 1
        fifth, _ = self.train("fifth", epochs=1, previous=fourth, sample_fps=2.)
        self.assertFalse(json.loads((fifth / "completed.json").read_text(encoding="utf-8"))["warm_start"])

    def test_warm_start_rejects_past_training_in_current_validation(self):
        first, _ = self.train("provenance-first", epochs=1)
        # A new training film does not make the old training film unseen.
        self.sequences = [self.sequence(0, 96, "train", "film-c"), self.sequence(96, 48, "val", "film-a")]
        second, _ = self.train("provenance-rejected", epochs=1, previous=first)
        result = json.loads((second / "completed.json").read_text(encoding="utf-8"))
        self.assertFalse(result["warm_start"])
        self.assertIn("验证泄漏", result["warm_start_reason"])

    def test_legacy_provenance_and_transitive_history_are_preserved(self):
        first, _ = self.train("legacy-first", epochs=1)
        checkpoint = torch.load(first / "movie_temporal.pt", weights_only=True)
        checkpoint.pop("training_ranges")
        torch.save(checkpoint, first / "movie_temporal.pt")
        self.sequences = [self.sequence(0, 96, "train", "film-c"), self.sequence(96, 48, "val", "film-d")]
        second, _ = self.train("legacy-warm", epochs=1, previous=first)
        result = json.loads((second / "completed.json").read_text(encoding="utf-8"))
        self.assertTrue(result["warm_start"])
        history = torch.load(second / "movie_temporal.pt", weights_only=True)["training_ranges"]
        self.assertEqual({row["film_id"] for row in history}, {"film-a", "film-c"})
        self.sequences[1]["film_id"] = "film-a"
        third, _ = self.train("transitive-rejected", epochs=1, previous=second)
        self.assertFalse(json.loads((third / "completed.json").read_text(encoding="utf-8"))["warm_start"])
        (first / "movie_corpus.json").unlink()
        self.sequences[1]["film_id"] = "film-d"
        missing, _ = self.train("missing-provenance", epochs=1, previous=first)
        self.assertFalse(json.loads((missing / "completed.json").read_text(encoding="utf-8"))["warm_start"])

    def test_constant_input_early_stops_without_replacing_better_initial_candidate(self):
        self.features[:] = 0
        run, _ = self.train("constant", epochs=20, patience=3)
        completed = json.loads((run / "completed.json").read_text(encoding="utf-8"))
        self.assertEqual(completed["actual_epochs"], 3)
        self.assertEqual(completed["best_epoch"], 0)
        self.assertEqual(completed["best_origin"], "persistence-baseline")
        self.assertTrue(completed["early_stopped"])
        self.assertEqual(completed["val_loss"], 0)
        self.assertIsNone(completed["improvement_percent"])
        metrics = json.loads((run / "movie_metrics.json").read_text(encoding="utf-8"))
        self.assertTrue(all(row["learning_rate"] > 0 for row in metrics["epochs"]))

    def test_evaluation_reads_saved_candidate_without_mutating_weights(self):
        run, _ = self.train("evaluate", epochs=3)
        before = (run / "movie_temporal.pt").read_bytes()
        result = learning.evaluate_run(run)
        completed = json.loads((run / "completed.json").read_text(encoding="utf-8"))
        self.assertEqual((run / "movie_temporal.pt").read_bytes(), before)
        self.assertTrue(result["finite_predictions"])
        self.assertEqual(result["sample_count"], 47)
        self.assertEqual(result["split"], "val")
        self.assertEqual({row["name"] for row in result["features"]}, set(self.names))
        self.assertAlmostEqual(result["model_loss"], completed["val_loss"], places=6)
        self.assertAlmostEqual(result["improvement_percent"], completed["improvement_percent"], delta=1e-4)
        self.assertTrue(any(row["name"] == "people" for row in result["groups"]))

    def test_missing_modalities_are_not_reported_as_perfect_learning(self):
        self.names = ["luma", "audio_rms", "audio_available", "people_count", "people_available"]
        self.features = np.column_stack((self.features[:, 0], np.zeros((len(self.features), 4)))).astype(np.float32)
        run, _ = self.train("missing-modalities", epochs=1)
        result = learning.evaluate_run(run)
        rows = {row["name"]: row for row in result["features"]}
        self.assertEqual(rows["audio_rms"]["sample_count"], 0)
        self.assertIsNone(rows["audio_rms"]["model_loss"])
        self.assertIsNone(rows["people_count"]["baseline_loss"])
        self.assertIsNone(rows["people_count"]["improvement_percent"])
        self.assertEqual(rows["luma"]["sample_count"], 47)
        groups = {row["name"]: row for row in result["groups"]}
        self.assertEqual(groups["audio"]["sample_count"], 0)
        self.assertIsNone(groups["people"]["model_loss"])
        self.assertTrue(np.isfinite(result["model_loss"]))

    def test_training_only_is_labeled_and_invalid_inputs_rejected(self):
        self.sequences = [self.sequence(0, 144, "train", "film-a")]
        run, _ = self.train("no-validation", epochs=1)
        completed = json.loads((run / "completed.json").read_text(encoding="utf-8"))
        self.assertEqual(completed["validation_kind"], "none")
        self.assertIsNone(completed["val_loss"])
        self.assertIsNone(completed["baseline_loss"])
        with self.assertRaises(ValueError):
            learning.predict_next(run, [[0, 1, 2]])
        with self.assertRaises(ValueError):
            learning.predict_next(run, [[float("nan"), 1, 2, 3]])
        with self.assertRaises(ValueError):
            learning.export_bundle(run, run / "movie_temporal.pt")
        saved = torch.load(run / "movie_temporal.pt", weights_only=True)
        bad = self.root / "invalid.pt"
        saved["scale"][0] = float("inf")
        torch.save(saved, bad)
        with self.assertRaisesRegex(ValueError, "标准化"):
            learning.predict_next(bad, self.features[:4])
        saved["scale"][0] = 1.
        saved["context_steps"] = 0
        torch.save(saved, bad)
        with self.assertRaisesRegex(ValueError, "上下文"):
            learning.predict_next(bad, self.features[:4])
        self.sequences[0]["split"] = "val"
        with self.assertRaisesRegex(ValueError, "没有可训练"):
            self.train("only-validation", epochs=1)
        self.assertFalse((self.root / "only-validation" / "completed.json").exists())
        with self.assertRaisesRegex(ValueError, "训练轮数"):
            self.train("invalid-epochs", epochs=0)
        self.assertFalse((self.root / "invalid-epochs").exists())

    def test_profile_context_is_an_input_not_a_prediction_target_and_old_model_upgrades(self):
        legacy, _ = self.train("before-context", epochs=3)
        old = torch.load(legacy / "movie_temporal.pt", weights_only=True)
        profile = {"status": "confirmed", "cast": [{"role": "旅人", "actor": "演员甲", "aliases": []}],
                   "synopsis": "一段漫长的返乡旅途。", "events": []}
        self.contexts = movie_context.encode_profile(profile, np.arange(len(self.features)) * 1000)
        run, _ = self.train("with-context", epochs=12, previous=legacy)
        saved = torch.load(run / "movie_temporal.pt", weights_only=True)
        self.assertEqual(saved["mean"], old["mean"])
        self.assertEqual(len(saved["feature_names"]), 4)
        self.assertEqual(saved["state_dict"]["gru.weight_ih_l0"].shape[1], 103)
        self.assertGreater(float(saved["state_dict"]["gru.weight_ih_l0"][:, 4:].abs().sum()), 0)
        result = learning.predict_next(run, self.features[:16], steps=2, profile=profile)
        self.assertEqual(np.asarray(result["values"]).shape, (2, 4))
        self.assertTrue(result["profile_conditioned"])
        without_context = learning.predict_next(run, self.features[:16], steps=2)
        self.assertFalse(np.allclose(result["values"], without_context["values"], rtol=0, atol=1e-7))
        evaluation = learning.evaluate_run(run)
        self.assertEqual(len(evaluation["features"]), 4)
        self.assertTrue(evaluation["profile_conditioned"])
        self.assertTrue(any("剧情先验" in warning for warning in evaluation["warnings"]))
        profile["events"] = [{"start_ms": 0, "end_ms": 4000, "text": "旅途开始", "roles": []}]
        with self.assertRaisesRegex(ValueError, "history_times_ms"):
            learning.predict_next(run, self.features[:16], profile=profile)

    def test_restart_and_custom_rate_are_recorded_and_balanced_film_mass_is_equal(self):
        previous, _ = self.train("previous", epochs=2)
        run, _ = self.train("fresh", epochs=2, previous=previous, restart=True, learning_rate=.0003)
        completed = json.loads((run / "completed.json").read_text(encoding="utf-8"))
        self.assertFalse(completed["warm_start"])
        self.assertTrue(completed["restart"])
        self.assertEqual(completed["initial_learning_rate"], .0003)
        with self.assertRaisesRegex(ValueError, "学习率"):
            self.train("bad-rate", learning_rate=float("nan"))
        manifest = {"sequences": [self.sequence(0, 4, "train", "short"), self.sequence(4, 10, "train", "long")]}
        windows = [(0, target) for target in range(1, 4)] + [(4, target) for target in range(5, 14)]
        weights = learning._balanced_weights(manifest, windows)
        self.assertAlmostEqual(sum(weights[:3]), sum(weights[3:]))


if __name__ == "__main__":
    unittest.main()
