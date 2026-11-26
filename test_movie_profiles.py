"""Reviewed metadata never silently becomes training context."""
from pathlib import Path
import tempfile
import unittest

import dataset
import movie_profiles as profiles


class MovieProfilesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="yingge-profiles-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        dataset.initialize(self.root)
        with dataset.connect(self.root) as db:
            db.executemany("INSERT INTO movies(id,digest,name,mode) VALUES(?,?,?,'train')",
                           [(1,"one","测试影片"),(2,"two","另一部影片")])

    def complete_profile(self):
        return {"title":"测试影片", "year":"1995", "cast":[{"role":"至尊宝","actor":"周星驰","aliases":["阿宝"]}],
                "synopsis":"人物经历相遇与误会。", "events":[{"start_ms":0,"end_ms":10000,"text":"人物相遇","roles":["至尊宝"]}],
                "sources":[{"url":"https://example.org/film","title":"资料","license":"用户提供"}]}

    def test_legacy_project_reads_unconfirmed_without_mutating_schema(self):
        self.assertFalse(profiles.is_ready(profiles.get(self.root,1)))
        self.assertEqual(profiles.required_missing(self.root,None),[1,2])
        with dataset.connect(self.root) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='movie_profiles'").fetchone())

    def test_confirmed_profile_syncs_roles_not_actors_and_preserves_existing_cast(self):
        dataset.save_cast(self.root,1,"原有角色")
        draft = profiles.save(self.root,1,self.complete_profile())
        self.assertFalse(profiles.is_ready(draft))
        self.assertEqual(dataset.cast_names(self.root,1),["原有角色"])
        confirmed = profiles.save(self.root,1,draft,confirm=True)
        self.assertTrue(profiles.is_ready(confirmed))
        self.assertEqual(dataset.cast_names(self.root,1),["原有角色","至尊宝"])
        self.assertNotIn("周星驰",dataset.cast_names(self.root,1))
        self.assertEqual(profiles.required_missing(self.root,[1,2]),[2])
        with dataset.connect(self.root) as db:
            self.assertEqual(profiles.snapshot_from_db(db,1),confirmed)

    def test_missing_material_requires_explicit_acknowledgement(self):
        with self.assertRaisesRegex(ValueError,"尚缺"):
            profiles.save(self.root,1,{"title":"测试影片"},confirm=True)
        result = profiles.save(self.root,1,{"title":"测试影片"},confirm=True,allow_missing=True)
        self.assertTrue(result["allow_missing"])
        self.assertEqual(result["cast"],[])
        self.assertTrue(profiles.is_ready(result))

    def test_edit_returns_profile_to_draft_and_research_does_not_clobber_fields(self):
        result = profiles.save(self.root,1,self.complete_profile(),confirm=True)
        result = profiles.record_research(self.root,1,{"status":"failed","query":"测试影片","message":"暂不可用"})
        self.assertEqual(result["synopsis"],self.complete_profile()["synopsis"])
        self.assertTrue(profiles.is_ready(result))
        result["synopsis"]="人工纠错后剧情"
        updated = profiles.save(self.root,1,result)
        self.assertFalse(profiles.is_ready(updated))
        self.assertEqual(updated["synopsis"],"人工纠错后剧情")

    def test_parsers_distinguish_actor_only_and_validate_timing(self):
        cast = profiles.parse_cast("至尊宝 | 周星驰 | 阿宝,悟空\n | 吴孟达\n旁白")
        self.assertEqual(cast[1],{"role":"","actor":"吴孟达","aliases":[]})
        self.assertEqual(profiles.parse_cast(profiles.format_cast(cast)),cast)
        events = profiles.parse_events("00:01:20-00:02:00 | 相遇 | 至尊宝,旁白")
        self.assertEqual(events[0]["start_ms"],80000)
        self.assertEqual(profiles.parse_events(profiles.format_events(events)),events)
        bad = {**self.complete_profile(),"events":[{"start_ms":10,"end_ms":0,"text":"错误"}]}
        with self.assertRaisesRegex(ValueError,"时间段"):
            profiles.save(self.root,1,bad)

    def test_source_and_alias_validation(self):
        for source in ("file:///C:/secret", "javascript:alert(1)","https://name:secret@example.org/"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                profiles.save(self.root,1,{**self.complete_profile(),"sources":[source]})
        with self.assertRaisesRegex(ValueError,"先填写角色名"):
            profiles.save(self.root,1,{**self.complete_profile(),"cast":[{"actor":"演员","aliases":["别名"]}]})

    def test_search_returns_candidates_without_auto_selecting_or_writing(self):
        calls=[]
        def api(url,params):
            calls.append(params)
            return {"search":[{"id":"Q2","label":"同名片","description":"2000 film"},
                               {"id":"Q1","label":"同名片","description":"1995 film"}]}
        result=profiles.search_online("同名片","1995",api=api)
        self.assertEqual(result["status"],"found")
        self.assertEqual([row["id"] for row in result["candidates"]],["Q1","Q2"])
        self.assertEqual(len(calls),1)
        self.assertFalse(profiles.is_ready(profiles.get(self.root,1)))

    def test_network_failure_and_not_found_are_explicit_manual_fallbacks(self):
        def offline(*args):
            raise TimeoutError("timeout")
        result=profiles.search_online("测试影片",api=offline)
        self.assertEqual(result["status"],"failed")
        self.assertEqual(result["candidates"],[])
        self.assertIn("手动填写",result["message"])
        self.assertEqual(profiles.record_research(self.root,1,result)["status"],"manual-needed")
        self.assertEqual(profiles.search_online("没有此片",api=lambda *args:{"search":[]})["status"],"not-found")

    def test_fetch_extracts_roles_and_attributed_plot_and_stays_draft(self):
        def item(value):
            return {"datavalue":{"value":value}}
        def api(url,params):
            if params.get("ids")=="Q1":
                return {"entities":{"Q1":{"labels":{"zh":{"value":"测试影片"}},
                         "claims":{"P577":[{"mainsnak":item({"time":"+1995-01-01T00:00:00Z"})}],
                                   "P161":[{"mainsnak":item({"id":"Q2"}),"qualifiers":{"P453":[item({"id":"Q3"})]}},
                                             {"mainsnak":item({"id":"Q4"}),"qualifiers":{"P4633":[item("角色乙")]}}]},
                         "sitelinks":{"zhwiki":{"title":"测试影片"}}}}}
            if params.get("action")=="wbgetentities":
                return {"entities":{"Q2":{"labels":{"zh":{"value":"演员甲"}}},
                                     "Q3":{"labels":{"zh":{"value":"角色甲"}}},
                                     "Q4":{"labels":{"zh":{"value":"演员乙"}}}}}
            return {"query":{"pages":[{"extract":"影片介绍\n== 剧情 ==\n相遇与争论。\n== 演员 ==\n演员名单"}]}}
        result=profiles.fetch_candidate({"id":"Q1","title":"测试影片"},api=api)
        self.assertEqual(result["year"],"1995")
        self.assertEqual(result["synopsis"],"相遇与争论。")
        self.assertEqual(result["cast"],[{"role":"角色甲","actor":"演员甲","aliases":[]},
                                         {"role":"角色乙","actor":"演员乙","aliases":[]}])
        self.assertEqual(len(result["sources"]),2)
        self.assertFalse(profiles.is_ready(result))

    def test_fetch_does_not_invent_plot_or_use_entity_ids_as_names(self):
        def api(url,params):
            return {"entities":{"Q1":{"labels":{"en":{"value":"Unknown film"}},
                    "claims":{"P161":[{"mainsnak":{"datavalue":{"value":{"id":"Q2"}}}}]}}}}
        result=profiles.fetch_candidate({"id":"Q1"},api=api)
        self.assertEqual(result["cast"],[])
        self.assertEqual(result["synopsis"],"")
        self.assertIn("手动补充",result["research"]["message"])


if __name__=="__main__":
    unittest.main()
