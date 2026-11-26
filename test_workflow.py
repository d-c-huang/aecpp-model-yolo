import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image

import dataset
import dialogue
import film_learning
import model_tasks
import scripts
import subtitles


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.root = self.folder/"中文 工程"
        dataset.initialize(self.root)

    def add_review_fixture(self):
        Image.new("RGB",(40,40),"blue").save(self.root/"frames/test.jpg")
        with dataset.connect(self.root) as db:
            db.execute("INSERT INTO movies(digest,name,mode) VALUES('film','电影','train')")
            db.execute("INSERT INTO frames(movie,slot,ms,path,split) VALUES(1,0,700,'frames/test.jpg','train')")

    @unittest.skipUnless(shutil.which("ffmpeg"),"FFmpeg generates the video fixture")
    def test_import_capture_and_reopen(self):
        movie = self.folder/"原片.mkv"
        subprocess.run(["ffmpeg","-v","error","-f","lavfi","-i","color=blue:size=64x64:rate=10:duration=3",
                        "-c:v","ffv1",str(movie)],check=True)
        quiet = lambda _:None
        film, _ = dataset.import_movie(self.root,movie,"val",quiet)
        self.assertEqual(dataset.frames(self.root),[])
        record = dataset.movies(self.root)[0]
        first = dataset.generate_screenshots(self.root,record,1,quiet,at=.7)
        self.assertEqual(first[0],1)
        self.assertEqual(dataset.generate_screenshots(self.root,record,1,quiet,at=.7),(0,first[1]))
        self.assertEqual(dataset.generate_screenshots(self.root,record,1,quiet,at=1.5)[0],1)
        self.assertEqual(dataset.generate_screenshots(self.root,record,3,quiet,next_batch=True),(0,None))
        batches = [row["batch_id"] for row in dataset.frames(self.root)]
        self.assertEqual(len(set(batches)),2)
        self.assertTrue(all(batches))
        renamed = movie.with_name("换位置.mkv")
        movie.rename(renamed)
        self.assertEqual(dataset.import_movie(self.root,renamed,"train",quiet)[0],film)
        self.assertEqual({row["split"] for row in dataset.frames(self.root)},{"val"})
        self.assertEqual(dataset.movies(self.root)[0]["source_path"],str(renamed))

    @unittest.skipUnless(shutil.which("ffmpeg"),"FFmpeg generates the video fixture")
    def test_next_batch_fills_used_sampling_slots_with_unique_frames(self):
        movie = self.folder/"动态短片.mkv"
        subprocess.run(["ffmpeg","-v","error","-f","lavfi","-i","testsrc2=size=96x64:rate=10:duration=6",
                        "-c:v","ffv1",str(movie)],check=True)
        quiet = lambda _:None
        dataset.import_movie(self.root,movie,"train",quiet)
        record = dataset.movies(self.root)[0]
        count,first = dataset.generate_screenshots(self.root,record,1,quiet,start=4.1,end=4.8)
        self.assertEqual(count,1)
        boxes = [[0,.1,.1,.9,.9]]
        dataset.save_boxes(self.root,first,boxes,"reviewed")
        added,next_frame = dataset.generate_screenshots(self.root,record,3,quiet,next_batch=True,start=4.1,end=5.8)
        self.assertEqual(added,3)
        rows = dataset.frames(self.root)
        self.assertEqual(next_frame,rows[1]["id"])
        self.assertEqual(len({row["ms"] for row in rows}),4)
        self.assertEqual(len({row["content_hash"] for row in rows}),4)
        self.assertTrue(all(4100<=row["ms"]<5800 for row in rows))
        self.assertEqual((rows[0]["state"],json.loads(rows[0]["boxes"])),("reviewed",boxes))
        self.assertNotEqual(rows[0]["batch_id"],rows[1]["batch_id"])
        self.assertEqual({row["batch_id"] for row in rows[1:]},{rows[1]["batch_id"]})
        self.assertEqual({row["layer_ms"] for row in rows[1:]},{1000,100})

    def test_batch_migration_recovers_empty_machine_drafts(self):
        self.add_review_fixture()
        with dataset.connect(self.root) as db:
            db.execute("DROP INDEX frame_batch")
            db.execute("ALTER TABLE frames DROP COLUMN batch_id")
            db.execute("UPDATE frames SET state='predicted' WHERE id=1")
            db.execute("""INSERT INTO frames(movie,slot,ms,path,split,state)
                          VALUES(1,1,900,'frames/reviewed.jpg','train','reviewed')""")
        dataset.initialize(self.root)
        rows = dataset.frames(self.root)
        self.assertEqual([(r["batch_id"],r["state"]) for r in rows],[("","new"),("","reviewed")])

    def test_prediction_keeps_batch_scope_and_retries_empty_results(self):
        self.add_review_fixture()
        source = self.root/"frames/test.jpg"
        box = [0,.1,.1,.7,.8]
        with dataset.connect(self.root) as db:
            db.execute("INSERT INTO movies(digest,name,mode) VALUES('other','另一部电影','train')")
            db.execute("UPDATE frames SET batch_id='newer' WHERE id=1")
            for movie,batch,split,state in [(1,"newer","train","new"), (1,"newer","val","new"),
                                           (1,"older","train","new"), (2,"newer","train","new"),
                                           (1,"newer","train","reviewed"), (1,"newer","train","draft")]:
                slot = db.execute("SELECT MAX(id) FROM frames").fetchone()[0]
                image = self.root/f"frames/{slot}.jpg"
                shutil.copyfile(source,image)
                db.execute("""INSERT INTO frames(movie,slot,ms,path,split,state,batch_id)
                              VALUES(?,?,?,?,?,?,?)""",(movie,slot,slot*200,f"frames/{slot}.jpg",split,state,batch))
        weights = self.folder/"base.pt"
        weights.touch()
        primary = SimpleNamespace(task="detect",names={0:"人物"})
        general = SimpleNamespace(task="detect",names={0:"person"})
        ultralytics = SimpleNamespace(YOLO=lambda path:general if path==str(weights) else primary)
        with patch.dict(sys.modules,ultralytics=ultralytics), patch.object(model_tasks,"BASE_WEIGHTS",weights), \
             patch.object(model_tasks,"detect_boxes",side_effect=[[],[box],[],[]]) as detect, \
             patch.object(model_tasks,"report"):
            model_tasks.predict(self.root,str(self.folder/"project.pt"),.25,movie=1,batch_id="newer")
        self.assertEqual([call.args[0] for call in detect.call_args_list],[primary,general,primary,general])
        rows = dataset.frames(self.root)
        self.assertEqual([r["state"] for r in rows],["predicted","new","new","new","new","reviewed","draft"])
        self.assertEqual(json.loads(rows[0]["boxes"]),[box])

    def test_validation_prediction_requires_opt_in_and_single_frame(self):
        self.add_review_fixture()
        with dataset.connect(self.root) as db:
            db.execute("UPDATE frames SET batch_id='current' WHERE id=1")
            for i,state in ((2,"new"),(3,"new"),(4,"reviewed")):
                db.execute("""INSERT INTO frames(movie,slot,ms,path,split,state,batch_id)
                              VALUES(1,?,?,?,'val',?,'current')""",(i,i*1000,f"frames/{i}.jpg",state))
        self.assertEqual([r["id"] for r in dataset.prediction_frames(self.root,1,"current")],[1])
        self.assertFalse(dataset.prediction_frames(self.root,1,"current",frame_id=2))
        self.assertEqual([r["id"] for r in dataset.prediction_frames(self.root,1,"current",frame_id=2,allow_validation=True)],[2])
        self.assertFalse(dataset.prediction_frames(self.root,1,"current",frame_id=4,allow_validation=True))
        with self.assertRaisesRegex(ValueError,"指定一张"):
            dataset.prediction_frames(self.root,1,"current",allow_validation=True)

    def test_script_matching_and_export(self):
        self.add_review_fixture()
        script = self.folder/"剧本.txt"
        script.write_text("阿青：回来就好。\n阿青：好。\n小白：好。\n[00:00:02.000 - 00:00:03.000] 小白：念白。\n",encoding="utf-8")
        self.assertEqual(scripts.import_script(self.root,1,script),4)
        self.assertEqual(scripts.candidates(self.root,1,2500,[])[0]["role"],"小白")
        srt = self.folder/"独立字幕.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:01,000\n回来就好！\n\n2\n00:00:01,000 --> 00:00:02,000\n好。\n",encoding="utf-8")
        self.assertEqual(subtitles.import_subtitles(self.root,1,srt,lambda _:None),2)
        self.assertEqual(scripts.candidates(self.root,1,1200,subtitles.candidates(self.root,1,1200)),[])
        match, = scripts.candidates(self.root,1,700,subtitles.candidates(self.root,1,700))
        self.assertEqual((match["role"],match["line"]),("阿青","回来就好！"))
        choices,_ = dialogue.subtitle_choices(self.root,{"movie":1,"ms":700})
        direct, = [choice for choice in choices if "subtitle" in choice]
        self.assertEqual(direct["role"],"阿青")
        ambiguous,_ = dialogue.subtitle_choices(self.root,{"movie":1,"ms":1200})
        self.assertFalse(any(choice.get("role") for choice in ambiguous))
        caption = match.pop("subtitle")
        dataset.save_boxes(self.root,1,[[0,.1,.1,.9,.9,{"role":"阿青","lines":[match["line"]],
                           "script_refs":[match],"subtitle_refs":[caption]}]],"reviewed")
        bundle = self.folder/"学习包.zip"
        dataset.export_samples(self.root,bundle,lambda _:None)
        with zipfile.ZipFile(bundle) as archive:
            source = json.loads(archive.read("scripts.jsonl").splitlines()[0])
            note = json.loads(archive.read("samples.jsonl"))["objects"][0]
            self.assertEqual(note["script_refs"][0]["script_id"],source["script_id"])
            self.assertEqual(note["character"],source["role"])

    def test_matches_nearby_subtitle_fragments_and_synchronized_translation(self):
        self.add_review_fixture()
        script = self.folder/"剧本.txt"
        script.write_text("阿青：你本来答应护送大家到前方的小镇，怎么突然改变主意了？\n"
                          "旁白：这是因为你还没有遇上那个送你蓝色手套的人。\n"
                          "[00:00:04.100 - 00:00:04.800] 小白：保持安静！\n"
                          "[00:00:06.100 - 00:00:06.800] 导游：保持安静！\n",encoding="utf-8")
        scripts.import_script(self.root,1,script)
        subtitles.store_track(self.root,1,"zh","中文","zh",[(1500,1800,"你本来答应护送大家"),
                                                           (4200,4700,"保持安静"),
                                                           (5300,5500,"是因为你还没遇见送你蓝色手套的人")])
        subtitles.store_track(self.root,1,"en","英文","en",[(1500,1800,"You promised to escort the group.")])
        choices,hint = dialogue.subtitle_choices(self.root,{"movie":1,"ms":1400})
        self.assertIn("前后",hint)
        raw = {choice["text"]:choice["role"] for choice in choices if "subtitle" in choice}
        self.assertEqual(raw,{"你本来答应护送大家":"阿青","You promised to escort the group.":"阿青",
                              "保持安静":"小白","是因为你还没遇见送你蓝色手套的人":"旁白"})

    def test_speaker_from_confirmed_subtitle_is_scoped_to_the_same_cue(self):
        self.add_review_fixture()
        subtitles.store_track(self.root,1,"zh","中文","test",[(500,1000,"谢谢你。"),(3500,4000,"谢谢你。")])
        cue = subtitles.candidates(self.root,1,700)[0]
        ref = {k:cue[k] for k in ("source","start_ms","end_ms","text")}
        note = {"role":"手工确认的角色","lines":[cue["text"]],"subtitle_refs":[ref]}
        dataset.save_boxes(self.root,1,[[0,.1,.1,.9,.9,note]],"reviewed")
        first,_ = dialogue.subtitle_choices(self.root,{"movie":1,"ms":700})
        later,_ = dialogue.subtitle_choices(self.root,{"movie":1,"ms":3700})
        self.assertEqual(first[0]["role"],note["role"])
        self.assertFalse(later[0]["role"])

    def test_cast_is_ordered_and_scoped_to_a_movie(self):
        self.add_review_fixture()
        with dataset.connect(self.root) as db:
            db.execute("INSERT INTO movies(digest,name,mode) VALUES('other','另一部电影','train')")
        self.assertEqual(dataset.save_cast(self.root,1," 小白 \r\n\r\n阿青\r\n小白\r\n"),2)
        self.assertEqual(dataset.cast_names(self.root,1),["小白","阿青"])
        self.assertEqual(dataset.role_names(self.root,2),[])
        dataset.save_boxes(self.root,1,[[0,.1,.1,.9,.9,{"role":"阿青","lines":[]}]],"reviewed")
        self.assertEqual(dataset.role_names(self.root,1),["小白","阿青"])
        dataset.save_cast(self.root,1,"小白\n阿红")
        dataset.initialize(self.root)
        self.assertEqual(dataset.cast_names(self.root,1),["小白","阿红"])
        self.assertEqual(dataset.role_names(self.root,1),["小白","阿红","阿青"])

    def test_deleting_frames_excludes_them_from_workflows_and_restores_annotations(self):
        self.add_review_fixture()
        labels = [[0,.1,.1,.8,.9,{"role":"阿青","lines":["我来了。"]}]]
        dataset.save_boxes(self.root,1,labels,"reviewed")
        with dataset.connect(self.root) as db:
            db.execute("UPDATE frames SET batch_id='first' WHERE id=1")
            for i in (2,3):
                db.execute("INSERT INTO frames(movie,slot,ms,path,split,batch_id) VALUES(1,?,?,?,'train','second')",
                           (i,i*1000,f"frames/{i}.jpg"))
        self.assertEqual([r["id"] for r in dataset.prediction_frames(self.root,1,"second",frame_id=3)],[3])
        self.assertEqual(dataset.set_frames_deleted(self.root,1,[1,3],deleted=True),2)
        self.assertEqual([r["id"] for r in dataset.frames(self.root,1)],[2])
        self.assertEqual(dataset.frame_batches(self.root,1),["first","second"])
        self.assertFalse(dataset.prediction_frames(self.root,1,"second",frame_id=3))
        self.assertFalse(film_learning.snapshot(self.root)[1])
        self.assertFalse(dataset.role_names(self.root,1))
        self.assertEqual(dataset.save_boxes(self.root,3,[],"predicted",only_new=True),0)
        with self.assertRaisesRegex(ValueError,"先确认"):
            dataset.export_samples(self.root,self.folder/"空.zip",lambda _:None)
        self.assertEqual(dataset.set_frames_deleted(self.root,1,[1,3],deleted=False),2)
        self.assertEqual(json.loads(dataset.frames(self.root,1)[0]["boxes"]),labels)
        self.assertEqual([r["id"] for r in film_learning.snapshot(self.root)[1]],[1])

    def test_nearby_subtitle_lookup_preserves_exact_lookup(self):
        self.add_review_fixture()
        subtitles.store_track(self.root,1,"test","test","test",[(1000,1300,"之前"),(2500,3000,"之后"),(9000,9500,"太远")])
        self.assertEqual(subtitles.candidates(self.root,1,2000),[])
        nearby = subtitles.candidates(self.root,1,2000,nearby_ms=5000)
        self.assertEqual([cue["text"] for cue in nearby],["之前","之后"])

    def test_subtitle_correction_survives_reimport_and_exports_effective_text(self):
        self.add_review_fixture()
        raw = [(500,1000,"回来就好？\n去门外。")]
        subtitles.store_track(self.root,1,"track","中文字幕","source",raw)
        cue = subtitles.candidates(self.root,1,700)[0]
        subtitles.correct_cue(self.root,1,cue,"回来就好。\n去门外看看。")
        subtitles.store_track(self.root,1,"track","中文字幕","reindexed",raw)
        updated = subtitles.candidates(self.root,1,700)[0]
        self.assertEqual(updated["text"],"回来就好。\n去门外看看。")
        self.assertEqual(updated["original_text"],raw[0][2])
        text_path = self.folder/"字幕.srt"
        subtitles.export_srt(self.root,cue["track_id"],text_path)
        self.assertIn(updated["text"],text_path.read_text(encoding="utf-8-sig"))
        dataset.save_boxes(self.root,1,[],"reviewed")
        bundle = self.folder/"素材.zip"
        dataset.export_samples(self.root,bundle,lambda _:None)
        with zipfile.ZipFile(bundle) as archive:
            self.assertEqual(json.loads(archive.read("subtitles.jsonl"))["text"],updated["text"])
        subtitles.correct_cue(self.root,1,updated,updated["original_text"])
        self.assertEqual(subtitles.candidates(self.root,1,700)[0]["text"],raw[0][2])

    def test_context_uses_reviewed_snapshot_and_skips_unchanged_feed(self):
        self.add_review_fixture()
        voice = {"kind":"narration","role":"旁白","lines":["电影开始了。"]}
        dataset.save_boxes(self.root,1,[],"reviewed",voices=[voice])
        run = self.root/"runs"/"first"
        film_learning.train(self.root,"unused",1,"cpu",lambda _:None,run=run)
        matches = film_learning.search_context(run/"context.json","film",text="电影开始了",kind="narration")
        self.assertEqual(matches[0]["role"],"旁白")
        no_change = self.root/"runs"/"no-change"
        film_learning.train(self.root,"unused",1,"cpu",lambda _:None,run=no_change,previous=run/"completed.json")
        self.assertFalse(no_change.exists())
        reviewed = film_learning.snapshot(self.root)
        voice["lines"] = ["电影结束了。"]
        dataset.save_boxes(self.root,1,[],"reviewed",voices=[voice])
        frozen = self.root/"runs"/"frozen-feed"
        film_learning.train(self.root,"unused",1,"cpu",lambda _:None,run=frozen,reviewed=reviewed)
        stored = json.loads((frozen/"context.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["entries"][0]["lines"],["电影开始了。"])
        second = self.root/"runs"/"second"
        film_learning.train(self.root,"unused",1,"cpu",lambda _:None,run=second,previous=run/"completed.json")
        self.assertTrue((second/"completed.json").exists())


if __name__=="__main__":
    unittest.main()
