"""Conservative prose cues for one-click assembly from existing animation assets."""
from pathlib import Path
import tempfile
import unittest

import story_pipeline as story


class NovelAutomaticDirectionTests(unittest.TestCase):
    def test_short_novel_keeps_named_subjects_dialogue_and_setting_together(self):
        text = ('清晨，小明走进客厅，笑着说：“今天发财了！”'
                '小红瞪着小明说：“先交房租。”'
                '过了一会儿，小红跑到厨房。')
        board = story.generate_storyboard(text)
        self.assertEqual([c['name'] for c in board['characters']], ['小明', '小红'])
        self.assertEqual([(s['speaker'], s['text']) for s in board['shots'] if s['speaker']],
                         [('character-1', '今天发财了！'), ('character-2', '先交房租。')])
        self.assertEqual([s['scene'] for s in board['shots']], ['客厅'] * 4 + ['厨房'])
        self.assertEqual([s['actor'] for s in board['shots']],
                         ['character-1', 'character-1', 'character-2', 'character-2', 'character-2'])
        self.assertEqual(board['source_text'], text)
        self.assertEqual(''.join(s['text'] for s in board['shots']), text.replace('“', '').replace('”', ''))
        self.assertIn('小明走进客厅，笑着说', board['shots'][1]['role_evidence'])
        self.assertIn('小红瞪着小明说', board['shots'][3]['role_evidence'])

    def test_named_action_changes_on_comma_do_not_move_the_previous_person(self):
        board = story.generate_storyboard('小明走进客厅，小红突然跳起来。小明震惊得发抖。', source_format='novel')
        self.assertEqual([c['name'] for c in board['characters']], ['小明', '小红'])
        self.assertEqual([(s['actor'], s['action']) for s in board['shots']],
                         [('character-1', 'walk'), ('character-2', 'jump'), ('character-1', 'shake')])
        self.assertTrue(all(not s['speaker'] for s in board['shots']))

    def test_post_quote_evidence_does_not_leak_to_next_unattributed_quote(self):
        board = story.generate_storyboard('“谁在门口？”，小明问道。“不知道。”\n“是我。”小红回答。')
        spoken = [s for s in board['shots'] if s['text'] in ('谁在门口？', '不知道。', '是我。')]
        self.assertEqual([s['speaker'] for s in spoken], ['character-1', '', 'character-2'])
        self.assertEqual(spoken[0]['role_evidence'], '原文引号后归属：小明问道')
        self.assertNotIn('role_evidence', spoken[1])
        self.assertEqual(board['shots'][1]['actor'], 'character-1')

    def test_prose_said_tag_is_preserved_in_auto_and_explicit_novel_modes(self):
        for source_format in ('auto', 'novel'):
            for text in ('小明说道：“你好！”', '小明说道：你好！'):
                with self.subTest(source_format=source_format, text=text):
                    board = story.generate_storyboard(text, source_format=source_format)
                    self.assertEqual([c['name'] for c in board['characters']], ['小明'])
                    self.assertEqual([s['text'] for s in board['shots']], ['小明说道：', '你好！'])
                    self.assertEqual([s['speaker'] for s in board['shots']], ['', 'character-1'])

    def test_pronouns_never_inherit_the_named_object_or_previous_speaker(self):
        text = '小红：在这里。\n他看着小红，笑着说：“快跑！”\n她问：“谁？”\n我说：“不知道。”'
        board = story.generate_storyboard(text)
        self.assertEqual([c['name'] for c in board['characters']], ['小红'])
        self.assertTrue(all(not s['speaker'] and not s['actor'] for s in board['shots'][1:]))

    def test_subjectless_actions_and_descriptions_do_not_create_people(self):
        text = ('一个人物得知薪水到账，双手抬起。正在发抖。慢慢地走。突然跳了起来。'
                '笑着说：“哈哈。”他跑进厨房。小明和小红走进客厅。')
        board = story.generate_storyboard(text, source_format='novel')
        self.assertEqual(board['characters'], [])
        self.assertTrue(all(not s['speaker'] and not s['actor'] for s in board['shots']))

    def test_numbered_scene_headings_and_arrivals_change_only_actual_setting(self):
        text = ('场景一：客厅\n小明：以后去厨房。\n'
                '小明打算走进办公室。小明没有走进教室。小红想走进厨房。\n'
                '在厨房里，小红笑着说：“终于到了。”\n'
                '第二场：公园\n小明回到客厅。')
        board = story.generate_storyboard(text)
        self.assertEqual([c['name'] for c in board['characters']], ['小明', '小红'])
        self.assertEqual([s['scene'] for s in board['shots']], ['客厅', '客厅', '客厅', '客厅', '厨房', '厨房', '客厅'])
        self.assertEqual(board['shots'][5]['speaker'], 'character-2')

    def test_quoted_story_or_destination_does_not_create_cast_or_switch_scene(self):
        board = story.generate_storyboard('场景：客厅\n小明说：“小红跑到厨房，小王跳了起来。”\n“老板跑到公园。”')
        self.assertEqual([c['name'] for c in board['characters']], ['小明'])
        self.assertTrue(all(s['scene'] == '客厅' for s in board['shots']))
        self.assertEqual(board['shots'][-1]['speaker'], '')
        self.assertEqual(board['shots'][-1]['actor'], '')

    def test_narrative_subject_is_retained_when_long_sentence_is_chunked(self):
        text = '小明走进客厅，' + '一边欣赏墙上的画，一边仔细观察四周，' * 10 + '终于停下脚步。'
        board = story.generate_storyboard(text, source_format='novel')
        self.assertGreater(len(board['shots']), 2)
        self.assertEqual([c['name'] for c in board['characters']], ['小明'])
        self.assertTrue(all(s['actor'] == 'character-1' and not s['speaker'] for s in board['shots']))
        self.assertEqual(''.join(s['text'] for s in board['shots']), text)

    def test_source_and_attribution_evidence_survive_portable_save(self):
        board = story.generate_storyboard('小明看着小红，轻声说：“你好。”\n“你好。”小红回答。')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '小说分镜.json'
            story.save_storyboard(board, path)
            self.assertEqual(story.load_storyboard(path), board)
        self.assertTrue(all(s.get('role_evidence') for s in board['shots'] if s['speaker']))


if __name__ == '__main__':
    unittest.main()
