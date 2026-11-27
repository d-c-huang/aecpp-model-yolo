import gc
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch

from PIL import Image
from aecpp_preview import _run,audio_filters
from story_assembly import AssemblyTask,explain_assembly,load_assembly,latest_assembly
from story_automation import generate_animation
from test_online_jobs import wait_for
import test_aecpp_resources as fixtures


class AssemblyTests(unittest.TestCase):
    def setUp(self):
        f=self.fixture=fixtures.ResourceTests();f.setUp();self.addCleanup(f.doCleanups)
        self.root=f.root/'p';self.root.mkdir()
        self.generated=generate_animation(self.root,'场景：教室\n小明：今天不用上班。',resource_root=f.library)
        self.folder=self.generated['run_dir'];self.value=load_assembly(self.folder)

    def manifest(self):
        folder=self.folder/'preview-test';folder.mkdir()
        for i in range(2):Image.new('RGB',(80,50),(i*100,30,90)).save(folder/f'{i}.png')
        (folder/'p.mp4').write_bytes(b'video fixture')
        data={'folder':folder.name,'duration':1,'full_duration':1,'fps':2,'frames':['0.png','1.png'],
            'video':'p.mp4','project_sha256':hashlib.sha256(self.generated['output'].read_bytes()).hexdigest()}
        (self.folder/'assembly-preview.json').write_text(json.dumps(data),encoding='utf-8')
        return data

    def test_explanation_uses_final_actual_assets_and_post_speech_time(self):
        board=self.generated['board'];matching=json.loads(self.generated['report_path'].read_text(encoding='utf-8'))
        selection=matching['shot_matches'][0]
        selection['visual_reference']={'movie_id':7,'start_ms':100,'end_ms':3000,'confidence':.8,
            'applied_fields':['emotion','duration'],'changes':{'emotion':{'before':'neutral','after':'sad'},
              'duration':{'before':2,'after':1.6,'observed_seconds':1.2}}}
        matching['sound']={'timing':[{'shot_id':board['shots'][0]['id'],'before':1.6,'after':4,'speech_seconds':3.7}]}
        board['shots'][0]['duration']=selection['duration']=4
        value=explain_assembly(board,matching,self.generated['output']);row=value['rows'][0]
        self.assertEqual(row['duration'],4);self.assertEqual(row['reference']['movie_id'],7)
        self.assertEqual(row['sound_timing']['after'],4)
        self.assertEqual(row['action']['asset_id'],selection['action']['asset_id'])
        self.assertIn(row['action']['name'],[m['asset_name'] for m in matching['matches']])

    def test_real_preview_restores_and_modified_project_invalidates_only_preview(self):
        self.manifest();self.assertTrue(load_assembly(self.folder)['preview'])
        self.generated['output'].write_text(self.generated['output'].read_text(encoding='utf-8')+' ',encoding='utf-8')
        value=latest_assembly(self.root)
        self.assertTrue(value['rows']);self.assertIsNone(value['preview']);self.assertIn('变化',value['preview_error'])

    def test_foreign_or_missing_preview_preserves_editable_project_and_explanation(self):
        data=self.manifest();data['frames'][0]='../outside.png'
        (self.folder/'assembly-preview.json').write_text(json.dumps(data),encoding='utf-8')
        value=load_assembly(self.folder)
        self.assertTrue(Path(value['output']).is_file());self.assertIsNone(value['preview']);self.assertTrue(value['preview_error'])

    def test_failed_preview_does_not_regenerate_or_hide_successful_project(self):
        task=AssemblyTask(self.root)
        with patch('aecpp_preview.render_preview',side_effect=ValueError('fixture engine unavailable')),\
             patch('story_assembly.render_preview',side_effect=ValueError('fixture engine unavailable')):
            task.start('小明：午饭开始了。',resources=self.fixture.library,sound=False)
            wait_for(lambda:not task.busy,timeout=15)
        state=task.snapshot();self.assertFalse(state['error']);value=state['result']
        self.assertTrue(Path(value['output']).is_file());self.assertIn('unavailable',value['preview_error'])
        self.assertEqual(len(list((self.root/'creations/animations').iterdir())),2)

    def test_task_rejects_concurrent_start_and_restores_last_generation(self):
        task=AssemblyTask(self.root);entered,release=threading.Event(),threading.Event();self.addCleanup(release.set)
        def delayed(*a,**kw):entered.set();release.wait(5);raise ValueError('cancelled fixture')
        with patch('story_automation.generate_animation',side_effect=delayed):
            self.assertTrue(task.start('小明：你好。'));self.assertTrue(entered.wait(2))
            self.assertFalse(task.start('小红：你好。'));task.cancel();release.set();wait_for(lambda:not task.busy)
        self.assertIn('cancelled',task.snapshot()['error'])
        task.restore();wait_for(lambda:not task.busy)
        self.assertEqual(task.snapshot()['result']['output'],str(self.generated['output']))

    def test_audio_mixer_respects_native_trim_offset_volume_and_speed(self):
        filters=audio_filters([{'sourceOffset':1,'duration':3,'offset':.4,'volume':.25,'speed':4}])
        self.assertIn('atrim=start=1.0:duration=12.0',filters)
        self.assertIn('atempo=2,atempo=2.0',filters)
        self.assertIn('volume=0.25,adelay=400',filters)
        self.assertIn('normalize=0',filters)

    def test_cancel_terminates_only_owned_preview_subprocess(self):
        stop=threading.Event();timer=threading.Timer(.15,stop.set);timer.start()
        try:
            with self.assertRaisesRegex(ValueError,'取消'):_run([sys.executable,'-c','import time; time.sleep(20)'],stop)
        finally:timer.join()


class AssemblyPanelTests(unittest.TestCase):
    def setUp(self):
        import test_learning_dashboard as views
        self.fixture=views.DashboardTests();self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.dashboard=self.fixture.view;self.panel=self.dashboard.assembly;self.task=self.dashboard.assembly_task
        wait_for(lambda:not self.task.busy)

    def test_input_error_stays_visible_and_generation_snapshots_tk_values(self):
        self.assertFalse(self.panel.generate());self.panel._poll()
        self.assertIn('正文',self.panel.status.get())
        self.panel.source.insert('1.0','小明：你好。')
        with patch.object(self.task,'start',return_value=True) as start:
            self.assertTrue(self.panel.generate())
            self.assertTrue(start.call_args.kwargs['sound'])
            self.assertEqual(start.call_args.args[0],'小明：你好。')

    def test_close_releases_tk_values_before_background_gc_without_cancel(self):
        errors=[]
        with patch('sys.unraisablehook',side_effect=errors.append),patch.object(self.task,'cancel') as cancel:
            self.dashboard.close()
            worker=threading.Thread(target=gc.collect);worker.start();worker.join(2)
            self.assertIsNone(self.panel.title);self.assertIsNone(self.dashboard.follow)
            self.assertFalse(errors);cancel.assert_not_called()

    def test_evidence_selection_keeps_exact_clip_and_source_fingerprint(self):
        reference={'movie_id':1,'clip_id':'visual-3','reference_path':'saved.json','source_hash':'a'*64}
        self.dashboard._show_assembly_evidence(reference)
        self.assertEqual(self.dashboard._evidence_selection,reference)
        self.assertFalse(self.dashboard.follow.get())
        self.assertEqual(self.dashboard.tabs.select(),str(self.dashboard.process_page))
        self.dashboard._select_movie();self.assertIsNone(self.dashboard._evidence_selection)

    def test_project_switch_is_blocked_while_assembly_runs(self):
        from app import Studio
        studio=Studio.__new__(Studio);studio.busy=False;studio.training=None;studio.story_window=None
        studio.learning_assembly_task=Mock(busy=True);studio.status=Mock()
        self.assertFalse(studio.use('elsewhere'));studio.status.set.assert_called_once()

if __name__=='__main__':unittest.main()
