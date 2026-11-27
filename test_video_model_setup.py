import json
import threading
import unittest

from test_video_semantics import server
from video_model_setup import VISION_MODEL, prepare_vision_model
from video_semantics import AnalysisCancelled


class VisionSetupTests(unittest.TestCase):
    def test_download_reports_actual_layer_bytes_then_validates_capability(self):
        events=[]
        def value(path):
            if path=="/api/show":
                return {"capabilities":["completion","vision"]}
            return b'{"status":"pulling file","total":100,"completed":42}\n{"status":"success"}\n'
        with server(value=value) as (url,calls):
            self.assertEqual(prepare_vision_model(base_url=url,report=events.append),VISION_MODEL)
        self.assertEqual(calls[0]["body"],{"model":VISION_MODEL,"stream":True})
        self.assertEqual(VISION_MODEL,"qwen2.5vl:3b")
        self.assertIn("3.2 GB",events[0]["message"])
        self.assertEqual([row["percent"] for row in events if "percent" in row],[42])
        self.assertEqual(calls[-1]["path"],"/api/show")

    def test_incomplete_download_and_wrong_capability_not_ready(self):
        for data in (b'{"status":"pulling"}\n',b'{"error":"failed"}\n',
                     b'{"status":"success"}\n'):
            with self.subTest(data=data),server(value=lambda path: {"capabilities":["completion"]} if path=="/api/show" else data) as (url,_):
                with self.assertRaises(ValueError):
                    prepare_vision_model(base_url=url)

    def test_cancel_during_silent_response(self):
        stop=threading.Event()
        with server(value=b'{"status":"success"}\n',body_delay=1.2) as (url,_):
            timer=threading.Timer(.15,stop.set)
            timer.start()
            try:
                with self.assertRaises(AnalysisCancelled):
                    prepare_vision_model(base_url=url,stop=stop)
            finally:
                timer.cancel()

    def test_setup_is_local_and_has_no_arbitrary_model_download(self):
        with self.assertRaisesRegex(ValueError,"本机"):
            prepare_vision_model(base_url="https://example.com")
        with self.assertRaises(ValueError):
            prepare_vision_model(base_url="http://user:secret@localhost")


if __name__=="__main__":
    unittest.main()
