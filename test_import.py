"""Integration checks using short, locally generated video fixtures."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from PIL import Image
import dataset
from media import open_movie


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg CLI is used to generate fixtures")
class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "中文 工程"
        dataset.initialize(self.root)

    def movie(self, ext, codec):
        path = self.root / ("片 段." + ext)
        subprocess.run(["ffmpeg","-v","error","-f","lavfi","-i","testsrc2=size=160x96:rate=10:duration=3",
                        "-c:v",codec,str(path)],check=True)
        return path

    def test_containers(self):
        for ext,codec in [("mp4","libx264"),("m4v","libx264"),("mkv","libx265"),("mov","mpeg4"),
                          ("avi","mpeg4"),("webm","libvpx-vp9"),("ts","mpeg2video"),("m2ts","mpeg2video"),
                          ("mpg","mpeg1video"),("wmv","wmv2"),("flv","flv"),("h264","libx264")]:
            with self.subTest(extension=ext):
                # MPEG-1 permits only prescribed frame rates.
                if ext=="mpg":
                    path = self.root/"片 段.mpg"
                    subprocess.run(["ffmpeg","-v","error","-f","lavfi","-i","testsrc2=size=160x96:rate=25:duration=3",
                                    "-c:v",codec,str(path)],check=True)
                else:
                    path = self.movie(ext,codec)
                self.assertEqual(dataset.extract(self.root,path,1,"train",lambda _:None),1)
                row = dataset.frames(self.root)[-1]
                with Image.open(self.root/row["path"]) as im:
                    self.assertEqual(im.size,(160,96))
                self.assertTrue(0 <= row["ms"] <= 3100)

    def test_failure_and_resume(self):
        movie = self.movie("mp4","libx264")
        renamed = movie.with_suffix(".unknown")
        movie.rename(renamed)
        self.assertEqual(dataset.extract(self.root,renamed,1,"val",lambda _:None),1)
        self.assertEqual(dataset.extract(self.root,renamed,2,"time",lambda _:None),2)
        self.assertEqual({r["split"] for r in dataset.frames(self.root)},{"val"})
        bad = self.root/"损坏.mp4"
        bad.write_bytes(b"invalid video")
        with self.assertRaises(ValueError):
            with open_movie(bad,lambda _:None):
                pass

    def test_repeat_content_across_layers_and_legacy_index(self):
        path = self.root/"静止镜头.mkv"
        subprocess.run(["ffmpeg","-v","error","-f","lavfi","-i",
                        "color=red:size=64x64:rate=12:duration=6","-c:v","ffv1",str(path)],check=True)
        quiet = lambda _:None
        self.assertEqual(dataset.extract(self.root,path,9,"train",quiet,step_ms=5000),1)
        with dataset.connect(self.root) as db:
            db.execute("UPDATE frames SET content_hash=NULL")  # old project, digest must be recovered
        self.assertEqual(dataset.extract(self.root,path,10,"time",quiet,step_ms=250,start=1,end=5),0)
        self.assertEqual(len(dataset.frames(self.root)),1)

    def test_fine_layer_adds_distinct_frames_in_range(self):
        movie = self.movie("mp4","libx264")
        quiet = lambda _:None
        self.assertEqual(dataset.extract(self.root,movie,1,"train",quiet,step_ms=5000),1)
        self.assertGreater(dataset.extract(self.root,movie,4,"train",quiet,step_ms=250,start=1,end=3),0)
        rows = dataset.frames(self.root)
        self.assertTrue(all(1000<=r["ms"]<3000 for r in rows[1:]))
        self.assertEqual(len(rows),len({r["ms"] for r in rows}))
        self.assertEqual(len(rows),len({r["content_hash"] for r in rows}))


if __name__=="__main__":
    unittest.main()
