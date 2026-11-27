import hashlib
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile

import dataset
from learning_telemetry import (dashboard_snapshot,record_point,read_history,visual_event,METRICS)


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'project';dataset.initialize(self.root)

    def counts(self,**changes):return {'ready':True,**{k:0 for k in METRICS},**changes}

    def test_exact_observed_history_deduplicates_and_can_decrease(self):
        self.assertEqual(read_history(self.root),[])
        self.assertTrue(record_point(self.root,self.counts(visual_clip_count=4),now=100))
        self.assertFalse(record_point(self.root,self.counts(visual_clip_count=4),now=110))
        self.assertTrue(record_point(self.root,self.counts(visual_clip_count=4),now=161))
        record_point(self.root,self.counts(visual_clip_count=2),now=162)
        points=read_history(self.root)
        self.assertEqual([p['visual_clip_count'] for p in points],[4,4,2])
        self.assertEqual([p['time'] for p in points],[100,161,162])

    def test_stale_or_invalid_counts_never_recorded(self):
        self.assertFalse(record_point(self.root,{'ready':False}))
        for invalid in (-1,True,float('nan'),'3'):
            with self.assertRaises(ValueError):record_point(self.root,self.counts(video_count=invalid))
        self.assertEqual(read_history(self.root),[])

    def test_sampling_history_preserves_actual_first_and_latest(self):
        for i in range(12):record_point(self.root,self.counts(video_count=i),now=100+i)
        history=read_history(self.root,4)
        self.assertEqual(len(history),4)
        self.assertEqual((history[0]['video_count'],history[-1]['video_count']),(0,11))
        self.assertEqual([p['video_count'] for p in history],[0,4,7,11])

    def test_empty_project_snapshot_no_background_managers_or_fake_visuals(self):
        data=dashboard_snapshot(self.root)
        self.assertEqual(data['totals']['video_count'],0)
        self.assertEqual(len(data['history']),1)
        self.assertIsNone(data['visual'])
        self.assertFalse((self.root/'online_jobs.sqlite').exists())
        self.assertFalse((self.root/'auto_learning.sqlite').exists())

    def test_event_bounded_local_frames_hashes_and_real_rhythm(self):
        frames=[]
        for i in range(3):
            path=self.root/f'{i}.jpg';path.write_bytes(bytes([i])*10)
            frames.append({'path':str(path),'time_ms':100+i*500})
        result=visual_event(self.root,{'frames':frames,'start_ms':0,'end_ms':1500,'clip_index':1,
            'clip_count':2,'model':'local','observation':{'action':'talk','emotion':'neutral','camera':'medium','confidence':.8}},
            {'method':'ffmpeg-scene-difference','duration_ms':4000,'cut_times_ms':[1000,2000,3000]})
        self.assertEqual(result['frames'][0]['path'],'0.jpg')
        self.assertEqual(result['frames'][0]['sha256'],hashlib.sha256(bytes(10)).hexdigest())
        self.assertEqual(result['rhythm']['median_seconds'],1)
        self.assertEqual(result['rhythm']['cut_count'],3)
        self.assertEqual(result['observation']['action'],'talk')
        frames[0]['path']=str(Path(self.temp.name)/'outside.jpg')
        Path(frames[0]['path']).write_bytes(b'outside')
        with self.assertRaises(ValueError):visual_event(self.root,{'frames':frames})
        self.assertIsNone(visual_event(self.root,{'frames':frames[:1]}))

    def test_history_database_is_backed_up_in_portable_export(self):
        from portable_project import export_project
        record_point(self.root,self.counts(visual_clip_count=7),now=100)
        archive=Path(self.temp.name)/'export.zip'
        export_project(self.root,archive,include_videos=False)
        with zipfile.ZipFile(archive) as z:
            name=next(n for n in z.namelist() if n.endswith('learning_metrics.sqlite'))
            copied=Path(self.temp.name)/'copied.sqlite';copied.write_bytes(z.read(name))
        with closing(sqlite3.connect(copied)) as db:
            self.assertEqual(json.loads(db.execute('SELECT data FROM points').fetchone()[0])['visual_clip_count'],7)

if __name__=='__main__':unittest.main()
