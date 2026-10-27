from pathlib import Path
import tempfile
import unittest

from agent.claims import check_answer_claims
from agent.dialogue import understanding
from agent.firecrawl import FirecrawlConfig
from agent.llm import LLMConfig, LLMResponse
from agent.runtime import Agent, AgentConfig


BAD = '这句目前未见可靠出处；选集中有“极有价值的贡献”【03_selected_works_v3:p0244】。引用时不应写成“毛泽东说过……”。'
GOOD = '候选/待核：本地已核资料中未见这句话的对应出处；OCR近似页面不能证明原话。若有具体书名页码或链接，可再核对；当前不能据此认定毛泽东说过或从未说过。'


def call(name, args):
    import json
    return {'id': name, 'function': {'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}}


class QuoteRepairTests(unittest.TestCase):
    def agent(self, replies, *, max_steps=6, reviewed_summary=False):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        row = ({'evidence_id': 'C1', 'record_type': 'evidence_card', 'source_role': 'author_work',
                'reviewed': True, 'verification_status': 'reviewed', 'quote_verified': False, 'quote_eligible': False,
                'summary': '限定摘要'} if reviewed_summary else
               {'anchor': '03_selected_works_v3:p0244', 'record_type': 'passage', 'reviewed': False,
                'verification_status': 'candidate', 'quote_eligible': False, 'text': '未核OCR样例'})
        class Knowledge:
            def search(self, *args, **kwargs): return {'results': []}
            def trace_quote(self, **kwargs): return {'results': [row], 'untrusted': False}
            def trace_event(self, **kwargs): return {'results': []}
            def store_web_sources(self, result): pass
        initial = LLMResponse(tool_calls=[
            call('understand_user', {'phase': 'analyze', 'intent': 'source', 'summary': '用户只核对出处',
                                     'proposed_action': {'title': '不应出现的任务'}}),
            call('trace_quote', {'text': '完成KPI才有价值'})])
        class Client:
            def __init__(self): self.replies = [initial, *replies]; self.calls = []
            def chat(self, messages, tools=None):
                self.calls.append({'messages': list(messages), 'tools': tools})
                return self.replies.pop(0)
        cfg = AgentConfig(LLMConfig(api_key_env='unused', keychain_service=''), FirecrawlConfig(api_key_env='unused'),
                          Path(state.name)/'db', Path(state.name)/'missing', Path(state.name), max_steps=max_steps)
        return Agent(cfg, client=Client(), knowledge=Knowledge(), firecrawl=object())

    def test_captured_candidate_quote_is_removed_by_one_tool_free_source_repair(self):
        checked = check_answer_claims(BAD, [{'result': {'results': [{'anchor': '03_selected_works_v3:p0244', 'record_type': 'passage', 'quote_eligible': False}]}}])
        self.assertFalse(checked['passed'])
        self.assertEqual([item['text'] for item in checked['quote_checks']], ['极有价值的贡献'])
        agent = self.agent([LLMResponse(content=BAD), LLMResponse(content=BAD), LLMResponse(content=GOOD)])
        events = list(agent.stream('只核对“完成KPI才有价值”的出处。', mode='consult'))
        self.assertEqual(len(agent.client.calls), 4)
        self.assertIsNone(agent.client.calls[-1]['tools'])
        content = ''.join(item.get('text', '') for item in events if item['type'] == 'content')
        self.assertEqual(content, GOOD)
        self.assertNotIn('极有价值的贡献', content)
        analysis = next(item['analysis'] for item in events if item['type'] == 'analysis')
        self.assertEqual(analysis['intent'], 'source')
        self.assertIsNone(analysis['proposed_action'])
        self.assertEqual(len([item for item in events if item['type'] == 'analysis']), 1)
        policy = agent.client.calls[-1]['messages'][-1]['content']
        self.assertIn('不得只去掉引号', policy)
        self.assertIn('修改公开analysis', policy)

    def test_repeated_failure_or_exhausted_budget_never_publishes_a_draft(self):
        for steps, replies, calls in [(6, [BAD, BAD, BAD], 4), (3, [BAD, BAD], 3)]:
            with self.subTest(steps=steps):
                agent = self.agent([LLMResponse(content=text) for text in replies], max_steps=steps)
                events = list(agent.stream('只核对出处', mode='consult'))
                self.assertEqual(len(agent.client.calls), calls)
                self.assertFalse(any(item['type'] == 'content' for item in events))
                self.assertTrue(any(item['type'] == 'error' for item in events))

    def test_format_and_quote_repairs_share_one_budget(self):
        bad = '原文：“天道酬勤。”【C1】'
        agent = self.agent([LLMResponse(content=bad), LLMResponse(content=bad),
                            LLMResponse(content='当前没有可核的逐字出处。' * 10)], reviewed_summary=True)
        events = list(agent.stream('只核对出处，最多40字', mode='consult'))
        self.assertEqual(len(agent.client.calls), 4)
        self.assertFalse(any(item['type'] == 'content' for item in events))
        self.assertTrue(any(item['type'] == 'error' and '字数' in item['error'] for item in events))

    def test_source_answer_that_already_passes_needs_no_extra_model_call(self):
        agent = self.agent([LLMResponse(content=GOOD), LLMResponse(content=GOOD)])
        events = list(agent.stream('只核对出处', mode='consult'))
        self.assertEqual(len(agent.client.calls), 3)
        self.assertEqual(''.join(item.get('text', '') for item in events if item['type'] == 'content'), GOOD)
        self.assertIsNone(understanding(phase='analyze', intent='source', summary='核对出处',
                                       proposed_action={'status': 'accepted'})['proposed_action'])

    def test_repair_cannot_remove_quote_marks_and_keep_the_same_rejected_assertion(self):
        bad = '原文：“天道酬勤。”【C1】'
        agent = self.agent([LLMResponse(content=bad), LLMResponse(content=bad),
                            LLMResponse(content='毛泽东的意思是天道酬勤。【C1】')], reviewed_summary=True)
        events = list(agent.stream('只核对出处', mode='consult'))
        self.assertEqual(len(agent.client.calls), 4)
        self.assertFalse(any(item['type'] == 'content' for item in events))
        self.assertTrue(any(item['type'] == 'error' and '转述' in item['error'] for item in events))


if __name__ == '__main__': unittest.main()
