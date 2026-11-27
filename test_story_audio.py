"""Real PCM, silence/mouth alignment and portable native audio publication."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from story_audio import _read,_write,prepare_story_audio,mouth_keys,RATE
from story_pipeline import generate_storyboard
from story_automation import generate_animation
import test_aecpp_resources as fixtures


def speech(lines,folder,report,check):
    result=[]
    for line in lines:
        check()
        t=np.arange(RATE*3)/RATE
        sound=.15*np.sin(2*np.pi*230*t)
        sound[:RATE//2]=0;sound[RATE:RATE+RATE//2]=0;sound[-RATE//4:]=0
        _write(line['path'],sound)
        result.append({**line,'voice':'offline-fixture'})
    return result


class StoryAudioTests(unittest.TestCase):
    def test_speech_drives_duration_and_pause_closes_mouth_and_effects_stay_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            original=generate_storyboard('场景：教室\n小明：你好。\n小红（开心地跳起）：好耶！\n旁白：午饭开始了。')
            before=json.dumps(original)
            board,bundle=prepare_story_audio(original,folder,synthesizer=speech)
            self.assertEqual(json.dumps(original),before)
            self.assertTrue(all(s['duration']>=3.3 for s in board['shots']))
            self.assertEqual(bundle['summary']['voiced_shots'],3)
            self.assertEqual(bundle['summary']['mouth_clips'],2)
            self.assertEqual(bundle['summary']['effect_events'],1)
            for voice in bundle['voices']:
                self.assertEqual(voice['keys'][-1]['openness'],0)
                self.assertTrue(any(k['openness']>.5 for k in voice['keys']))
                self.assertTrue(any(k['openness']==0 for k in voice['keys'] if k['time']>.5))
            self.assertTrue(any(t['id']=='effects' and t['volume']<1 for t in bundle['tracks']))
            self.assertTrue(all(Path(t['path']).is_file() and t['waveform'] for t in bundle['tracks']))

    def test_many_roles_fit_native_track_budget_with_separate_mouths(self):
        with tempfile.TemporaryDirectory() as folder:
            text='\n'.join(f'角色{i}：今天吃饭。' for i in range(33))
            board,b=prepare_story_audio(generate_storyboard(text),folder,effects=False,synthesizer=speech)
            self.assertEqual(len(b['tracks']),1)
            self.assertEqual(len(b['voices']),33)
            self.assertEqual(len({v['character_id'] for v in b['voices']}),33)
            self.assertEqual(b['voices'][-1]['audio_track_id'],b['tracks'][0]['id'])
            self.assertGreater(b['voices'][-1]['keys'][0]['time'],90)

    def test_native_export_keeps_audio_relative_linked_editable_and_packaged(self):
        f=fixtures.ResourceTests();f.setUp();self.addCleanup(f.doCleanups)
        root=f.root/'project';root.mkdir()
        with patch('story_audio.synthesize',side_effect=speech):
            r=generate_animation(root,'场景：教室\n小明：你好。\n小红（开心地跳起）：吃饭了！',
                                 resource_root=f.library,sound=True)
        native=json.loads(r['output'].read_text(encoding='utf-8'))['scene']
        self.assertEqual(len(native['audioTracks']),3)
        for track in native['audioTracks']:
            self.assertTrue((r['output'].parent/track['path']).is_file())
            self.assertFalse(Path(track['path']).is_absolute())
            self.assertGreater(len(_read(r['output'].parent/track['path'])),0)
        actor=next(l['actor'] for l in native['layers'] if l['contentKind']==3)
        mouth=actor['mouths'][0]
        self.assertIn(mouth['audioTrackId'],{t['id'] for t in native['audioTracks']})
        self.assertTrue(mouth['keyframes'])
        self.assertTrue(all(not e['speakingEnabled'] for e in actor['expressions']))
        report=json.loads(r['report_path'].read_text(encoding='utf-8'))
        self.assertEqual(report['sound']['voiced_shots'],2)
        self.assertEqual(report['shot_matches'][0]['duration'],r['board']['shots'][0]['duration'])
        self.assertGreater(report['sound']['timing'][0]['after'],report['sound']['timing'][0]['before'])

    def test_silent_synthesis_or_cancellation_does_not_publish_a_fake_success(self):
        f=fixtures.ResourceTests();f.setUp();self.addCleanup(f.doCleanups)
        root=f.root/'project';root.mkdir()
        def silent(lines,folder,report,check):
            for l in lines:_write(l['path'],np.zeros(RATE))
            return [{**l,'voice':'fixture'} for l in lines]
        with patch('story_audio.synthesize',side_effect=silent):
            with self.assertRaisesRegex(ValueError,'静音'):
                generate_animation(root,'小明：你好。',resource_root=f.library,sound=True)
        self.assertFalse(list(root.glob('creations/animations/*/story.aecpp')))
        with patch('story_audio.synthesize',side_effect=RuntimeError('cancelled')):
            with self.assertRaisesRegex(RuntimeError,'cancelled'):
                generate_animation(root,'小明：你好。',resource_root=f.library,sound=True)
        self.assertFalse(list(root.glob('creations/animations/*/story.aecpp')))

    def test_repeated_dialogue_uses_one_linked_envelope_per_character_with_closed_gaps(self):
        f=fixtures.ResourceTests();f.setUp();self.addCleanup(f.doCleanups)
        root=f.root/'project';root.mkdir()
        with patch('story_audio.synthesize',side_effect=speech):
            r=generate_animation(root,'小明：第一句。\n小红：轮到我了。\n小明：第三句。',resource_root=f.library,sound=True)
        native=json.loads(r['output'].read_text(encoding='utf-8'))['scene']
        mouths=[m for l in native['layers'] for m in l.get('actor',{}).get('mouths',[])]
        self.assertEqual(len(mouths),2)
        first=next(m for m in mouths if m['audioTrackId']=='voice_01')
        self.assertEqual(first['start'],.1)
        self.assertEqual(first['offset'],0)
        self.assertTrue(any(k['time']>6 and k['openness']>.5 for k in first['keyframes']))
        self.assertTrue(all(k['openness']==0 for k in first['keyframes'] if 3<=k['time']<6))


class SoundWindowTests(unittest.TestCase):
    def test_sound_choice_is_snapshotted_and_restored_without_extra_steps(self):
        import test_story_oneclick_window as windows
        f=windows.StoryOneClickWindowTests();f.setUp();self.addCleanup(f.tearDown)
        f.window.sound_enabled.set(True)
        with patch('story_automation.generate_animation',side_effect=lambda *a,**k:f.result()) as generate:
            self.assertTrue(f.window._generate_animation())
            self.assertEqual(str(f.window.sound_check.cget('state')),'disabled')
            f.wait_idle()
        self.assertEqual(generate.call_args.kwargs['sound'],{'enabled':True,'effects':True})
        f.window.sound_enabled.set(False)
        f.window.save_current()
        f.reopen()
        self.assertFalse(f.window.sound_enabled.get())
