import json
from pathlib import Path
import tempfile
import time
import unittest

from learning_totals import summarize_learning


def outcome(movie=1, *, status="completed", examples=3, clips=2, frames=8, **extra):
    return {"id": f"learn-{movie}", "movie_id": movie, "status": status, "example_count": examples,
            "film_hash": f"film-{movie}", "updated": "2026-11-27T10:00:00+00:00",
            "model_analysis": {"visual_status": "completed", "visual_clip_count": clips,
                               "visual_frame_count": frames}, **extra}


def movie(movie_id=1, **extra):
    return {"id": movie_id, "digest": f"film-{movie_id}", "mode": "time", **extra}


class LearningTotalsTests(unittest.TestCase):
    def test_complete_partial_visual_only_and_no_output_states(self):
        rows = [outcome(), outcome(2, status="partial", examples=4, clips=0, frames=0),
                outcome(3, examples=0, clips=1, frames=4), outcome(4, examples=0, clips=0),
                outcome(5, status="needs_input"), outcome(6, status="failed"), outcome(7, status="queued")]
        totals = summarize_learning([], rows, movies=[movie(i) for i in range(1, 8)])
        self.assertEqual(totals["video_count"], 3)
        self.assertEqual(totals["completed_count"], 2)
        self.assertEqual(totals["partial_count"], 1)
        self.assertEqual(totals["subtitle_reference_count"], 7)
        self.assertEqual(totals["visual_clip_count"], 3)
        self.assertEqual(totals["visual_frame_count"], 12)

    def test_pending_failed_retry_keeps_only_prior_published_outcome(self):
        published = outcome(); published.update(published_at=published.pop("updated"))
        for state in ("queued", "running", "paused", "failed"):
            row = outcome(status=state, examples=999, published_result=published)
            totals = summarize_learning([], [row], movies=[movie()])
            self.assertEqual(totals["video_count"], 1)
            self.assertEqual(totals["subtitle_reference_count"], 3)
        row["published_result"] = {"status": "needs_input", "example_count": 0}
        self.assertEqual(summarize_learning([], [row], movies=[movie()])["video_count"], 0)

    def test_same_source_different_quality_and_retries_count_latest_once(self):
        url = "https://www.iqiyi.com/v_sample.html"
        online = [{"id": "download-a", "url": url, "result": {"movie_id": 1}},
                  {"id": "download-b", "url": "http://m.iqiyi.com/v_sample.html?from=search", "result": {"movie_id": 2}}]
        first = outcome()
        newer = outcome(2, status="partial", examples=5, clips=1, frames=3, updated="2026-11-27T11:00:00+00:00")
        total = summarize_learning(online, [first, newer, newer], movies=[movie(), movie(2)])
        self.assertEqual(total["video_count"], 1)
        self.assertEqual(total["partial_count"], 1)
        self.assertEqual(total["subtitle_reference_count"], 5)
        self.assertEqual(total["visual_frame_count"], 3)

    def test_same_digest_is_deduplicated_without_online_jobs(self):
        newer = outcome(2, film_hash="film-1", examples=4, updated="2026-11-27T12:00:00Z")
        result = summarize_learning([], [outcome(), newer], movies=[movie(), movie(2, digest="film-1")])
        self.assertEqual((result["video_count"], result["subtitle_reference_count"]), (1, 4))

    def test_removed_or_changed_sources_excluded_and_old_summaries_are_explicit(self):
        rows = [outcome(), outcome(2), outcome(3)]
        rows[2].pop("film_hash")
        result = summarize_learning([], rows, movies=[movie(2, digest="changed"), movie(3)])
        self.assertEqual(result["video_count"], 1)
        self.assertEqual(result["stale_count"], 2)
        self.assertEqual(result["source_validation"], "existence-only")
        self.assertIn("未核验源指纹", result["message"])
        result = summarize_learning([], [outcome()])
        self.assertEqual(result["source_validation"], "unchecked")
        self.assertEqual(result["video_count"], 1)

    def test_validation_movies_count_as_analysed_but_never_generation_references(self):
        result = summarize_learning([], [outcome()], movies=[movie(mode="val")])
        self.assertEqual((result["video_count"], result["evaluation_count"]), (1, 1))
        self.assertEqual(result["subtitle_reference_count"], 0)
        self.assertEqual(result["visual_clip_count"], 0)
        self.assertEqual(result["visual_frame_count"], 0)
        self.assertIn("不计生成参考", result["message"])

    def test_false_counts_and_failed_visual_stage_do_not_inflate_results(self):
        rows = [outcome(examples=True, clips=-2), outcome(2, examples="bad", clips=0),
                outcome(3, model_analysis={"visual_status": "failed", "visual_clip_count": 99, "visual_frame_count": 999})]
        result = summarize_learning([], rows, movies=[movie(i) for i in (1, 2, 3)])
        self.assertEqual(result["video_count"], 1)
        self.assertEqual(result["subtitle_reference_count"], 3)
        self.assertEqual(result["visual_clip_count"], 0)

    def test_online_download_complete_or_candidate_complete_is_not_learning(self):
        result = summarize_learning([{"id": "a", "status": "completed", "result": {"movie_id": 1}}], [],
                                    [{"id": "c", "state": "completed", "movie_id": 1}], movies=[movie()])
        self.assertEqual(result["video_count"], 0)

    def test_skip_views_deduplicate_and_legacy_failure_requires_classification(self):
        url = "https://www.iqiyi.com/v_restricted.html"
        online = [{"id": "a", "url": url, "status": "skipped", "message": "站点限制",
                   "updated": "2026-11-27T10:00:00Z", "discovery": {"source_url": "https://search.video.iqiyi.com/o?key=test"}},
                  {"id": "old", "url": "https://v.youku.com/v_show/id_old.html", "status": "failed", "error": "6004"}]
        candidates = [{"id": "c", "online_job_id": "a", "url": url, "state": "skipped", "detail": "需要会员",
                       "updated": "2026-11-27T11:00:00Z", "title": "受限视频"}]
        result = summarize_learning(online, [], candidates, recent_limit=1)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["latest_skip"]["reason"], "需要会员")
        self.assertEqual(result["latest_skip"]["site"], "iqiyi")
        self.assertEqual(result["latest_skip"]["source_url"], "https://search.video.iqiyi.com/o?key=test")
        legacy = {**online[1], "reason": "优酷区域限制 6004", "timestamp": "2026-11-27T12:00:00Z"}
        result = summarize_learning(online, [], candidates, skipped=[legacy], recent_limit=1)
        self.assertEqual(result["skipped_count"], 2)
        self.assertEqual(len(result["recent_skips"]), 1)
        self.assertEqual(result["latest_skip"]["reason"], "优酷区域限制 6004")
        self.assertEqual(result["video_count"], 0)

    def test_function_is_pure_json_serializable_and_restart_independent(self):
        rows = [outcome()]
        before = json.dumps(rows)
        first = summarize_learning([], rows, movies=[movie()])
        second = summarize_learning([], json.loads(before), movies=[movie()])
        self.assertEqual(first, second)
        self.assertEqual(json.dumps(rows), before)
        json.dumps(first, allow_nan=False)

    def test_manual_retry_retains_past_skip_and_readable_discovery_title(self):
        url = "https://v.youku.com/v_show/id_XN12345==.html"
        row = {"id":"download", "url":url, "status":"completed", "title":"id_XN12345==.html",
               "discovery":{"title":"电影预告片", "source_url":"https://www.youku.com/ku/webhome"},
               "last_skip":{"url":url, "reason":"优酷限制播放 -6004", "timestamp":"2026-11-27T10:00:00Z"}}
        total = summarize_learning([row], [])
        self.assertEqual(total["skipped_count"], 1)
        self.assertEqual(total["latest_skip"]["title"], "电影预告片")
        self.assertEqual(total["latest_skip"]["source_url"], row["discovery"]["source_url"])
        self.assertEqual(total["video_count"], 0)
        # Also deduplicate the still-visible skipped candidate from that task.
        self.assertEqual(summarize_learning([row],[],[
            {"url":url,"state":"skipped","online_job_id":"download","detail":"优酷限制播放 -6004"}
        ])["skipped_count"], 1)


class PublishedResultPersistenceTests(unittest.TestCase):
    def test_real_queue_publish_reopen_failed_reanalysis_keeps_previous_totals(self):
        self._check_reanalysis_persistence()

    def test_legacy_partial_is_preserved_before_retry_and_remains_unverified(self):
        self._check_reanalysis_persistence(legacy=True)

    def _check_reanalysis_persistence(self, *, legacy=False):
        import auto_learning
        import dataset

        def wait(manager):
            deadline = time.monotonic() + 5
            while manager.busy and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertFalse(manager.busy)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            dataset.initialize(root)
            source = Path(temporary) / "source.dat"; source.write_bytes(b"a real local source")
            dataset.register_movie(root, source, "time", lambda message: None)
            current = dataset.movies(root)[0]; movie_id = current["id"]

            def analyse(*args):
                return {"movie_id": movie_id, "title": "test", "film_hash": current["digest"], "mode": "time",
                        "snapshot_hash": "snapshot-test", "duration_ms": 6000, "cut_candidates_ms": [], "boards": [],
                        "timing_examples": [{"text": "真实字幕时间码", "duration": 2., "start_ms": 0,
                                             "end_ms": 2000, "source_kind": "subtitle"}]}

            manager = auto_learning.AutoLearningManager(root, analyser=analyse)
            try:
                manager.enqueue([movie_id]); wait(manager)
                published = manager.list_jobs()[0]["published_result"]
                self.assertEqual(published["example_count"], 1)
                self.assertEqual(published["film_hash"], current["digest"])
                if legacy:
                    with manager._db(write=True) as db:
                        job = manager._get(db, manager.list_jobs()[0]["id"])
                        job.pop("published_result"); job.pop("film_hash")
                        job.update(status="partial", updated="2026-11-20T10:00:00Z")
                        manager._save(db, job)
                        legacy_stamp = manager._get(db, job["id"])["updated"]
            finally:
                manager.close(); wait(manager)

            def fail(*args):
                raise ValueError("本次模型不可用")

            reopened = auto_learning.AutoLearningManager(root, analyser=fail)
            try:
                self.assertEqual(summarize_learning([], reopened.list_jobs(), movies=dataset.movies(root))["video_count"], 1)
                reopened.pause()
                reopened.enqueue([movie_id], force=True)
                self.assertEqual(reopened.list_jobs()[0]["status"], "paused")
                self.assertEqual(summarize_learning([], reopened.list_jobs(), movies=dataset.movies(root))["subtitle_reference_count"], 1)
                if legacy:
                    published = reopened.list_jobs()[0]["published_result"]
                    self.assertEqual(published["status"], "partial")
                    self.assertEqual(published["published_at"], legacy_stamp)
                    self.assertEqual(published["film_hash"], "")
                reopened.resume(); wait(reopened)
                self.assertEqual(reopened.list_jobs()[0]["status"], "failed")
                self.assertEqual(reopened.list_jobs()[0]["published_result"], published)
                totals = summarize_learning([], reopened.list_jobs(), movies=dataset.movies(root))
                self.assertEqual((totals["video_count"], totals["subtitle_reference_count"]), (1, 1))
                if legacy:
                    self.assertEqual(totals["partial_count"], 1)
                    self.assertEqual(totals["source_validation"], "existence-only")
            finally:
                reopened.close(); wait(reopened)


if __name__ == "__main__":
    unittest.main()
