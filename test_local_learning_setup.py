"""Local setup transaction, service ownership, cache reuse and real progress relay."""
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import local_learning_setup as setup
from video_analysis import load_settings, save_settings, SETTINGS
from video_semantics import AnalysisCancelled, ModelSetupRequired


def make_model(path):
    path.mkdir(parents=True, exist_ok=True)
    for name in ("config.json", "tokenizer.json", "model.bin", "vocabulary.txt"):
        (path/name).write_text("fixture", encoding="utf-8")
    return path.resolve()


class LocalLearningSetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.root = self.folder/"project"
        self.cache = self.folder/"models"
        self.old = {"provider": "openai-compatible", "base_url": "https://example.test/v1",
                    "visual_model": "old", "model": "old", "visual_enabled": False, "semantic_enabled": False}
        save_settings(self.root, self.old)
        self.original = (self.root/SETTINGS).read_bytes()
        self.events = []
        self.patch("ASR_CACHE", self.cache)
        self.dependencies = self.patch("_dependencies", return_value=None)
        self.models = self.patch("_models", return_value=[setup.VISION_MODEL])
        self.verify = self.patch("_verify_vision", return_value=None)
        self.space = self.patch("_free_space", return_value=None)
        self.process = Mock()
        self.process.poll.return_value = None
        self.popen = self.patch("subprocess.Popen", return_value=self.process)
        self.vision = self.patch("prepare_vision_model", side_effect=AssertionError("unexpected model download"))
        self.asr = self.patch("download_model", side_effect=AssertionError("unexpected ASR download"))

    def patch(self, name, *args, **kwargs):
        target = setup
        parts = name.split(".")
        for part in parts[:-1]:
            target = getattr(target, part)
        patcher = patch.object(target, parts[-1], *args, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def test_reuses_local_models_enables_all_analysis_without_remote_inference(self):
        model = make_model(self.cache/"tiny")
        result = setup.prepare_local_learning_models(self.root, report=self.events.append)
        self.assertEqual(result["reused"], {"vision": True, "asr": True})
        self.assertFalse(result["service_started"])
        saved = load_settings(self.root)
        self.assertTrue(all(saved[key] for key in ("enabled", "asr_enabled", "semantic_enabled", "visual_enabled", "keyframes")))
        self.assertEqual(saved["provider"], "ollama")
        self.assertEqual(saved["base_url"], "http://127.0.0.1:11434")
        self.assertEqual(saved["visual_model"], saved["model"])
        self.assertEqual(saved["api_key_env"], "")
        self.assertEqual(saved["asr_model_path"], str(model))
        self.assertEqual(result["settings"], saved)
        self.vision.assert_not_called(); self.asr.assert_not_called(); self.popen.assert_not_called()
        self.assertEqual(self.events[-1]["phase"], "models-ready")

    def test_user_selected_local_asr_is_preferred_and_invalid_selection_falls_back(self):
        preferred = make_model(self.folder/"custom-base")
        tiny = make_model(self.cache/"tiny")
        save_settings(self.root, {"asr_model_path": str(preferred), "asr_model": "base"})
        result = setup.prepare_local_learning_models(self.root)
        self.assertEqual(result["asr_model_path"], str(preferred))
        self.assertEqual(result["settings"]["asr_model"], "base")
        (preferred/"model.bin").unlink()
        result = setup.prepare_local_learning_models(self.root)
        self.assertEqual(result["asr_model_path"], str(tiny))
        self.asr.assert_not_called()

    def test_cached_base_is_used_before_downloading_tiny(self):
        base = make_model(self.cache/"base")
        result = setup.prepare_local_learning_models(self.root)
        self.assertEqual(result["asr_model_path"], str(base))
        self.assertEqual(result["settings"]["asr_model"], "base")
        self.asr.assert_not_called()

    def test_missing_models_download_presets_and_relay_observed_bytes(self):
        self.models.return_value = []
        def vision(**kwargs):
            self.assertEqual(kwargs["base_url"], setup.LOCAL_BASE_URL)
            kwargs["report"]({"phase": "vision-download", "completed": 42, "total": 100,
                              "percent": 42., "url": setup.LOCAL_BASE_URL+"/api/pull", "message": "actual fixture bytes"})
            return setup.VISION_MODEL
        def asr(size, cache, **kwargs):
            self.assertEqual(size, "tiny")
            kwargs["report"]({"phase": "model_download", "downloaded_bytes": 30, "total_bytes": 90,
                              "url": "https://huggingface.co/fixture/model.bin", "message": "actual fixture bytes"})
            return make_model(Path(cache)/size)
        self.vision.side_effect, self.asr.side_effect = vision, asr
        result = setup.prepare_local_learning_models(self.root, report=self.events.append)
        self.assertEqual(result["reused"], {"vision": False, "asr": False})
        self.assertEqual([row["completed"] for row in self.events if "completed" in row], [42])
        self.assertEqual([row["downloaded_bytes"] for row in self.events if "downloaded_bytes" in row], [30])
        self.assertEqual(self.events[-1]["phase"], "models-ready")

    def test_service_is_started_hidden_and_kept_for_subsequent_analysis(self):
        make_model(self.cache/"tiny")
        self.models.side_effect = [ModelSetupRequired("not running"), [setup.VISION_MODEL]]
        with patch.object(setup, "_ollama_executable", return_value="C:/Ollama/ollama.exe"):
            result = setup.prepare_local_learning_models(self.root)
        self.assertTrue(result["service_started"])
        args = self.popen.call_args
        self.assertEqual(args.args[0], ["C:/Ollama/ollama.exe", "serve"])
        self.assertEqual(args.kwargs["env"]["OLLAMA_HOST"], "127.0.0.1:11434")
        self.assertEqual(args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(args.kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.process.terminate.assert_not_called()

    def test_failure_stops_only_newly_owned_service_and_keeps_old_settings(self):
        self.models.side_effect = [ModelSetupRequired("not running"), [setup.VISION_MODEL]]
        self.asr.side_effect = ValueError("fixture network failure")
        with patch.object(setup, "_ollama_executable", return_value="C:/Ollama/ollama.exe"):
            with self.assertRaisesRegex(ValueError, "fixture network failure"):
                setup.prepare_local_learning_models(self.root)
        self.process.terminate.assert_called_once()
        self.assertEqual((self.root/SETTINGS).read_bytes(), self.original)
        self.popen.reset_mock(); self.process.reset_mock(); self.models.side_effect = None
        self.models.return_value = [setup.VISION_MODEL]
        with self.assertRaisesRegex(ValueError, "fixture network failure"):
            setup.prepare_local_learning_models(self.root)
        self.popen.assert_not_called(); self.process.terminate.assert_not_called()

    def test_cancel_before_setup_during_download_and_before_publication_preserve_settings(self):
        stop = threading.Event(); stop.set()
        with self.assertRaises(AnalysisCancelled):
            setup.prepare_local_learning_models(self.root, stop=stop)
        self.dependencies.assert_not_called()
        stop.clear(); self.models.return_value = []
        def download(**kwargs):
            stop.set()
        self.vision.side_effect = download
        with self.assertRaises(AnalysisCancelled):
            setup.prepare_local_learning_models(self.root, stop=stop)
        self.asr.assert_not_called()
        stop.clear(); self.models.return_value = [setup.VISION_MODEL]
        make_model(self.cache/"tiny")
        def report(event):
            if event["phase"] == "model-settings":
                stop.set()
        with self.assertRaises(AnalysisCancelled):
            setup.prepare_local_learning_models(self.root, stop=lambda: stop.is_set(), report=report)
        self.assertEqual((self.root/SETTINGS).read_bytes(), self.original)

    def test_missing_dependencies_or_ollama_do_not_publish_partial_settings(self):
        self.dependencies.side_effect = ValueError("FFmpeg missing")
        with self.assertRaisesRegex(ValueError, "FFmpeg"):
            setup.prepare_local_learning_models(self.root)
        self.models.assert_not_called()
        self.dependencies.side_effect = None
        self.models.side_effect = ModelSetupRequired("not running")
        with patch.object(setup, "_ollama_executable", side_effect=ValueError("未找到本地 Ollama")):
            with self.assertRaisesRegex(ValueError, "Ollama"):
                setup.prepare_local_learning_models(self.root)
        self.popen.assert_not_called()
        self.assertEqual((self.root/SETTINGS).read_bytes(), self.original)

    def test_invalid_vision_capability_does_not_claim_ready_or_change_settings(self):
        self.verify.side_effect = ValueError("视觉与文本能力缺失")
        with self.assertRaisesRegex(ValueError, "能力"):
            setup.prepare_local_learning_models(self.root, report=self.events.append)
        self.asr.assert_not_called()
        self.assertFalse(any(row["phase"] == "models-ready" for row in self.events))
        self.assertEqual((self.root/SETTINGS).read_bytes(), self.original)

    def test_late_cancel_after_publication_returns_observed_success(self):
        make_model(self.cache/"tiny")
        stop = threading.Event()
        def report(event):
            if event["phase"] == "models-ready":
                stop.set()
        result = setup.prepare_local_learning_models(self.root, report=report, stop=stop)
        self.assertTrue(stop.is_set())
        self.assertEqual(load_settings(self.root), result["settings"])


class LocalServiceChecks(unittest.TestCase):
    def test_dependency_failures_are_actionable_before_model_download(self):
        with patch.object(setup.shutil, "which", return_value=None), patch.object(setup.importlib, "import_module") as load:
            with self.assertRaisesRegex(ValueError, "ffmpeg / ffprobe"):
                setup._dependencies()
        load.assert_not_called()
        with patch.object(setup.shutil, "which", return_value="executable"), patch.object(setup.importlib, "import_module", side_effect=OSError("DLL missing")):
            with self.assertRaisesRegex(ValueError, "setup.cmd"):
                setup._dependencies()

    def test_low_disk_space_does_not_start_a_download(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(setup.shutil, "disk_usage", return_value=Mock(free=10)):
            with self.assertRaisesRegex(ValueError, "空间不足"):
                setup._free_space(Path(temporary)/"not-created"/"models", 4*1024**3, "模型缓存")
            self.assertFalse((Path(temporary)/"not-created").exists())

    def test_verify_vision_checks_both_local_capabilities_without_credentials(self):
        with patch.object(setup, "_request", return_value={"capabilities": ["completion", "vision"]}) as request:
            setup._verify_vision(None)
        self.assertEqual(request.call_args.args[0], setup.LOCAL_BASE_URL+"/api/show")
        self.assertNotIn("api_key", request.call_args.kwargs)
        for capabilities in ([], ["completion"], ["vision"], "vision completion"):
            with self.subTest(capabilities=capabilities), patch.object(setup, "_request", return_value={"capabilities": capabilities}):
                with self.assertRaises(ValueError):
                    setup._verify_vision(None)

    def test_cancel_waiting_for_own_service_cleans_child(self):
        process = Mock(); process.poll.return_value = None
        stop = threading.Event()
        def models(_):
            if process in started:
                stop.set()
            raise ModelSetupRequired("not ready")
        started = []
        def launch(*args, **kwargs):
            started.append(process)
            return process
        with patch.object(setup, "_models", side_effect=models), patch.object(setup, "_ollama_executable", return_value="ollama"), patch.object(setup.subprocess, "Popen", side_effect=launch):
            with self.assertRaises(AnalysisCancelled):
                setup._ensure_service(lambda event: None, stop)
        process.terminate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
