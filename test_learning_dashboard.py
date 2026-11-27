from copy import deepcopy
import gc
import hashlib
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import Mock,patch

from PIL import Image
import dataset
import theme
from learning_telemetry import dashboard_snapshot
from learning_dashboard import LearningDashboard,phase_stage


class DashboardTests(unittest.TestCase):
    def setUp(self):
        gc.collect();self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        dataset.initialize(self.root)
        try:self.tk=tk.Tk()
        except tk.TclError as exc:self.skipTest(str(exc))
        self.tk.withdraw();theme.apply_theme(self.tk)
        self.data=dashboard_snapshot(self.root)
        frames=[]
        for i in range(3):
            path=self.root/f'{i}.jpg';Image.new('RGB',(180,120),(i*80,60,110)).save(path)
            frames.append({'path':path.name,'time_ms':1000+i*2000,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
        self.data.update(job={'movie_id':1,'title':'动画','last_visual':{}},source='https://www.iqiyi.com/v_demo.html',
            learning={'movie_id':1,'title':'动画','phase':'visual-understanding','message':'正在理解'},
            movies=[{'id':1,'title':'动画','status':'running'}],visual={'frames':frames,'start_ms':0,'end_ms':6000,
                'model':'local','clip_index':1,'clip_count':2,'rhythm':{'cut_times_ms':[1500,3000],'duration_ms':6000,'cut_count':2}})
        self.calls=[];self.start=Mock();self.stop=Mock();self.closed=Mock()
        def loader(root,selected):self.calls.append((threading.get_ident(),selected));return deepcopy(self.data)
        self.view=LearningDashboard(self.tk,self.root,loader=loader,runtime=lambda:{'active':True,'phase':'searching'},
            on_start=self.start,on_stop=self.stop,on_close=self.closed)
        self.until(lambda:len(self.view.photos)==3)

    def tearDown(self):
        if hasattr(self,'view') and not self.view._closed:self.view.close()
        if hasattr(self,'view') and self.view._worker:self.view._worker.join(2)
        if hasattr(self,'tk'):self.tk.destroy()
        if hasattr(self,'view'):del self.view
        gc.collect();self.temp.cleanup()

    def until(self,predicate,timeout=3):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            self.tk.update()
            if predicate():return
            time.sleep(.02)
        self.fail('UI condition timed out')

    def test_real_frame_loop_pause_resume_and_background_loading(self):
        count=self.view._animation_count
        self.until(lambda:self.view._animation_count>=count+2)
        self.assertNotEqual(self.calls[0][0],threading.get_ident())
        self.assertIn('原片',self.view.frame_label.get())
        self.view._toggle_play();count=self.view._animation_count
        end=time.monotonic()+.8
        while time.monotonic()<end:self.tk.update();time.sleep(.02)
        self.assertEqual(self.view._animation_count,count)
        self.view._toggle_play();self.until(lambda:self.view._animation_count>count)
        self.assertIn('iqiyi.com',self.view.source.get())

    def test_live_labels_clear_for_next_window_and_show_previous_observation(self):
        data=deepcopy(self.data)
        observation={'action':'walk','emotion':'shocked','camera':'medium','confidence':.8,'evidence':'身体位置改变'}
        data['visual']['observation']=observation;self.view._apply(data)
        self.assertIn('走路',self.view.observation.get());self.assertIn('80%',self.view.observation.get())
        self.assertIn('身体位置改变',self.view.evidence.get())
        data['job']['last_visual']=deepcopy(data['visual'])
        data['visual'].pop('observation');data['visual']['start_ms']=7000
        self.view._apply(data)
        self.assertIn('等待',self.view.observation.get());self.assertIn('走路',self.view.previous.get())
        self.assertEqual(self.view.evidence.get(),'')

    def test_growth_charts_history_selection_and_minimum_layout(self):
        self.view.win.geometry('900x640');self.tk.update()
        self.assertGreater(self.view.preview.winfo_height(),60)
        for c in self.view.charts:self.assertGreater(c.winfo_height(),90)
        self.view.chart_vars[0].set('证据画面（帧）');self.view._draw_charts()
        self.assertTrue(self.view.charts[0].find_all())
        self.view._select_movie();self.view._refresh_now()
        self.until(lambda:any(selected==1 for _,selected in self.calls))
        self.assertFalse(self.view.follow.get())
        self.assertTrue(all(p.width()<=self.view.preview.winfo_width() for p in self.view.photos))

    def test_corrupt_or_missing_frame_is_not_displayed(self):
        data=deepcopy(self.data);data['visual']['frames'][0]['sha256']='0'*64
        self.view._apply(data);self.assertFalse(self.view.photos)
        self.assertIn('变化',self.view.frame_label.get())

    def test_close_only_disposes_view_and_timers(self):
        self.view.close();self.assertTrue(self.view._closed)
        self.assertFalse(self.view._timers);self.closed.assert_called_once()
        self.stop.assert_not_called()

    def test_phase_mapping_handles_empty_and_visual_and_publish(self):
        self.assertEqual(phase_stage(None),'')
        self.assertEqual(phase_stage('visual-understanding'),'画面')
        self.assertEqual(phase_stage('learning'),'入库')

    def test_cached_analysis_performance_is_separate_from_cumulative_results(self):
        data=deepcopy(self.data);data['learning']=None
        data['job']['model_analysis']={'performance':{'wall_seconds':1.2,'visual':{'cache_hits':2,'model_requests':0},
            'text':{'cache_hits':1},'scan_cache_hit':True,'rhythm_cache_hit':True}}
        self.view._apply(data)
        self.assertIn('1.20',self.view.efficiency.get());self.assertIn('新增推理 0',self.view.efficiency.get())
        self.assertIn('切镜已复用',self.view.efficiency.get())


class DashboardAppTests(unittest.TestCase):
    def test_dashboard_reuses_view_and_uses_lightweight_runtime(self):
        from app import Studio
        app=Studio.__new__(Studio);app.root=Path('fixture');app.win=Mock()
        app.get_continuous_learning_manager=Mock(return_value=Mock(snapshot=Mock(return_value={'active':False})))
        with patch('learning_dashboard.LearningDashboard') as view:
            app.show_learning_dashboard();app.show_learning_dashboard()
            view.assert_called_once();view.return_value.show.assert_called_once()
            self.assertEqual(view.call_args.kwargs['runtime'](),{'active':False})
            view.call_args.kwargs['on_close']();self.assertIsNone(app.learning_dashboard)

if __name__=='__main__':unittest.main()
