import copy
import json
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import unittest

from aecpp_export import build_project, export_project, export_script, to_script


def example():
    return {"version": 1, "title": "加班奇遇", "characters": [
        {"id": "ming", "name": "小明"}, {"id": "boss", "name": "老板"}], "shots": [
        {"id": "one", "scene": "办公室", "speaker": "ming", "text": "老板，我要下班！",
         "action": "shake", "emotion": "angry", "camera": "medium", "duration": 3.},
        {"id": "two", "scene": "办公室", "speaker": "boss", "text": "先把这份报告写完。",
         "action": "talk", "emotion": "happy", "camera": "medium", "duration": 3.},
        {"id": "three", "scene": "办公室", "speaker": "", "text": "小明震惊得跳了起来。",
         "action": "jump", "emotion": "shocked", "camera": "close", "duration": 3.}]}


class AecppExportTests(unittest.TestCase):
    def test_editable_layers_native_subtitles_and_bounded_keys(self):
        project = build_project(example())
        self.assertEqual((project["format"], project["version"]), ("aecpp-project", 6))
        scene = project["scene"]
        self.assertEqual(scene["animation"]["duration"], 9.)
        self.assertEqual(len(scene["subtitles"]), 3)
        self.assertEqual(scene["subtitles"][2]["style"]["content"], "小明震惊得跳了起来。")
        self.assertEqual([(r["start"], r["end"]) for r in scene["timelineRegions"]], [(0, 3), (3, 6), (6, 9)])
        ids = {layer["id"] for layer in scene["layers"]}
        self.assertEqual(len(ids), len(scene["layers"]))
        self.assertFalse(any(layer.get("imagePath") or layer.get("actor") for layer in scene["layers"]))
        for track in scene["animation"]["tracks"]:
            self.assertIn(track["targetLayerId"], ids)
            keys = track["keyframes"]
            self.assertEqual([k["time"] for k in keys], sorted(k["time"] for k in keys))
            self.assertTrue(all(0 <= key["time"] <= 9 for key in keys))
            self.assertTrue(all(abs(key["transform"]["rotation"]) <= math.pi for key in keys))

    def test_narration_moves_only_its_named_subject_and_keeps_empty_speaker(self):
        board = example()
        board["shots"] = [board["shots"][2]]
        scene = build_project(board)["scene"]
        heads = {layer["id"]: layer["name"] for layer in scene["layers"] if layer["name"].endswith(" / 头")}
        tracks = {heads[t["targetLayerId"]]: t["keyframes"] for t in scene["animation"]["tracks"] if t["targetLayerId"] in heads}
        self.assertGreater(len({k["transform"]["position"]["y"] for k in tracks["小明 / 头"]}), 2)
        self.assertTrue(all(k["transform"]["opacity"] == 0 for k in tracks["老板 / 头"]))
        script = to_script(board)
        self.assertEqual(script["scenes"][0]["dialogue"][0]["characterId"], "")
        self.assertEqual(script["scenes"][0]["characters"][0]["characterId"], "character_1")
        board["shots"][0].update(text="他突然跳了起来。", actor="boss")
        self.assertEqual(to_script(board)["scenes"][0]["characters"][0]["characterId"], "character_2")

    def test_indoor_and_outdoor_backgrounds_switch_at_scene_boundaries(self):
        board = example()
        for shot, setting in zip(board["shots"], ("客厅", "公园", "办公室")):
            shot["scene"] = setting
        scene = build_project(board)["scene"]
        layers = {layer["id"]: layer for layer in scene["layers"]}
        tracks = {layers[t["targetLayerId"]]["name"]: t["keyframes"]
                  for t in scene["animation"]["tracks"]}
        for name, expected in (("客厅 / 沙发靠背", [1, 0, 0]),
                               ("室外 / 太阳", [0, 1, 0]),
                               ("办公室 / 电脑屏幕", [0, 0, 1])):
            keys = tracks[name]
            self.assertEqual([k["time"] for k in keys], [0, 3, 6])
            self.assertEqual([k["transform"]["opacity"] for k in keys], expected)
            self.assertTrue(all(k["interpolation"] == 0 for k in keys))

    def test_publication_preserves_existing_project_and_cancellation(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)/"中文工程.aecpp"
            self.assertEqual(export_project(example(), target), target)
            original = target.read_bytes()
            with self.assertRaisesRegex(ValueError, "已存在"):
                export_project(example(), target)
            self.assertEqual(target.read_bytes(), original)
            cancelled = Path(folder)/"cancelled.aecpp"
            def stop(message):
                if "保存" in message:
                    raise RuntimeError("cancelled")
            with self.assertRaises(RuntimeError):
                export_project(example(), cancelled, stop)
            self.assertFalse(cancelled.exists())
            self.assertFalse(list(Path(folder).glob("*.tmp")))
            script = Path(folder)/"script.json"
            export_script(example(), script)
            with self.assertRaisesRegex(ValueError, "已存在"):
                export_script(example(), script)

    def test_invalid_input_is_not_published_and_long_script_is_not_truncated(self):
        for field, invalid in (("speaker", "unknown"), ("actor", "unknown"), ("action", "fly"),
                               ("duration", float("nan")), ("duration", True)):
            board = example()
            board["shots"][0][field] = invalid
            with self.subTest(field=field), self.assertRaises(ValueError):
                build_project(board)
        board = example()
        board["shots"] = [copy.deepcopy(board["shots"][0]) for _ in range(65)]
        with self.assertRaisesRegex(ValueError, "64"):
            to_script(board)
        board = example()
        board["shots"][0]["text"] = "长"*4001
        with self.assertRaisesRegex(ValueError, "台词"):
            to_script(board)

    def test_no_cast_story_and_unverified_resource_ids(self):
        board = example()
        board["characters"] = []
        board["shots"] = [dict(board["shots"][0], speaker="", text="天亮了。")]
        self.assertEqual(to_script(board)["scenes"][0]["characters"], [])
        self.assertEqual(build_project(board)["scene"]["subtitles"][0]["style"]["content"], "天亮了。")
        script = to_script(example())
        self.assertTrue(all("actionId" not in c and "expressionId" not in c
                            for scene in script["scenes"] for c in scene["characters"]))


AECPP_ROOT = Path(__file__).resolve().parent.parent/"AECPP"
ENGINE = Path(os.environ.get("AECPP_ENGINE_PATH", AECPP_ROOT/"out/build/mingw-release/aecpp.exe"))


class NativeConsumerTests(unittest.TestCase):
    @unittest.skipUnless(ENGINE.is_file(), "Local AECPP engine is needed for consumer verification")
    def test_actual_engine_opens_renders_and_saves_editable_project(self):
        from PIL import Image, ImageStat
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            project = export_project(example(), folder/"source.aecpp")
            process = subprocess.Popen([str(ENGINE), "--engine", "--asset-root", str(folder), "--session-root", str(folder)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            responses = queue.Queue()
            def read():
                for line in process.stdout:
                    try:
                        responses.put(json.loads(line))
                    except ValueError:
                        responses.put({"error": line})
            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            request_id = 0
            def request(method, params):
                nonlocal request_id
                request_id += 1
                process.stdin.write(json.dumps({"id": request_id, "method": method, "params": params})+"\n")
                process.stdin.flush()
                while True:
                    result = responses.get(timeout=30)
                    self.assertNotIn("error", result, result)
                    if result.get("id") == request_id:
                        return result["result"]
            try:
                state = request("openProject", {"path": str(project)})
                self.assertEqual((state["name"], state["duration"], state["subtitleCount"]), ("加班奇遇", 9, 3))
                frames = []
                for time in (1., 4., 7.5):
                    rendered = request("renderFrame", {"time": time, "width": 640, "height": 360, "format": "png"})
                    with Image.open(rendered["path"]) as image:
                        self.assertEqual(image.size, (640, 360))
                        self.assertGreater(sum(ImageStat.Stat(image.convert("RGB")).var), 100)
                        frames.append(image.tobytes())
                self.assertEqual(len(set(frames)), 3)
                saved = folder/"roundtrip.aecpp"
                request("saveProject", {"path": str(saved)})
                roundtrip = json.loads(saved.read_text(encoding="utf-8"))
                self.assertEqual(len(roundtrip["scene"]["layers"]), len(build_project(example())["scene"]["layers"]))
                self.assertEqual(roundtrip["scene"]["subtitles"][0]["style"]["content"], "小明：老板，我要下班！")
            finally:
                process.stdin.close()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                reader.join(timeout=2)
                process.stdout.close()
                process.stderr.close()

    @unittest.skipUnless(shutil.which("node") and (AECPP_ROOT/"electron/renderer/script-workflow-model.js").is_file(),
                         "Local AECPP structured-script consumer is needed")
    def test_actual_aecpp_script_parser_accepts_output(self):
        with tempfile.TemporaryDirectory() as folder:
            script = export_script(example(), Path(folder)/"script.json")
            model = AECPP_ROOT/"electron/renderer/script-workflow-model.js"
            js = ('import fs from "node:fs"; const m=await import(process.env.YINGGE_AECPP_MODEL); '
                  'const s=m.parseScriptText(fs.readFileSync(process.env.YINGGE_AECPP_SCRIPT,"utf8")); '
                  'console.log(JSON.stringify(s));')
            result = subprocess.run(["node", "--input-type=module", "-e", js], capture_output=True,
                text=True, encoding="utf-8", timeout=30,
                env=dict(os.environ, YINGGE_AECPP_MODEL=model.as_uri(), YINGGE_AECPP_SCRIPT=str(script)))
            self.assertEqual(result.returncode, 0, result.stderr)
            parsed = json.loads(result.stdout)
            self.assertEqual(parsed["title"], "加班奇遇")
            self.assertEqual(len(parsed["scenes"]), 3)
            self.assertEqual(parsed["scenes"][2]["dialogue"][0]["text"], "小明震惊得跳了起来。")


if __name__ == "__main__":
    unittest.main()
