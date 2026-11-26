import json
from pathlib import Path
import tempfile
import unittest

from movie_usage import learning_history


class MovieUsageTests(unittest.TestCase):
    def test_completed_failed_and_active_runs_are_distinguished(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/"logs").mkdir()
            for name,status,ids in (("01","completed",[1,1]),("02","failed",[1,2]),("03","running",[2])):
                folder = root/"runs"/name
                folder.mkdir(parents=True)
                (root/"logs"/(name+"-train.json")).write_text(json.dumps({"kind":"movie-temporal","movie_ids":ids,"status":status}))
                if status=="completed":
                    (folder/"completed.json").write_text('{"kind":"movie-temporal"}')
            result = learning_history(root,active="03")
            self.assertEqual(result[1]["completed"],1)
            self.assertEqual(result[1]["attempts"],2)
            self.assertEqual(result[1]["last_state"],"failed")
            self.assertEqual(result[2]["last_state"],"running")
            self.assertEqual(learning_history(root)[2]["last_state"],"unfinished")

    def test_legacy_sources_are_read_but_detection_runs_do_not_count(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name,kind in (("old","movie-temporal"),("yolo","annotations")):
                folder = root/"runs"/name
                folder.mkdir(parents=True)
                (folder/"completed.json").write_text(json.dumps({"kind":kind}))
                (folder/"movie_sources.json").write_text('{"movies":[{"id":1}]}')
            invalid = root/"runs"/"invalid"
            invalid.mkdir()
            (invalid/"completed.json").write_text("[]")
            self.assertEqual(learning_history(root)[1]["completed"],1)


if __name__=="__main__":
    unittest.main()
