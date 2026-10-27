from pathlib import Path
import json
import tempfile
import unittest
from agent.db import connect, ensure_schema
from agent.principles import PRINCIPLES, search_principles
from agent.dialogue import CuratedMethodLibrary
from agent.knowledge_versions import knowledge_revision
from scripts.evaluate_mrmao import evaluate
from agent.tests import test_dialogue as fixtures
from agent.llm import LLMResponse
from agent.runtime import Agent


class MethodRetrievalTests(unittest.TestCase):
    def test_fixed_editorial_benchmark_no_exact_regression_or_distractors(self):
        r = evaluate()['channels']; old, new = r['lexical'], r['enhanced']
        self.assertGreater(new['top1_pass'], old['top1_pass'])
        for a, b in zip(old['rows'],new['rows']):
            if a['id'].startswith(('exact-', 'none-')):
                self.assertGreaterEqual(b['top1'], a['top1'])

    def test_original_question_is_retrieved_without_becoming_a_fact(self):
        ids={x for p in PRINCIPLES for x in p['evidence_ids']}
        rows=search_principles('方法', ids, original_query='身体快熬坏了还要硬扛')
        self.assertEqual(rows[0]['principle_id'],'principle:people-and-care')
        self.assertFalse(rows[0]['quote_eligible'])
        self.assertIn('counterexample', rows[0])
        self.assertNotIn('身体快熬坏了', json.dumps(rows[0],ensure_ascii=False))
        self.assertFalse(any(r['principle_id']=='principle:people-and-care' for r in search_principles('硬扛身体熬坏',ids-{'VE11'})))

    def test_historical_mode_cannot_invoke_unadvertised_consult_search(self):
        fixture=fixtures.DialogueTests();fixture.setUp()
        try:
            client=fixtures.Client([LLMResponse('', '', [fixtures.call('method_search',{'query':'调查'})], {}),
                                    LLMResponse('当前范围没有可用资料。','',[],{})])
            agent=Agent(fixture.config,client=client)
            agent.method_library.search=lambda **kw:self.fail('historical cutoff bypassed')
            events=list(agent.stream('核对当时资料',mode='historical',as_of='1930-01-01',network_enabled=False))
            result=next(e['result'] for e in events if e['type']=='tool')
            self.assertIn('error',result)
        finally:fixture.tearDown()

    def test_revocation_during_generation_prevents_publication(self):
        fixture=fixtures.DialogueTests(); fixture.setUp()
        try:
            class ChangingClient(fixtures.Client):
                def chat(self, *args, **kwargs):
                    result = super().chat(*args, **kwargs)
                    if len(self.calls)==4:
                        fixture.conn.execute("UPDATE evidence_cards SET reviewed=0 WHERE evidence_id='C1'");fixture.conn.commit()
                    return result
            client=ChangingClient([
                LLMResponse('', '', [fixtures.call('understand_user',fixtures.interpretation('analyze'))],{}),
                LLMResponse('', '', [fixtures.call('method_search',{'query':'调查'})],{}),
                LLMResponse('调查的摘要【C1】。', '', [], {}), LLMResponse('调查的摘要【C1】。', '', [], {})])
            before=knowledge_revision(fixture.db)
            events=list(Agent(fixture.config,client=client).stream('请分析调查',mode='consult'))
            self.assertNotEqual(before,knowledge_revision(fixture.db))
            self.assertFalse(any(e['type']=='content' for e in events))
            self.assertIn('核验资格',next(e['error'] for e in events if e['type']=='error'))
        finally: fixture.tearDown()
