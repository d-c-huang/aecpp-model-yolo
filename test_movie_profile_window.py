"""Tk integration of metadata review, staged training and network fallback."""
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

import dataset
import movie_profiles as profiles
from movie_profile_window import MovieProfileWindow
import theme


class MovieProfileWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="profile-window-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        dataset.initialize(self.root)
        with dataset.connect(self.root) as db:
            db.executemany("INSERT INTO movies(id,digest,name,mode) VALUES(?,?,?,'train')",[(1,"one","电影甲"),(2,"two","电影乙")])
        for movie_id in (1,2):
            profiles.record_research(self.root,movie_id,{"query":profiles.get(self.root,movie_id)["title"],
                                     "status":"selected","engine":"Bing","message":"请核对"})
        try:
            self.tk = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.tk.withdraw()
        theme.apply_theme(self.tk)
        self.addCleanup(self.tk.destroy)
        self.ready,self.closed = Mock(),Mock()
        self.window = MovieProfileWindow(self.tk,self.root,[1,2],on_ready=self.ready,on_close=self.closed)
        self.addCleanup(self.window.close)
        self.window.win.withdraw()

    def fill(self, title="电影甲"):
        profile = profiles.get(self.root,self.window._current_id)
        profile.update(title=title,cast=[{"role":"角色甲","actor":"演员甲","aliases":[]}],synopsis="故事情节")
        self.window._set_fields(profile)
        self.window._dirty = True

    def test_confirmed_all_films_resume_once_and_sync_roles(self):
        self.fill()
        self.assertTrue(self.window._save(True))
        self.assertEqual(self.window._current_id,2)
        self.ready.assert_not_called()
        self.fill("电影乙")
        self.assertTrue(self.window._save(True))
        self.ready.assert_called_once_with()
        self.window._save(True)
        self.ready.assert_called_once_with()
        self.assertEqual(dataset.cast_names(self.root,1),["角色甲"])

    def test_empty_profile_needs_explicit_missing_acknowledgement(self):
        with patch("movie_profile_window.messagebox.showerror") as error:
            self.assertFalse(self.window._save(True))
        error.assert_called_once()
        self.assertEqual(self.window._current_id,1)
        self.window.allow_missing.set(True)
        self.assertTrue(self.window._save(True))
        self.assertTrue(profiles.get(self.root,1)["allow_missing"])

    def test_switch_saves_edits_as_draft_without_publishing(self):
        self.fill()
        self.window._load(2)
        self.assertEqual(profiles.get(self.root,1)["synopsis"],"故事情节")
        self.assertFalse(profiles.is_ready(profiles.get(self.root,1)))
        self.assertEqual(dataset.cast_names(self.root,1),[])

    def test_stale_network_result_does_not_replace_other_film(self):
        token = self.window._search_token
        self.window._load(2)
        self.window._events.put((token,1,"search",{"query":"甲","status":"found","message":"旧结果","candidates":[]},None))
        self.window._receive()
        self.assertEqual(self.window.title.get(),"电影乙")
        self.assertNotEqual(self.window.status.get(),"旧结果")

    def test_failed_search_remains_editable_and_records_manual_fallback(self):
        with patch("movie_profiles.search_online",return_value={"query":"电影甲","status":"failed","message":"网络不可用，请手填","candidates":[]}):
            self.window._search()
            deadline=time.monotonic()+2
            while self.window._events.empty() and time.monotonic()<deadline:
                time.sleep(.01)
            self.window._receive()
        self.assertEqual(profiles.get(self.root,1)["status"],"manual-needed")
        self.assertFalse(self.window._searching)
        self.fill()
        self.assertTrue(self.window._save(False))

    def test_fetch_preserves_time_alignment_and_retains_polling_on_invalid_edit(self):
        self.window.events.insert("1.0","bad timing")
        self.window._dirty=True
        result=profiles.get(self.root,1)
        result["research"]={"message":"候选"}
        self.window._events.put((self.window._search_token,1,"fetch",result,None))
        if self.window._timer:
            self.window.win.after_cancel(self.window._timer)
        self.window._poll()
        self.assertIsNotNone(self.window._timer)
        self.assertIn("资料未应用",self.window.status.get())
        self.window.events.delete("1.0","end")
        self.window._dirty=False

    def test_close_cancels_timer_and_never_starts_training(self):
        self.assertTrue(self.window.close())
        self.assertTrue(self.window._closed)
        self.assertIsNone(self.window._timer)
        self.closed.assert_called_once_with()
        self.ready.assert_not_called()

    def test_matching_candidate_fills_and_saves_draft_around_manual_edits(self):
        window = self.window
        self.fill()
        self.assertTrue(window._save(False))
        candidate = {"id":"tmdb:12", "title":"电影甲", "description":"1995", "year":"1995"}
        result = {"query":"电影甲", "status":"found", "candidates":[candidate], "match":candidate, "message":"已找到"}
        details = {"year":"1995", "cast":[], "synopsis":"网上剧情", "sources":[{"url":"https://example.org/12"}],
                   "research":{"status":"selected"}}
        with patch("movie_profiles.fetch_candidate", return_value=details) as fetch:
            window._events.put((window._search_token, 1, "search", result, None))
            window._receive()
            deadline = time.monotonic()+2
            while window._searching and time.monotonic()<deadline:
                window._receive()
                time.sleep(.01)
        fetch.assert_called_once_with(candidate)
        draft = profiles.get(self.root, 1)
        self.assertEqual((draft["year"], draft["status"], draft["synopsis"]), ("1995","draft","故事情节"))
        self.assertEqual(draft["sources"][0]["url"], "https://example.org/12")
        self.assertEqual(draft["cast"][0]["role"], "角色甲")
        self.ready.assert_not_called()

    def test_changing_query_discards_pending_autofill(self):
        window = self.window
        token = window._search_token
        window._searching = True
        window.year.set("2001")
        window._events.put((token, 1, "autofill", {"year":"1995"}, None))
        window._receive()
        self.assertEqual(window.year.get(), "2001")
        self.assertEqual(profiles.get(self.root, 1)["year"], "")

    def test_reopen_retries_previous_miss_once_per_window(self):
        profiles.record_research(self.root, 2, {"query":"电影乙","status":"not-found","engine":"百度"})
        with patch.object(self.window, "_search") as search:
            self.window._load(2)
            self.window._load(1)
            self.window._load(2)
        search.assert_called_once()

    def test_small_window_keeps_review_actions_visible(self):
        self.window.win.deiconify()
        self.window.win.geometry("920x640+20+20")
        self.tk.update()
        button=self.window.confirm_button
        self.assertGreater(button.winfo_width(),50)
        self.assertLessEqual(button.winfo_rooty()+button.winfo_height(),self.window.win.winfo_rooty()+self.window.win.winfo_height())
        self.assertGreater(self.window.cast.winfo_height(),30)


if __name__=="__main__":
    unittest.main()
