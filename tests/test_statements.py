"""Provenance and user controls must survive summaries, repeats and pruning."""
from copy import deepcopy
import unittest
import uuid

from agent.issues import public_metadata
from agent.statements import (MAX_STATEMENTS, add_turn, effective_analysis,
                              prompt_statement_context, public_statements,
                              update_statement)


def understanding(goal='保住客户', **extra):
    return {'phase': 'analyze', 'summary': '模型整理的暂定理解', 'stage_goal': goal,
            'goal': goal, 'long_term_direction': '', 'turn_request': '一起梳理',
            'known_facts': ['团队两人'], 'constraints': ['没有额外预算'],
            'uncertainties': ['稳定性的具体含义未知'], **extra}


class StatementTests(unittest.TestCase):
    def turn(self, meta, data=None, question='希望保住客户，团队两人，没有额外预算。'):
        return add_turn(meta, data or understanding(), question, uuid.uuid4().hex, '2026-10-27T00:00:00Z')

    def test_excerpt_must_be_in_the_current_user_message_and_is_not_confirmation(self):
        meta = {}
        self.turn(meta, understanding(statement_sources=[
            {'field': 'stage_goal', 'quote': '保住客户'},
            {'field': 'known_facts:0', 'quote': '团队两人'},
            {'field': 'constraints:0', 'quote': '预算无穷'},
            {'field': 'uncertainties:0', 'quote': '团队两人'},
            {'field': 'api_key', 'quote': '保住客户'}]))
        goal = next(x for x in meta['statements'] if x['field'] == 'stage_goal')
        self.assertEqual(goal['origin'], 'user_excerpt')
        self.assertEqual(goal['source_excerpt'], '保住客户')
        self.assertEqual(goal['confirmation'], 'tentative')
        constraint = next(x for x in meta['statements'] if x['field'] == 'constraints:0')
        self.assertEqual((constraint['origin'], constraint['source_excerpt']), ('model_summary', ''))
        unknown = next(x for x in meta['statements'] if x['field'] == 'uncertainties:0')
        self.assertEqual(unknown['origin'], 'model_summary')
        self.assertTrue(all(x['source_turn_id'] for x in meta['statements']))

    def test_pausing_correction_and_resume_do_not_claim_the_action_was_accepted(self):
        meta = {'actions': [{'id': 'action', 'status': 'proposed'}]}
        self.turn(meta)
        goal = next(x for x in meta['statements'] if x['field'] == 'stage_goal')
        update_statement(meta, goal['statement_id'], 'inactive', timestamp='t2')
        self.assertEqual(meta['analysis']['stage_goal'], '')
        self.turn(meta)  # A repetition by the model is not a user resume.
        self.assertEqual(meta['analysis']['goal'], '')
        update_statement(meta, goal['statement_id'], 'active', timestamp='t3')
        self.assertEqual(meta['analysis']['stage_goal'], '保住客户')
        update_statement(meta, goal['statement_id'], corrected_value='先保住健康', timestamp='t4')
        current = next(x for x in meta['statements'] if x['field'] == 'stage_goal' and x['status'] == 'active')
        self.assertNotEqual(current['statement_id'], goal['statement_id'])
        self.assertEqual(current['supersedes'], goal['statement_id'])
        self.assertEqual((current['origin'], current['confirmation']), ('user_correction', 'confirmed'))
        self.turn(meta)
        self.assertEqual(meta['analysis']['goal'], '先保住健康')
        self.assertEqual(meta['actions'][0]['status'], 'proposed')
        self.assertTrue(any(x['reason'] == '用户在理解笔记中更正' for x in meta['goal_history']))
        with self.assertRaises(ValueError):
            update_statement(meta, goal['statement_id'], 'confirmed', timestamp='t5')

    def test_new_current_user_source_can_replace_an_earlier_confirmed_scalar(self):
        meta = {}
        self.turn(meta)
        row = next(x for x in meta['statements'] if x['field'] == 'stage_goal')
        update_statement(meta, row['statement_id'], 'confirmed', timestamp='t2')
        self.turn(meta, understanding('先看外部机会', statement_sources=[{'field':'stage_goal','quote':'先看外部机会'}]), '现在先看外部机会。')
        self.assertEqual(meta['analysis']['stage_goal'], '先看外部机会')
        self.assertEqual(next(x for x in meta['statements'] if x['statement_id'] == row['statement_id'])['status'], 'superseded')
        self.assertEqual(meta['goal_history'][-1]['source_excerpt'], '先看外部机会')
        self.assertEqual(meta['goal_history'][-1]['confirmation'], 'tentative')

    def test_attributable_withdrawal_can_leave_a_confirmed_goal_unknown(self):
        meta = {}
        self.turn(meta)
        row = next(x for x in meta['statements'] if x['field'] == 'stage_goal')
        update_statement(meta, row['statement_id'], 'confirmed', timestamp='t2')
        self.turn(meta, understanding('', statement_sources=[{'field':'stage_goal','quote':'不再想保住客户'}]), '我不再想保住客户，先不知道目标。')
        self.assertEqual(meta['analysis']['stage_goal'], '')
        self.assertEqual(meta['goal_history'][-1]['source_excerpt'], '不再想保住客户')
        self.turn(meta)  # The assistant cannot revive the withdrawn goal.
        self.assertEqual(meta['analysis']['goal'], '')

    def test_later_actual_excerpt_can_trace_an_earlier_summary_without_confirming_it(self):
        meta = {}
        self.turn(meta)
        before = next(x for x in meta['statements'] if x['field'] == 'known_facts:0')
        identity = before['statement_id']
        self.assertEqual(before['origin'], 'model_summary')
        self.turn(meta, understanding(statement_sources=[{'field':'known_facts:0','quote':'团队两人'}]), '补充说一下，现在团队两人。')
        after = next(x for x in meta['statements'] if x['statement_id'] == identity)
        self.assertEqual(after['origin'], 'user_excerpt')
        self.assertEqual(after['source_excerpt'], '团队两人')
        self.assertEqual(after['confirmation'], 'tentative')

    def test_paused_list_fact_is_removed_from_summary_memory_and_can_be_restored(self):
        meta = {}
        self.turn(meta)
        row = next(x for x in meta['statements'] if x['field'] == 'known_facts:0')
        update_statement(meta, row['statement_id'], 'inactive', timestamp='t2')
        self.assertNotIn('团队两人', meta['analysis']['known_facts'])
        self.assertNotIn('团队两人', meta['analysis']['summary'])
        self.turn(meta)
        self.assertEqual(meta['analysis']['known_facts'], [])
        context = prompt_statement_context(meta)
        self.assertFalse(any(x['value'] == '团队两人' for x in context['active']))
        self.assertTrue(any(x['value'] == '团队两人' for x in context['excluded']))
        update_statement(meta, row['statement_id'], 'active', timestamp='t3')
        self.assertIn('团队两人', meta['analysis']['known_facts'])

    def test_paused_list_fact_cannot_reappear_under_a_new_unsourced_paraphrase(self):
        meta = {}
        self.turn(meta)
        row = next(x for x in meta['statements'] if x['field'] == 'constraints:0')
        update_statement(meta, row['statement_id'], 'inactive', timestamp='t2')
        self.turn(meta, understanding(constraints=['预算不能增加'], statement_sources=[]), '继续讨论。')
        self.assertEqual(meta['analysis']['constraints'], [])
        self.assertNotIn('预算不能增加', meta['analysis']['summary'])
        self.assertFalse(any(x['status'] == 'active' and x['value'] == '预算不能增加' for x in meta['statements']))
        self.turn(meta, understanding(constraints=['可用预算100元'], statement_sources=[{'field':'constraints:0','quote':'可用预算100元'}]), '现在可用预算100元。')
        self.assertEqual(meta['analysis']['constraints'], ['可用预算100元'])

    def test_legacy_metadata_does_not_gain_invented_sources_or_confirmations(self):
        original = {'analysis': understanding(), 'mode': 'consult'}
        output = public_metadata(original)
        self.assertEqual(output['statements'], [])
        self.assertEqual(output['goal_history'], [])
        self.assertNotIn('source_excerpt', str(output['analysis']))
        self.assertEqual(original['analysis']['summary'], '模型整理的暂定理解')

    def test_bounded_history_keeps_explicit_controls_and_feedback_not_invented_progress(self):
        meta = {'actions': [{'status':'accepted'}]}
        self.turn(meta)
        paused = next(x for x in meta['statements'] if x['field'] == 'known_facts:0')
        update_statement(meta, paused['statement_id'], 'inactive', timestamp='pause')
        for i in range(120):
            self.turn(meta, understanding('目标%d' % i, phase='review'), '这次尚未执行，只想再整理。')
        self.assertLessEqual(len(meta['statements']), MAX_STATEMENTS)
        self.assertEqual(len(meta['goal_history']), 12)
        self.assertEqual(len(meta['review_history']), 12)
        self.assertTrue(any(x['statement_id'] == paused['statement_id'] and x['status'] == 'inactive' for x in meta['statements']))
        self.assertEqual(meta['actions'][0]['status'], 'accepted')
        self.assertIn('不证明', meta['review_history'][-1]['notice'])

    def test_sanitizers_do_not_alias_caller_data_or_accept_model_control_fields(self):
        meta = {}
        self.turn(meta)
        raw = deepcopy(meta['statements'])
        raw[0]['api_key'] = 'do-not-export'
        clean = public_statements(raw)
        clean[0]['value'] = 'changed'
        self.assertNotEqual(clean[0]['value'], raw[0]['value'])
        self.assertNotIn('api_key', clean[0])
        with self.assertRaises(ValueError):
            update_statement(meta, meta['statements'][0]['statement_id'], 'confirmed', '混合操作', timestamp='t')
        with self.assertRaises(ValueError):
            update_statement(meta, 'other-owner-id', 'inactive', timestamp='t')


if __name__ == '__main__':
    unittest.main()
