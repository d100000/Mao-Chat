import json
from pathlib import Path
import unittest
from agent.dialogue import response_contract, response_contract_message, final_review_message, answer_format_limits
from agent.tests.test_dialogue import Client, call, interpretation
from agent.llm import LLMResponse
from agent.runtime import Agent
from agent.tests import test_dialogue as fixtures


class ResponseContractTests(unittest.TestCase):
    def test_fixed_eight_requests_and_explicit_format(self):
        rows = json.loads((Path(__file__).with_name('mrmao_scenarios.json')).read_text())
        for row in rows:
            with self.subTest(row=row['id']):
                limits = answer_format_limits(row['question'])
                contract = response_contract(row['analysis'], limits)
                self.assertEqual(contract['style'], row['style'])
                self.assertEqual(contract['format_limits'], limits)
                self.assertIn(response_contract_message(row['analysis'], limits), final_review_message(row['analysis'], limits))
        self.assertEqual(response_contract({'phase':'listen','intent':'advice'})['action_policy'], 'none')
        self.assertNotIn('比较选项', response_contract_message({'phase':'listen'}))

    def test_special_support_precedes_style_and_urgent_format(self):
        for mode in ('professional','urgent_safety'):
            result = response_contract({'support_mode':mode,'intent':'compare','phase':'analyze'}, {'sentences':3})
            self.assertEqual(result['style'], mode)
            self.assertFalse(result['retrieval_required'])
            if mode == 'urgent_safety': self.assertEqual(result['format_limits'], {})

    def test_contract_reuses_generation_and_review_calls(self):
        # Reuse fixture setup without collecting its entire inherited test suite.
        fixture = fixtures.DialogueTests(); fixture.setUp()
        try:
            client = Client([LLMResponse('', '', [call('understand_user', interpretation('listen', intent='listen'))], {}),
                             LLMResponse('草稿不发布', '', [], {}), LLMResponse('我听到了，可以慢慢说。', '', [], {})])
            agent = Agent(fixture.config, client=client)
            events = list(agent.stream('先听我说', mode='consult', network_enabled=False))
            self.assertEqual(len(client.calls), 3)
            self.assertTrue(any('【本轮回应要求】' in str(m['content']) for m in client.calls[1]['messages']))
            self.assertIn('【本轮回应要求】', client.calls[2]['messages'][-1]['content'])
            self.assertEqual(''.join(e.get('text','') for e in events if e['type']=='content'), '我听到了，可以慢慢说。')
            self.assertFalse(any(e['type']=='reasoning' for e in events))
        finally: fixture.tearDown()
