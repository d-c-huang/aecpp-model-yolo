"""Local HTTP contracts, cancelled work, and actual keyframe evidence."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import video_semantics as semantics


def analysis():
    return {"title": "误会升级", "summary": "两人在对白中发生误会", "beats": ["提出问题", "意外回应"],
            "conflict": "双方理解不同", "humor": "对白反差可能形成笑点", "editingAdvice": "可尝试回应前短暂停顿，需人工核对"}


def transcript():
    return {"version": 1, "unit": "ms", "segments": [
        {"startMs": 0, "endMs": 1000, "text": "你今天迟到了"},
        {"startMs": 1200, "endMs": 2400, "text": "那是因为我来早了明天"}]}


@contextmanager
def server(*, value=None, status=200, delay=0, headers=None, body_delay=0, split_body=False):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send()
        def do_POST(self):
            self.send()
        def send(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else None
            calls.append({"path": self.path, "body": json.loads(body) if body else None,
                          "authorization": self.headers.get("Authorization")})
            time.sleep(delay)
            response = value(self.path) if callable(value) else value
            if response is None:
                response = {"message": {"content": json.dumps(analysis(), ensure_ascii=False)}}
            raw = response if isinstance(response, bytes) else json.dumps(response, ensure_ascii=False).encode()
            try:
                self.send_response(status)
                for key, val in (headers or {}).items():
                    self.send_header(key, val)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.flush()
                if split_body:
                    middle = len(raw)//2
                    self.wfile.write(raw[:middle])
                    self.wfile.flush()
                    time.sleep(body_delay)
                    self.wfile.write(raw[middle:])
                else:
                    time.sleep(body_delay)
                    self.wfile.write(raw)
            except (BrokenPipeError, ConnectionError):
                pass
        def log_message(self, *args):
            pass
    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=http.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}", calls
    finally:
        http.shutdown()
        http.server_close()
        worker.join(2)


class SemanticTests(unittest.TestCase):
    def test_ollama_real_request_preserves_timed_evidence_and_explicit_suggestions(self):
        events = []
        with server() as (url, calls):
            result = semantics.analyse_transcript(transcript(), model="fixture", base_url=url, report=events.append)
        self.assertEqual(calls[0]["path"], "/api/chat")
        request = calls[0]["body"]
        self.assertFalse(request["stream"])
        self.assertEqual(request["format"]["properties"]["editingAdvice"]["type"], "string")
        self.assertFalse(request["format"]["additionalProperties"])
        self.assertEqual(request["options"]["num_ctx"],8192)
        self.assertEqual(json.loads(request["messages"][1]["content"])["transcriptSegments"], transcript()["segments"])
        block = result["contentBlocks"][0]
        self.assertEqual((block["startMs"], block["endMs"]), (0, 2400))
        self.assertEqual(block["evidenceSegments"], transcript()["segments"])
        self.assertIn("非视觉识别", block["highlights"][-1]["text"])
        self.assertTrue(result["analysis"]["transcript_only"])
        self.assertEqual(result["analysis"]["review_status"], "unreviewed")
        self.assertEqual(result["mindmap"]["nodes"][1]["data"]["targetBlockId"], block["blockId"])
        self.assertEqual(events[-1]["percent"], 100)
        from video_analysis_notes import normalize_result
        normalized = normalize_result(result)
        self.assertEqual(normalized["analysis"]["transcript_hash"],result["analysis"]["transcript_hash"])
        self.assertEqual(normalized["contentBlocks"][0]["evidenceSegments"],transcript()["segments"])

    def test_openai_compatible_request_keeps_key_out_of_result_and_events(self):
        events = []
        response = {"choices": [{"message": {"content": json.dumps(analysis())}}]}
        with server(value=response) as (url, calls):
            result = semantics.analyse_transcript(transcript(), model="fixture", provider="openai-compatible",
                base_url=url+"/v1", api_key="test-key-not-saved", report=events.append)
        self.assertEqual(calls[0]["path"], "/v1/chat/completions")
        self.assertEqual(calls[0]["authorization"], "Bearer test-key-not-saved")
        self.assertNotIn("test-key-not-saved", json.dumps([result, events]))

    def test_empty_model_and_invalid_transcripts_never_make_requests(self):
        with server() as (url, calls):
            with self.assertRaises(semantics.ModelSetupRequired):
                semantics.analyse_transcript(transcript(), model="", base_url=url)
            for value in (None, {"version":True,"unit":"ms","segments":[]},
                          {"version":1,"unit":"ms","segments":[{"startMs":False,"endMs":10,"text":"x"}]}):
                with self.assertRaises(ValueError):
                    semantics.analyse_transcript(value,model="fixture",base_url=url)
            self.assertEqual(calls, [])

    def test_chunk_boundaries_come_from_actual_cues(self):
        source = transcript()
        source["segments"].append({"startMs":180_001,"endMs":181_000,"text":"下一段剧情"})
        with server() as (url, calls):
            result = semantics.analyse_transcript(source,model="fixture",base_url=url)
        self.assertEqual(len(calls),2)
        self.assertEqual([(b["startMs"],b["endMs"]) for b in result["contentBlocks"]],[(0,2400),(180001,181000)])
        with patch.object(semantics,"MAX_CHUNKS",1):
            with self.assertRaisesRegex(ValueError,"120"):
                semantics.transcript_chunks(source)

    def test_malformed_or_invented_time_analysis_is_rejected(self):
        values = [b"not-json", {"message":{"content":"not-json"}},
                  {"message":{"content":json.dumps({**analysis(),"startMs":999999})}},
                  {"message":{"content":json.dumps({**analysis(),"beats":"not a list"})}}]
        for value in values:
            with self.subTest(value=value), server(value=value) as (url,_):
                with self.assertRaises(ValueError):
                    semantics.analyse_transcript(transcript(),model="fixture",base_url=url)

    def test_redirect_response_size_and_access_errors_are_not_followed(self):
        for status, headers, error in ((302,{"Location":"http://example.invalid/secret"},ValueError),
                                      (401,{},semantics.ModelSetupRequired),
                                      (404,{},semantics.ModelSetupRequired)):
            with server(status=status,headers=headers) as (url,calls):
                with self.assertRaises(error):
                    semantics.analyse_transcript(transcript(),model="fixture",base_url=url)
                self.assertEqual(len(calls),1)
        with server(value=b"a"*1000) as (url,_), patch.object(semantics,"MAX_RESPONSE",64):
            with self.assertRaisesRegex(ValueError,"2 MiB"):
                semantics.analyse_transcript(transcript(),model="fixture",base_url=url)

    def test_waiting_for_model_is_cancellable_and_timeout_is_finite(self):
        stop = threading.Event()
        with server(delay=1) as (url,_):
            timer = threading.Timer(.1,stop.set)
            timer.start()
            started = time.monotonic()
            with self.assertRaises(semantics.AnalysisCancelled):
                semantics.analyse_transcript(transcript(),model="fixture",base_url=url,stop=stop)
            timer.join()
            self.assertLess(time.monotonic()-started,.8)
        with server(delay=.5) as (url,_):
            with self.assertRaises(TimeoutError):
                semantics.analyse_transcript(transcript(),model="fixture",base_url=url,timeout=.1)

    def test_model_listing_and_url_validation(self):
        with server(value={"models":[{"name":"qwen:small"},{"name":"qwen:small"}]}) as (url,calls):
            self.assertEqual(semantics.list_models(base_url=url),["qwen:small"])
            self.assertEqual(calls[0]["path"],"/api/tags")
        with server(value={"data":[{"id":"local-model"}]}) as (url,_):
            self.assertEqual(semantics.list_models(base_url=url+"/v1",provider="openai-compatible"),["local-model"])
        for url in ("http://user:password@localhost", "http://remote.example", "https://x/?token=key", "file:///tmp", "http://localhost/%2e%2e"):
            with self.assertRaises(ValueError):
                semantics.validate_base_url(url)

    def test_headers_and_partial_body_can_pause_longer_than_one_second(self):
        for split in (False, True):
            with self.subTest(split=split), server(body_delay=1.2,split_body=split) as (url,_):
                value = semantics.analyse_transcript(transcript(),model="fixture",base_url=url,timeout=4)
                self.assertEqual(len(value["contentBlocks"]),1)

    def test_stalled_body_is_cancellable_and_keeps_total_timeout(self):
        for split in (False, True):
            with self.subTest(split=split), server(body_delay=1.5,split_body=split) as (url,_):
                stop = threading.Event()
                timer = threading.Timer(.2,stop.set)
                timer.start()
                started = time.monotonic()
                with self.assertRaises(semantics.AnalysisCancelled):
                    semantics.analyse_transcript(transcript(),model="fixture",base_url=url,stop=stop,timeout=5)
                timer.join()
                self.assertLess(time.monotonic()-started,.8)
        with server(body_delay=1.5,split_body=True) as (url,_):
            with self.assertRaises(TimeoutError):
                semantics.analyse_transcript(transcript(),model="fixture",base_url=url,timeout=.3)

    def test_application_control_exception_is_not_rewritten_as_network_failure(self):
        class PausedByApplication(RuntimeError):
            pass
        cancelled = threading.Event()
        def check():
            if cancelled.is_set():
                raise PausedByApplication("application paused")
            return False
        with server(body_delay=1) as (url,calls):
            timer = threading.Timer(.2,cancelled.set)
            timer.start()
            with self.assertRaises(PausedByApplication):
                semantics.analyse_transcript(transcript(),model="fixture",base_url=url,stop=check)
            timer.join()
            self.assertEqual(len(calls),1)


@unittest.skipUnless(shutil.which("ffmpeg"),"FFmpeg supplies real video fixtures")
class KeyframeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder/"clip.mkv"
        subprocess.run(["ffmpeg","-v","error","-f","lavfi","-i","testsrc2=size=96x64:rate=5:duration=4",
                        "-c:v","ffv1",str(self.source)],capture_output=True,check=True)
        self.blocks = [{"blockId":"a","startMs":0,"endMs":2000},{"blockId":"b","startMs":2000,"endMs":4000}]

    def test_frames_are_real_bounded_midpoints_with_portable_inputs(self):
        from PIL import Image
        frames = semantics.extract_keyframes(self.source,self.blocks,self.folder/"frames")
        self.assertEqual([row["blockId"] for row in frames],["a","b"])
        self.assertEqual([row["timeMs"] for row in frames],[1000,3000])
        with Image.open(frames[0]["path"]) as image:
            self.assertEqual(image.size,(96,64))
        self.assertEqual(len(semantics.extract_keyframes(self.source,self.blocks,self.folder/"small",max_frames=1)),1)

    def test_out_of_range_and_cancelled_extraction_do_not_publish_partial_frames(self):
        stop = threading.Event()
        def report(event):
            if event.get("frame_index")==1:
                stop.set()
        target = self.folder/"frames"
        with self.assertRaises(semantics.AnalysisCancelled):
            semantics.extract_keyframes(self.source,self.blocks,target,report=report,stop=stop)
        self.assertEqual(list(target.glob("*.jpg")),[])
        with self.assertRaisesRegex(ValueError,"时长"):
            semantics.extract_keyframes(self.source,[{"blockId":"bad","startMs":5000,"endMs":6000}],target)


if __name__ == "__main__":
    unittest.main()
