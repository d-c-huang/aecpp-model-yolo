"""Actual video samples and local HTTP contracts for visual observations."""
import base64
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest

from PIL import Image

from test_video_semantics import server
import video_visual as visual
from video_semantics import AnalysisCancelled, ModelSetupRequired


def observation(**changes):
    return {"description": "人物向后退，张大嘴露出意外表情", "action": "walk",
            "emotion": "shocked", "camera": "medium", "confidence": .82,
            "evidence": "前后画面人物身体位置改变；可见张嘴和睁眼。动作推断仍待核对。", **changes}


def response(path):
    if path == "/api/show":
        return {"capabilities": ["completion", "vision"]}
    return {"message": {"content": json.dumps(observation(), ensure_ascii=False)}}


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg creates actual frame fixtures")
class VisualTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder/"video.mkv"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=96x64:rate=5:duration=6", "-c:v", "ffv1", str(self.source)],
                       capture_output=True, check=True)
        self.target = self.folder/"frames"

    def analyse(self, url, **kwargs):
        args = {"duration_ms": 6000, "observations": [{"start_ms": 200, "end_ms": 1500, "text": "真的发生了吗"}],
                "cut_candidates_ms": [2000, 4000], "output_dir": self.target, "model": "fixture-vision",
                "base_url": url, **kwargs}
        return visual.analyse_visual(self.source, **args)

    def test_actual_multiframe_ollama_request_preserves_times_and_context(self):
        events = []
        with server(value=response) as (url, calls):
            result = self.analyse(url, report=events.append)
        self.assertEqual([call["path"] for call in calls], ["/api/show", "/api/chat"])
        payload = calls[1]["body"]
        self.assertEqual(payload["format"], visual.VISUAL_SCHEMA)
        self.assertEqual(payload["keep_alive"], 300)
        self.assertNotIn("think", payload)
        self.assertEqual(len(payload["messages"][1]["images"]), 3)
        actual_images = []
        for image_data in payload["messages"][1]["images"]:
            raw = base64.b64decode(image_data)
            with Image.open(io.BytesIO(raw)) as image:
                self.assertEqual(image.size, (96, 64))
            actual_images.append(raw)
        self.assertEqual(len(set(actual_images)), 3)
        prompt = json.loads(payload["messages"][1]["content"])
        self.assertEqual(prompt["sampledFrameTimesMs"], [1000, 3000, 5000])
        self.assertEqual(prompt["subtitleContextOnly"], "真的发生了吗")
        clip = result["clips"][0]
        self.assertEqual((clip["start_ms"], clip["end_ms"]), (0, 6000))
        self.assertEqual([frame["time_ms"] for frame in clip["frames"]], [1000, 3000, 5000])
        self.assertEqual(clip["context_text"], "真的发生了吗")
        self.assertEqual(clip["text"], observation()["description"])
        self.assertEqual(clip["cut_times_ms"], [2000, 4000])
        self.assertEqual(result["rhythm_method"], "luma-cut-candidates")
        self.assertEqual(result["coverage"]["sampled_duration_ms"], 6000)
        self.assertEqual(result["review_status"], "unreviewed")
        waiting = [event for event in events if "等待视觉模型返回" in event.get("message", "")]
        self.assertEqual(len(waiting), 1)
        self.assertEqual(waiting[0]["phase"], "visual-understanding")
        self.assertEqual((waiting[0]["clip_index"], waiting[0]["clip_count"]), (1, 1))
        self.assertEqual((waiting[0]["start_ms"], waiting[0]["end_ms"]), (0, 6000))
        self.assertIn("第 1/1 段", waiting[0]["message"])
        self.assertIn("0.0–6.0 秒", waiting[0]["message"])
        self.assertIn("动作、表情、景别", waiting[0]["message"])
        self.assertIn("fixture-vision", waiting[0]["message"])
        self.assertEqual(waiting[0]["url"], url+"/api/chat")
        self.assertFalse(any(event.get("phase") == "model-request" and event.get("url", "").endswith("/api/chat")
                             for event in events))
        self.assertEqual(events[-1]["percent"], 100)

    def test_openai_multimodal_payload_and_key_never_leak_to_results(self):
        value = {"choices": [{"message": {"content": json.dumps(observation())}}]}
        events = []
        with server(value=value) as (url, calls):
            result = self.analyse(url+"/v1", provider="openai-compatible", api_key="runtime-secret", report=events.append)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["path"], "/v1/chat/completions")
        content = calls[0]["body"]["messages"][1]["content"]
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual([item["type"] for item in content[1:]], ["image_url"]*3)
        self.assertTrue(all(item["image_url"]["url"].startswith("data:image/jpeg;base64,") for item in content[1:]))
        self.assertNotIn("runtime-secret", json.dumps([result, events]))

    def test_text_model_is_rejected_before_extracting_or_submitting_frames(self):
        with server(value={"capabilities": ["completion"]}) as (url, calls):
            with self.assertRaisesRegex(ModelSetupRequired, "vision"):
                self.analyse(url)
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.target.exists())

    def test_thinking_is_disabled_only_when_the_model_declares_support(self):
        def thinking(path):
            return {"capabilities": ["completion", "vision", "thinking"]} if path == "/api/show" else response(path)
        with server(value=thinking) as (url, calls):
            self.analyse(url)
        self.assertIs(calls[1]["body"]["think"], False)

    def test_empty_model_and_invalid_inputs_make_no_requests(self):
        invalid = [{"model": ""}, {"duration_ms": True}, {"max_clips": 61}, {"max_clips": 0},
                   {"cut_candidates_ms": [6001]}, {"cut_candidates_ms": [True]},
                   {"observations": [{"start_ms": 0, "end_ms": 7000, "text": "x"}]}, {"timeout": float("nan")}]
        with server(value=response) as (url, calls):
            for kwargs in invalid:
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    self.analyse(url, **kwargs)
        self.assertFalse(calls)

    def test_unknown_labels_preserved_and_silent_video_can_be_analysed(self):
        def unknown(path):
            return response(path) if path == "/api/show" else {"message": {"content": json.dumps(
                observation(action="unknown", emotion="unknown", camera="unknown", confidence=.1))}}
        with server(value=unknown) as (url, calls):
            result = self.analyse(url, observations=[], cut_candidates_ms=[])
        self.assertEqual(result["clips"][0]["action"], "unknown")
        self.assertEqual(result["clips"][0]["context_text"], "")
        self.assertEqual(result["clips"][0]["confidence"], .1)
        self.assertEqual(len(calls), 2)

    def test_invented_times_and_invalid_predictions_remove_extracted_frames(self):
        invalid = [observation(start_ms=99999), observation(action="fly"), observation(confidence=True),
                   observation(confidence=float("nan")), observation(confidence=1.1), observation(evidence=""),
                   observation(description={"nested": "not text"})]
        for value in invalid:
            def handler(path):
                return response(path) if path == "/api/show" else {"message": {"content": json.dumps(value)}}
            with self.subTest(value=value), server(value=handler) as (url, _):
                with self.assertRaises(ValueError):
                    self.analyse(url)
            self.assertEqual(list(self.target.glob("*.jpg")), [])

    def test_cancellation_during_sampling_cleans_only_own_files(self):
        self.target.mkdir()
        existing = self.target/"existing.jpg"
        existing.write_bytes(b"existing-user-data")
        stop = threading.Event()
        def report(event):
            if event.get("frame_index") == 1:
                stop.set()
        with server(value=response) as (url, calls), self.assertRaises(AnalysisCancelled):
            self.analyse(url, stop=stop, report=report)
        self.assertEqual(len(calls), 1)
        self.assertEqual(list(self.target.glob("*.jpg")), [existing])

    def test_cancellation_before_visual_request_never_publishes_partial_analysis(self):
        stop = threading.Event()
        def report(event):
            if event.get("phase") == "visual-understanding":
                stop.set()
        with server(value=response) as (url, calls), self.assertRaises(AnalysisCancelled):
            self.analyse(url, stop=stop, report=report)
        self.assertEqual(len(calls), 1)
        self.assertEqual(list(self.target.glob("*.jpg")), [])

    def test_source_change_during_inference_invalidates_output(self):
        def report(event):
            if event.get("phase") == "visual-understanding" and event.get("percent") == 100:
                with self.source.open("ab") as stream:
                    stream.write(b"source-changed")
        with server(value=response) as (url, _), self.assertRaisesRegex(ValueError, "原片发生变化"):
            self.analyse(url, report=report)
        self.assertEqual(list(self.target.glob("*.jpg")), [])

    def test_no_duplicate_single_frame_motion_evidence(self):
        with server(value=response) as (url, calls), self.assertRaisesRegex(ValueError, "足够"):
            self.analyse(url, duration_ms=100, observations=[], cut_candidates_ms=[])
        self.assertEqual(len(calls), 1)
        self.assertEqual(list(self.target.glob("*.jpg")), [])

    def test_time_beyond_actual_video_is_not_used(self):
        with server(value=response) as (url, calls), self.assertRaisesRegex(ValueError, "实际时长"):
            self.analyse(url, duration_ms=100_000, observations=[])
        self.assertEqual(len(calls), 1)
        self.assertEqual(list(self.target.glob("*.jpg")), [])


class WindowTests(unittest.TestCase):
    def test_truncated_and_empty_final_answers_have_actionable_errors(self):
        for provider, response_value in (
            ("ollama", {"done_reason": "length", "message": {"content": "", "thinking": "private trace"}}),
            ("openai-compatible", {"choices": [{"finish_reason": "length", "message": {"content": "{incomplete"}}]}),
        ):
            with self.subTest(provider=provider), self.assertRaisesRegex(ValueError, "长度上限") as error:
                visual._parse(response_value, provider)
            self.assertNotIn("private trace", str(error.exception))
        with self.assertRaisesRegex(ValueError, "没有最终分析"):
            visual._parse({"message": {"content": "", "thinking": "private trace"}}, "ollama")
        with self.assertRaisesRegex(ValueError, "没有返回最终分析"):
            visual._parse({"message": {"content": " "}}, "ollama")

    def test_sparse_windows_cover_start_middle_and_end_without_claiming_whole_film(self):
        windows = visual._windows(3_600_000, 12)
        self.assertEqual(len(windows), 12)
        self.assertEqual(windows[0]["start_ms"], 0)
        self.assertEqual(windows[-1]["end_ms"], 3_600_000)
        self.assertTrue(all(window["end_ms"]-window["start_ms"] <= 8000 for window in windows))
        self.assertEqual(visual._covered_duration(windows), 12*8000)
        self.assertEqual(visual._windows(20_000, 1)[0]["start_ms"], 6000)

    def test_overlapping_short_windows_do_not_double_count_coverage(self):
        windows = visual._windows(10_000, 12)
        self.assertEqual(len(windows), 2)
        self.assertEqual(visual._covered_duration(windows), 10_000)


if __name__ == "__main__":
    unittest.main()
