"""Existing actor assembly, portable dependencies and native consumer rendering."""
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image
import test_aecpp_resources as fixtures
from aecpp_resources import scan_resources
from aecpp_resource_export import export_resource_project


class ResourceExportTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.ResourceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root,self.board=self.fixture.root,self.fixture.board
        self.catalog=scan_resources(self.fixture.assets)
        self.target=self.root/"movie.aecpp"

    def test_existing_actor_actions_images_and_subtitles_survive_library_removal(self):
        export_resource_project(self.board,self.target,catalog=self.catalog)
        project=json.loads(self.target.read_text(encoding="utf-8"))
        self.assertEqual({layer["contentKind"] for layer in project["scene"]["layers"]},{1,3})
        actors=[row for row in project["scene"]["layers"] if row["contentKind"]==3]
        self.assertEqual(len(actors),2)
        self.assertEqual(actors[0]["actor"]["actions"][0]["actionId"],"talk")
        self.assertEqual(actors[0]["actor"]["actions"][1]["actionId"],"idle")
        self.assertTrue(project["scene"]["subtitles"][0]["style"]["content"].startswith("小明："))
        images=[p for p in self.target.with_suffix('.assets').rglob('*') if p.is_file() and p.suffix in {'.png','.jpg'}]
        self.assertTrue(images)
        self.assertTrue(all(len(p.stem)==5 for p in images))
        library=self.fixture.library.resolve()
        self.assertTrue(library.is_relative_to(self.root.resolve()))
        shutil.rmtree(library)
        for actor in actors:
            file=self.root/actor["actor"]["resourcePath"]
            doc=json.loads(file.read_text(encoding="utf-8"))
            image=file.parent/doc["boneContours"][0]["image"]["path"]
            self.assertTrue(image.is_file())
            self.assertEqual(doc["motions"],self.fixture.document["motions"])
        for bg in [row for row in project["scene"]["layers"] if row["contentKind"]==1]:
            self.assertTrue((self.root/bg["imagePath"]).is_file())

    def test_custom_existing_expression_and_mouth_variants_copied(self):
        # Base changes the main expression image; native speaking variants only
        # accept actual mouth roles, never the catalog's "base" role.
        index_path=self.fixture.expression_dir/"expression-catalog.json"
        index=json.loads(index_path.read_text(encoding="utf-8"))
        base=dict(index["assets"][0]["variants"][0],role="base")
        index["assets"][0]["variants"].insert(0,base)
        self.fixture.write(index_path,index)
        self.catalog=scan_resources(self.fixture.assets)
        cid=self.board["characters"][0]["id"]
        export_resource_project(self.board,self.target,catalog=self.catalog,
            bindings={"emotions":{cid:{"neutral":"custom:big-laugh"}}})
        project=json.loads(self.target.read_text(encoding="utf-8"))
        actor=next(row for row in project["scene"]["layers"] if row["contentKind"]==3)
        clip=actor["actor"]["expressions"][0]
        self.assertEqual(clip["expressionAssetId"],"custom:big-laugh")
        self.assertTrue((self.root/clip["imagePath"]).is_file())
        self.assertEqual(clip["speakingVariants"][0]["role"],"open")
        self.assertEqual(len(clip["speakingVariants"]),1)
        self.assertTrue((self.root/clip["speakingVariants"][0]["imagePath"]).is_file())

    def test_missing_or_changed_materials_cannot_create_placeholder_export(self):
        self.board["shots"][0]["action"]="fall"
        with self.assertRaisesRegex(ValueError,"素材"):
            export_resource_project(self.board,self.target,catalog=self.catalog)
        self.assertFalse(self.target.exists())
        self.board["shots"][0]["action"]="talk"
        self.fixture.image(self.fixture.actor_dir/"body.png",size=(22,20))
        with self.assertRaises(ValueError):
            export_resource_project(self.board,self.target,catalog=self.catalog)
        self.assertFalse(self.target.exists())
        self.assertFalse(self.target.with_suffix(".assets").exists())

    def test_existing_outputs_preserved_and_failed_publication_cleans_own_materials(self):
        self.target.write_text("existing",encoding="utf-8")
        with self.assertRaises(ValueError):
            export_resource_project(self.board,self.target,catalog=self.catalog)
        self.assertEqual(self.target.read_text(),"existing")
        second=self.root/"new.aecpp"
        with patch("aecpp_resource_export._write_new",side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                export_resource_project(self.board,second,catalog=self.catalog)
        self.assertFalse(second.exists())
        self.assertFalse(second.with_suffix(".assets").exists())
        self.assertFalse(list(self.root.glob(".aecpp-materials-*")))


AECPP=Path(__file__).resolve().parent.parent/"AECPP"
ENGINE=Path(os.environ.get("AECPP_ENGINE_PATH",AECPP/"out/build/mingw-release/aecpp.exe"))


@unittest.skipUnless(ENGINE.is_file() and (AECPP/"assets/actors").is_dir(),"Local AECPP consumer and resources")
class NativeResourceExportTests(unittest.TestCase):
    def test_native_engine_renders_copied_actors_from_portable_materials(self):
        from story_pipeline import generate_storyboard
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            catalog=scan_resources(AECPP)
            board=generate_storyboard("场景：客厅\n小明：今天发工资了。\n小明开心地跳了起来。")
            bindings={"backgrounds":{"客厅":catalog["backgrounds"][0]["id"]}}
            source=export_resource_project(board,root/"actors.aecpp",catalog=catalog,bindings=bindings)
            process=subprocess.Popen([str(ENGINE),"--engine","--asset-root",str(root),"--session-root",str(root)],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,encoding="utf-8",
                creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            events=queue.Queue()
            def read():
                for line in process.stdout:
                    try:events.put(json.loads(line))
                    except ValueError:pass
            reader=threading.Thread(target=read,daemon=True);reader.start()
            number=0
            def request(method,params):
                nonlocal number
                number+=1
                process.stdin.write(json.dumps({"id":number,"method":method,"params":params})+"\n");process.stdin.flush()
                while True:
                    result=events.get(timeout=30)
                    if result.get("id")==number:
                        self.assertNotIn("error",result)
                        return result["result"]
            try:
                state=request("openProject",{"path":str(source)})
                actor=next(row for row in state["layers"] if row["kind"]=="actor")
                self.assertTrue(Path(actor["actorResourcePath"]).is_relative_to(root))
                self.assertEqual([row["actionId"] for row in actor["actions"]],["talk","jump"])
                frames=[]
                for t in (.4,board["shots"][0]["duration"]+.4):
                    frame=request("renderFrame",{"time":t,"width":640,"height":360,"format":"png"})
                    with Image.open(frame["path"]) as image:
                        self.assertEqual(image.size,(640,360))
                        frames.append(image.tobytes())
                self.assertNotEqual(*frames)
                saved=root/"roundtrip.aecpp"
                request("saveProject",{"path":str(saved)})
                value=json.loads(saved.read_text(encoding="utf-8"))
                self.assertEqual({row["contentKind"] for row in value["scene"]["layers"]},{1,3})
            finally:
                process.stdin.close()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
                reader.join(2);process.stdout.close()


if __name__=="__main__":
    unittest.main()
