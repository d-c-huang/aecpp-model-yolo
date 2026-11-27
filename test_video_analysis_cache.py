import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from video_analysis_cache import AnalysisCache,signature,file_hash
from video_semantics import AnalysisCancelled,analyse_transcript
from test_video_semantics import server,analysis,transcript
from test_video_visual import response
import test_video_visual as visual_fixtures
import video_visual as visual


class CacheStorageTests(unittest.TestCase):
    def test_corrupt_oversize_and_foreign_cache_are_misses(self):
        with tempfile.TemporaryDirectory() as folder:
            cache=AnalysisCache(folder);key=signature({'real':'request'})
            self.assertTrue(cache.put(key,{'answer':3}));self.assertEqual(cache.get(key),{'answer':3})
            (Path(folder)/(key+'.json')).write_text('{"version":1}',encoding='utf-8')
            self.assertIsNone(cache.get(key));self.assertIsNone(cache.get('../escape'))
            self.assertFalse(cache.put(key,{'text':'x'*(2*1024*1024)}))

    def test_file_identity_uses_bytes_and_cancellation(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'source';path.write_bytes(b'old');before=file_hash(path)
            path.write_bytes(b'new');self.assertNotEqual(before,file_hash(path))
            stopped=threading.Event();stopped.set()
            with self.assertRaises(AnalysisCancelled):file_hash(path,stopped)


@unittest.skipUnless(shutil.which('ffmpeg'),'real video evidence')
class VisualCacheTests(unittest.TestCase):
    def setUp(self):
        self.fixture=visual_fixtures.VisualTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.folder=self.fixture.folder;self.cache=self.folder/'cache';self.digest='a'*64;self.chat_hook=None

    def respond(self,path):
        if path=='/api/tags':return {'models':[{'name':'fixture-vision','digest':self.digest}]}
        if path=='/api/chat' and self.chat_hook:return self.chat_hook()
        return response(path)

    def analyse(self,url,**kwargs):return self.fixture.analyse(url,cache_dir=self.cache,**kwargs)

    def chats(self,calls):return [c for c in calls if c['path']=='/api/chat']

    def test_identical_input_reuses_complete_results_with_real_current_frames(self):
        with server(value=self.respond) as (url,calls):
            first=self.analyse(url);second=self.analyse(url)
        self.assertEqual(len(self.chats(calls)),1)
        self.assertEqual(second['performance']['cache_hits'],1)
        self.assertEqual(second['performance']['model_requests'],0)
        clean=lambda value:[{k:v for k,v in c.items() if k!='frames'} for c in value['clips']]
        self.assertEqual(clean(first),clean(second))
        self.assertTrue(all(Path(f['path']).is_file() for f in second['clips'][0]['frames']))
        self.assertEqual(self.chats(calls)[0]['body']['keep_alive'],300)

    def test_model_prompt_subtitles_and_source_bytes_each_invalidate(self):
        with server(value=self.respond) as (url,calls):
            self.analyse(url);self.digest='b'*64;self.analyse(url)
            with patch.object(visual,'SYSTEM_PROMPT',visual.SYSTEM_PROMPT+'\n使用相同质量标准。'):self.analyse(url)
            self.analyse(url,observations=[{'start_ms':0,'end_ms':1500,'text':'改变字幕上下文'}])
            with self.fixture.source.open('ab') as stream:stream.write(b'changed source identity')
            self.analyse(url)
        self.assertEqual(len(self.chats(calls)),5)

    def test_model_version_change_during_request_cannot_publish_checkpoint(self):
        def changed():self.digest='c'*64;return response('/api/chat')
        self.chat_hook=changed
        with server(value=self.respond) as (url,calls):
            with self.assertRaisesRegex(ValueError,'模型版本'):self.analyse(url)
        self.assertEqual(list(self.cache.glob('*.json')),[])

    def test_cancelled_batch_resumes_successful_clip_only(self):
        source=self.folder/'long.mkv'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=96x64:rate=5:duration=14','-c:v','ffv1',str(source)],check=True,capture_output=True)
        args={'duration_ms':14000,'observations':[],'cut_candidates_ms':[2000,4000,8000,12000],
              'output_dir':self.folder/'resume-frames','model':'fixture-vision','cache_dir':self.cache,'max_clips':2}
        stop=threading.Event()
        def report(event):
            if event.get('observation'):stop.set()
        with server(value=self.respond) as (url,calls):
            with self.assertRaises(AnalysisCancelled):visual.analyse_visual(source,base_url=url,report=report,stop=stop,**args)
            self.assertEqual(len(self.chats(calls)),1)
            self.assertEqual(list(args['output_dir'].glob('*.jpg')),[])
            result=visual.analyse_visual(source,base_url=url,**args)
        self.assertEqual(len(self.chats(calls)),2)
        self.assertEqual(len(result['clips']),2)
        self.assertEqual(result['performance']['cache_hits'],1)
        self.assertEqual(result['performance']['model_requests'],1)

    def test_corrupted_cached_response_is_revalidated_then_recomputed(self):
        with server(value=self.respond) as (url,calls):
            self.analyse(url);path=next(self.cache.glob('*.json'));saved=json.loads(path.read_text(encoding='utf-8'))
            saved['value']['message']['content']='{"action":"invented"}'
            AnalysisCache(self.cache).put(saved['key'],saved['value'])
            result=self.analyse(url)
        self.assertEqual(len(self.chats(calls)),2);self.assertEqual(result['performance']['cache_hits'],0)

    def test_unconfirmed_model_version_runs_fresh_without_false_cache(self):
        def no_version(path):return {} if path=='/api/tags' else response(path)
        with server(value=no_version) as (url,calls):
            self.analyse(url);result=self.analyse(url)
        self.assertEqual(len(self.chats(calls)),2);self.assertEqual(result['performance']['cache_hits'],0)

    def test_low_confidence_and_unknown_are_preserved_not_promoted_by_cache(self):
        from test_video_visual import observation
        self.chat_hook=lambda:{'message':{'content':json.dumps(observation(action='unknown',emotion='unknown',camera='unknown',confidence=.2))}}
        with server(value=self.respond) as (url,calls):
            self.analyse(url);result=self.analyse(url)
        self.assertEqual(result['clips'][0]['confidence'],.2)
        self.assertEqual(result['clips'][0]['action'],'unknown');self.assertEqual(len(self.chats(calls)),1)


class TranscriptCacheTests(unittest.TestCase):
    def test_transcript_checkpoints_reuse_and_context_change_invalidates(self):
        with tempfile.TemporaryDirectory() as folder:
            def respond(path):
                return {'models':[{'name':'text','digest':'d'*64}]} if path=='/api/tags' else {'message':{'content':json.dumps(analysis())}}
            args={'model':'text','cache_dir':folder,'source_hash':'source'}
            with server(value=respond) as (url,calls):
                first=analyse_transcript(transcript(),base_url=url,**args)
                repeated=analyse_transcript(transcript(),base_url=url,**args)
                changed=copy.deepcopy(transcript());changed['segments'][0]['text']='不同的真实字幕'
                analyse_transcript(changed,base_url=url,**args)
            self.assertEqual(first['contentBlocks'],repeated['contentBlocks'])
            self.assertEqual(repeated['analysis']['performance']['cache_hits'],1)
            self.assertEqual(len([c for c in calls if c['path']=='/api/chat']),2)


@unittest.skipUnless(shutil.which('ffmpeg'),'real video decoding')
class ScanCacheTests(unittest.TestCase):
    def setUp(self):
        import test_video_visual_pipeline as f
        self.fixture=f.VisualPipelineTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.root=self.fixture.root;self.source=self.fixture.source

    def test_coarse_scan_reuses_video_but_rebuilds_corrected_subtitles(self):
        from auto_learning import analyse_movie
        import subtitles
        cache=self.root/'video_analysis/cache';digest=file_hash(self.source)
        first=analyse_movie(self.root,self.fixture.movie,cache_dir=cache,source_hash=digest)
        subtitles.store_track(self.root,self.fixture.movie,'manual','人工字幕','fixture',[(0,1500,'更正后的真实字幕')])
        with patch('auto_learning._coarse_scan',side_effect=AssertionError('scan should be reused')):
            changed=analyse_movie(self.root,self.fixture.movie,cache_dir=cache,source_hash=digest)
        self.assertEqual(first['cut_candidates_ms'],changed['cut_candidates_ms'])
        self.assertTrue(changed['scan_cache_hit']);self.assertEqual(changed['observations'][0]['text'],'更正后的真实字幕')

    def test_rhythm_cache_retains_all_cuts_without_another_decoder(self):
        from video_rhythm import analyse_rhythm
        cache=self.root/'video_analysis/cache';digest=file_hash(self.source)
        first=analyse_rhythm(self.source,duration_ms=6000,cache_dir=cache,source_hash=digest)
        with patch('video_rhythm.subprocess.Popen',side_effect=AssertionError('no repeat decoding')):
            repeated=analyse_rhythm(self.source,duration_ms=6000,cache_dir=cache,source_hash=digest)
        self.assertTrue(repeated['cache_hit']);repeated.pop('cache_hit');self.assertEqual(first,repeated)

    def test_performance_settings_are_bounded_and_round_trip(self):
        from video_analysis import clean_settings,save_settings,load_settings
        for bad in (-1,True,3601,'300'):
            with self.assertRaises(ValueError):clean_settings({'ollama_keep_alive':bad})
        save_settings(self.root,{'analysis_cache':False,'ollama_keep_alive':0})
        self.assertFalse(load_settings(self.root)['analysis_cache']);self.assertEqual(load_settings(self.root)['ollama_keep_alive'],0)

    def test_source_digest_change_is_detected_even_when_size_and_mtime_match(self):
        from video_analysis import analyse_video,save_settings
        before=self.source.stat()
        with self.source.open('r+b') as stream:
            stream.seek(-8,2);value=stream.read(1);stream.seek(-1,1);stream.write(bytes([value[0]^1]))
        os.utime(self.source,ns=(before.st_atime_ns,before.st_mtime_ns))
        save_settings(self.root,{'asr_enabled':False})
        with self.assertRaisesRegex(ValueError,'摘要已变化'):analyse_video(self.root,self.fixture.movie)

if __name__=='__main__':unittest.main()
