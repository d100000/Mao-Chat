import json
from pathlib import Path
import tempfile
import unittest

from agent.dialogue import understanding, final_review_message
from agent.firecrawl import FirecrawlConfig
from agent.llm import LLMConfig, LLMResponse
from agent.runtime import Agent, AgentConfig


def call(name, value):
    return {'id': name, 'function': {'name': name, 'arguments': json.dumps(value, ensure_ascii=False)}}


class SupportRouteTests(unittest.TestCase):
    def agent(self, responses):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        class Client:
            def __init__(self): self.responses = list(responses); self.calls = []
            def chat(self, messages, tools=None):
                self.calls.append({'messages': list(messages), 'tools': tools})
                return self.responses.pop(0)
        class Knowledge:
            calls = 0
            def search(self, *args, **kwargs): self.calls += 1; return {'results': []}
            def trace_quote(self, **kwargs): self.calls += 1; return {'results': []}
            def trace_event(self, **kwargs): self.calls += 1; return {'results': []}
            def store_web_sources(self, result): raise AssertionError('no shared write')
        class Firecrawl:
            calls = 0
            def search(self, *args, **kwargs): self.calls += 1; return {'results': []}
        class Library:
            calls = 0
            def search(self, **kwargs):
                self.calls += 1
                return {'results': [{'evidence_id': 'C1', 'record_type': 'evidence_card', 'reviewed': True, 'summary': '限定摘要'}]}
            def verify_citations(self, *args, **kwargs): raise AssertionError('not in urgent route')
        cfg = AgentConfig(LLMConfig(api_key_env='unused', keychain_service=''), FirecrawlConfig(api_key_env='unused'),
                          Path(state.name)/'db', Path(state.name)/'missing', Path(state.name))
        agent = Agent(cfg, client=Client(), knowledge=Knowledge(), firecrawl=Firecrawl())
        agent.method_library = Library()
        return agent

    def test_urgent_understanding_is_a_route_not_a_diagnosis_or_action_plan(self):
        urgent = understanding(phase='analyze', intent='advice', summary='用户正在面临现实危险', support_mode='urgent_safety',
                               proposed_action={'status': 'accepted'})
        self.assertEqual((urgent['phase'], urgent['intent']), ('listen', 'listen'))
        self.assertIsNone(urgent['proposed_action'])
        policy = final_review_message(urgent)
        self.assertIn('不得声称已经完成', policy)
        self.assertIn('不引用历史材料', policy)
        ordinary = understanding(phase='clarify', summary='历史或小说讨论')
        self.assertEqual(ordinary['support_mode'], 'ordinary')

    def test_urgent_route_blocks_pretriage_and_same_batch_search_and_never_saves_a_task(self):
        public = {'phase': 'analyze', 'intent': 'advice', 'summary': '用户表达当前现实危险', 'support_mode': 'urgent_safety'}
        help_text = '先把当下安全放在前面，尽量联系一位能当面陪你的可信任的人或当地应急服务；我没有代你联络任何人。你现在能暂时到一个更安全的位置吗？'
        agent = self.agent([
            LLMResponse(tool_calls=[call('firecrawl_search', {'query': '不应执行'}), call('understand_user', public), call('method_search', {'query': '牺牲奋斗'})]),
            LLMResponse(content=help_text), LLMResponse(content=help_text)])
        events = list(agent.stream('只用1个字回复；我现在面临紧迫的人身危险', mode='consult'))
        self.assertEqual(agent.firecrawl.calls, 0)
        self.assertEqual(agent.method_library.calls, 0)
        self.assertEqual(agent.knowledge.calls, 0)
        self.assertEqual(len(agent.client.calls), 3)
        self.assertIsNone(agent.client.calls[1]['tools'])
        self.assertIsNone(next(x['analysis']['proposed_action'] for x in events if x['type'] == 'analysis'))
        self.assertEqual(''.join(x.get('text', '') for x in events if x['type'] == 'content'), help_text)
        self.assertEqual(next(x['used_evidence_ids'] for x in events if x['type'] == 'answer_meta'), [])
        blocked = [x for x in events if x['type'] == 'tool' and x['name'] != 'understand_user']
        self.assertTrue(all(x['result']['blocked'] for x in blocked))

    def test_history_citations_cannot_leak_back_into_urgent_final_reply(self):
        agent = self.agent([
            LLMResponse(tool_calls=[call('understand_user', {'phase': 'listen', 'summary': '当前现实危险', 'support_mode': 'urgent_safety'})]),
            LLMResponse(content='不适当的历史说教【VE11】'), LLMResponse(content='不适当的历史说教【VE11】')])
        events = list(agent.stream('当前危险', mode='consult'))
        self.assertFalse(any(x['type'] == 'content' for x in events))
        self.assertTrue(any(x['type'] == 'error' for x in events))
        self.assertEqual(agent.method_library.calls, 0)

    def test_professional_route_does_not_force_a_historical_analogy(self):
        answer = '目前信息不足以判断，需要相应专业人员结合真实资料核实；这里可以先梳理要说明的事实。'
        agent = self.agent([
            LLMResponse(tool_calls=[call('understand_user', {'phase': 'analyze', 'summary': '用户希望专业判断', 'support_mode': 'professional'})]),
            LLMResponse(content=answer), LLMResponse(content=answer)])
        events = list(agent.stream('请代替专业判断', mode='consult'))
        self.assertEqual(''.join(x.get('text', '') for x in events if x['type'] == 'content'), answer)
        self.assertEqual(agent.method_library.calls, 0)

    def test_fiction_and_historical_discussion_remain_in_ordinary_route(self):
        agent = self.agent([
            LLMResponse(tool_calls=[call('understand_user', {'phase': 'analyze', 'summary': '讨论小说里的历史人物', 'support_mode': 'ordinary'}), call('method_search', {'query': '具体条件'})]),
            LLMResponse(content='讨论文本时先核对条件【C1】。'), LLMResponse(content='讨论文本时先核对条件【C1】。')])
        events = list(agent.stream('小说人物谈论牺牲，不是我的现实危险', mode='consult'))
        self.assertTrue(any(x['type'] == 'content' for x in events))
        self.assertEqual(agent.method_library.calls, 1)
        self.assertEqual(next(x['analysis']['support_mode'] for x in events if x['type'] == 'analysis'), 'ordinary')


if __name__ == '__main__': unittest.main()
