"""Reviewed metadata never silently becomes training context."""
from pathlib import Path
import tempfile
import unittest

import dataset
import film_web
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
        result=profiles.search_online("同名片","1995",api=api,web_search=lambda *args: ([], []))
        self.assertEqual(result["status"],"found")
        self.assertEqual([row["id"] for row in result["candidates"]],["Q1","Q2"])
        self.assertEqual(len(calls),1)
        self.assertFalse(profiles.is_ready(profiles.get(self.root,1)))

    def test_network_failure_and_not_found_are_explicit_manual_fallbacks(self):
        def offline(*args):
            raise TimeoutError("timeout")
        no_web = lambda *args: ([], [])
        result=profiles.search_online("测试影片",api=offline,web_search=no_web)
        self.assertEqual(result["status"],"failed")
        self.assertEqual(result["candidates"],[])
        self.assertIn("手动填写",result["message"])
        self.assertEqual(profiles.record_research(self.root,1,result)["status"],"manual-needed")
        self.assertEqual(profiles.search_online("没有此片",api=lambda *args:{"search":[]},web_search=no_web)["status"],"not-found")

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

    def test_engines_discover_film_pages_and_catalog_disambiguates_by_year(self):
        link = "https://www.themoviedb.org/movie/12-film"
        results = {"Google": f'<a href="/url?q={link}"><h3>测试影片 (1995)</h3></a>',
                   "百度": f'<div mu="{link}"><a href="/link?url=opaque">测试影片 (1995)</a></div>',
                   "Bing": f'<rss><channel><item><title>测试影片 (1995)</title><link>{link}</link></item></channel></rss>'}
        catalog = '<div id="movie_results">' + "".join(
            f'<a href="/movie/{i}"><h2>测试影片</h2></a><span class="release_date">{y}年</span>'
            for i, y in [(12, 1995), (34, 2005)]) + '</div>'
        for engine, body in results.items():
            with self.subTest(engine=engine):
                self.assertEqual([r["id"] for r in film_web._web_results(body, engine)], ["tmdb:12"])
                def read(url):
                    return catalog if url.startswith(film_web.TMDB) else body
                rows, errors = film_web.search_candidates("测试影片", "1995", engine, read=read)
                self.assertFalse(errors)
                self.assertEqual(len(rows), 2)
                self.assertIsNone(film_web.unique_match("测试影片", "", rows))
                self.assertIsNone(film_web.unique_match("测试影片", "1996", rows))
                self.assertEqual(film_web.unique_match("测试影片", "1995", rows)["id"], "tmdb:12")
        self.assertEqual(film_web.identity("测试影片.1995.1080p.mkv"), ("测试影片", "1995"))
        self.assertEqual(film_web.identity("Blade Runner 2049.mkv"), ("Blade Runner 2049", ""))

    def test_tmdb_credits_use_cast_cards_and_validate_source_identity(self):
        page = ('<script type="application/ld+json">{"@type":"Movie","name":"测试影片",'
                '"url":"https://www.themoviedb.org/movie/12","datePublished":"1995-01-01",'
                '"description":"测试剧情","actor":[{"name":"演员甲"},{"name":"演员乙"}]}</script>'
                '<ol class="people scroller"><li class="card"><p><a href="/person/1">演员甲</a></p>'
                '<p class="character">角色甲</p></li></ol>')
        profile = film_web.fetch("12", read=lambda _: page)
        self.assertEqual([(r["role"], r["actor"]) for r in profile["cast"]], [("角色甲","演员甲"), ("","演员乙")])
        self.assertEqual(profile["synopsis"], "测试剧情")
        with self.assertRaises(ValueError):
            film_web.fetch("123", read=lambda _: page)

    def test_uploaded_titles_and_localized_catalog_names(self):
        for raw, expected in [
            ("（鹿鼎记）加长完整版/蓝光修复版", ("鹿鼎记", "")),
            ("【逃学威龙2】 4K60FPS 国语 双语字幕 周星驰", ("逃学威龙2", "")),
            ("周星驰《逃学威龙》(1991) 国语中字", ("逃学威龙", "1991")),
            ("[4K超清]国产凌凌漆（国语）", ("国产凌凌漆", "")),
            ("唐伯虎点秋香-超高清", ("唐伯虎点秋香", "")),
            ("月光宝盒.BD1280超清国粤双语中英双字.mp4", ("月光宝盒", "")),
        ]:
            with self.subTest(title=raw):
                self.assertEqual(film_web.identity(raw), expected)
        catalog = ('<div id="movie_results"><a href="/movie/12"><h2><span>逃学威龙</span>'
                   '<span> (逃學威龍)</span></h2></a><span class="release_date">1991</span></div>')
        rows, _ = film_web.search_candidates("逃学威龙", "", "Bing",
                        read=lambda url: catalog if url.startswith(film_web.TMDB) else "<rss/>")
        for name in ("逃学威龙", "逃學威龍"):
            self.assertEqual(film_web.unique_match(name, "", rows)["id"], "tmdb:12")

    def test_attached_year_respects_literal_movie_titles(self):
        def search(query, year, engine):
            films = {"家有喜事": "1992", "神奇女侠1984": "2020"}
            rows = [film_web._candidate("12", query, films[query], "TMDB", True)] if query in films else []
            return rows, []
        result = profiles.search_online("【家有喜事1992】4K超清未删减", web_search=search)
        self.assertEqual(result["match"]["title"], "家有喜事")
        self.assertEqual(profiles.search_online("神奇女侠1984", web_search=search)["match"]["year"], "2020")


if __name__=="__main__":
    unittest.main()
