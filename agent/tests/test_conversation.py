"""Actual final-review streaming boundaries; no clocks or network required."""
import unittest
from agent.tests import test_dialogue as fixture
from agent.runtime import Agent
from agent.llm import LLMResponse
from agent.conversation import minimized_query, private_query_terms


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixture.DialogueTests();self.fx.setUp();self.addCleanup(self.fx.tearDown)

    def stream(self, parts, question='先听我说，不用急着建议。', phase='listen'):
        test=self
        class Client:
            calls=0
            finished=False
            def stream_chat(self,messages,tools=None):
                self.calls+=1
                if self.calls==1:
                    yield {'type':'tool_call_delta','tool_calls':[fixture.call('understand_user',fixture.interpretation(phase))]}
                elif self.calls==2:
                    yield {'type':'content','text':'私有草稿不应出现。'}
                else:
                    for part in parts:yield {'type':'content','text':part}
                    self.finished=True
        self.client=Client()
        return Agent(self.fx.config,client=self.client).stream(question,'consult',show_reasoning=False)

    def test_first_verified_paragraph_precedes_provider_completion(self):
        events=self.stream(['可以先把难处说出来。\n\n','你最在意的是什么？'])
        first=next(x for x in events if x['type']=='content')
        self.assertFalse(self.client.finished)
        self.assertEqual(first['text'],'可以先把难处说出来。\n\n')
        self.assertEqual(first['delivery_mode'],'verified_paragraph')
        rest=list(events)
        self.assertTrue(any(x['type']=='done' for x in rest))
        self.assertEqual(self.client.calls,3)
        self.assertNotIn('私有草稿',str(first)+str(rest))

    def test_late_invalid_quote_never_published_or_repaired_over_prefix(self):
        events=list(self.stream(['可以先说说困难。\n\n','毛泽东说：“这是一句虚构名言。”\n\n']))
        self.assertEqual(''.join(x['text'] for x in events if x['type']=='content'),'可以先说说困难。\n\n')
        self.assertTrue(any(x['type']=='error' for x in events))
        self.assertFalse(any(x['type']=='done' for x in events))
        self.assertEqual(self.client.calls,3)

    def test_known_card_is_resolved_at_paragraph_boundary(self):
        events=self.stream(['毛泽东说：“先做调查”【C1】。\n\n','这是已核短片段。'])
        seen=[]
        for event in events:
            seen.append(event)
            if event['type']=='content':break
        self.assertFalse(self.client.finished)
        self.assertTrue(any(x.get('name')=='verify_citations' for x in seen))
        self.assertIn('C1',seen[-1]['text'])
        self.assertTrue(any(x['type']=='done' for x in events))

    def test_length_constraints_buffer_until_complete(self):
        events=self.stream(['先谈困难。\n\n','再说愿望。'],question='请用两句话，不超过80字，先听我说。')
        first=next(x for x in events if x['type']=='content')
        self.assertTrue(self.client.finished)
        self.assertEqual(first['delivery_mode'],'buffered_review')

    def test_open_quote_is_not_a_publishable_paragraph(self):
        events=list(self.stream(['毛泽东说：“这句话还未完成\n\n','而且没有出处。”']))
        self.assertFalse(any(x['type']=='content' for x in events))

    def test_oversized_quote_cannot_bypass_checks(self):
        events=list(self.stream(['毛泽东说：“'+'虚'*801+'”。\n\n']))
        self.assertFalse(any(x['type']=='content' for x in events))

    def test_single_quote_spanning_paragraphs_is_not_published(self):
        events=list(self.stream(["毛泽东说：'没有出处的\n\n", "杜撰句子'。" ]))
        self.assertFalse(any(x['type']=='content' for x in events))

    def test_heading_alone_does_not_count_as_first_body(self):
        events=self.stream(['## 本轮理解\n\n','可以先说说困难。\n\n','再聊下一步。'])
        first=next(x for x in events if x['type']=='content')
        self.assertIn('可以先说说困难。',first['text'])
        self.assertFalse(self.client.finished)
        self.assertTrue(any(x['type']=='done' for x in events))

    def test_library_update_after_first_paragraph_stops_later_publication(self):
        events=self.stream(['先说说困难。\n\n','再考虑下一步。\n\n'])
        next(x for x in events if x['type']=='content')
        self.fx.conn.execute("UPDATE evidence_cards SET reviewed=0 WHERE evidence_id='C1'");self.fx.conn.commit()
        rest=list(events)
        self.assertTrue(any(x['type']=='error' for x in rest));self.assertFalse(any(x['type']=='content' for x in rest))

    def test_single_provider_chunk_does_not_count_as_progressive_generation(self):
        events=list(self.stream(['先说说困难。\n\n然后再聊下一步。']))
        self.assertEqual(next(x['delivery_mode'] for x in events if x['type']=='done'),'buffered_review')

    def test_disabled_stream_respects_buffered_provider_configuration(self):
        self.fx.config.llm.stream=False
        client=fixture.Client([LLMResponse(tool_calls=[fixture.call('understand_user',fixture.interpretation('listen'))]),
                               LLMResponse(content='私有初稿'),LLMResponse(content='可以先说说。\n\n我会倾听。')])
        client.stream_chat=lambda *a,**kw:self.fail('streaming disabled')
        events=list(Agent(self.fx.config,client=client).stream('只想说说。','consult'))
        self.assertEqual(next(x['delivery_mode'] for x in events if x['type']=='done'),'buffered_review')
        self.assertNotIn('私有初稿',str(events))

    def test_listen_has_no_retrieval_stages(self):
        events=list(self.stream(['先听你讲。']))
        self.assertFalse(any(x.get('stage') in {'web','local'} for x in events))
        self.assertTrue(any(x.get('stage')=='review' for x in events))

    def test_queries_do_not_send_identifiers_or_chat(self):
        for query in ['我叫张三，手机号13800138000', 'abc@example.com 工作', '我的客户目前遇到难处', '话'*121, 'https://example.com/?token=private', '地址\n公开资料']:
            self.assertIsNone(minimized_query(query))
        self.assertEqual(minimized_query('2026年公开行业数据'),'2026年公开行业数据')
        names=private_query_terms(['我叫张三，在某地工作。'])
        self.assertIsNone(minimized_query('张三 行业求职',names))
        self.assertEqual(minimized_query('毛泽东 实践论',names),'毛泽东 实践论')

    def test_privacy_guard_precedes_search_dispatch(self):
        agent=Agent(self.fx.config,client=fixture.Client([]))
        agent.firecrawl.search=lambda *a,**kw:self.fail('must not dispatch')
        result=agent._firecrawl_search('我叫张三 手机号13800138000')
        self.assertTrue(result['blocked']);self.assertNotIn('张三',str(result))

class AutomaticSearchHTTPTests(unittest.TestCase):
    def setUp(self):
        from agent.tests import test_credits_http as http
        self.http=http
        self.fx=http.CreditsHTTPTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.fx.bootstrap()
        self.agent=http.SearchWireHTTPAgent(self.fx.server.agent.config)
        self.fx.server.agent=self.agent;self.fx.server.migration.agent=self.agent

    def test_automatic_capability_ignores_legacy_off_switch(self):
        self.fx.server.search_cash_bound='0.005'
        payload,quote=self.fx.quoted(search=False)
        status,events,_=self.fx.post('/api/agent/stream',{**payload,'quote_id':quote['quote_id']})
        self.assertEqual(status,200);self.assertEqual(self.agent.searches,1)
        self.assertTrue(any(x['type']=='done' for x in events))

    def test_missing_credential_degrades_without_blocking_generation(self):
        from unittest.mock import patch
        self.fx.server.search_cash_bound='0.005'
        with patch.object(self.agent.firecrawl.config,'api_key',side_effect=ValueError('no key')):
            payload,quote=self.fx.quoted(search=True)
            status,events,_=self.fx.post('/api/agent/stream',{**payload,'quote_id':quote['quote_id']})
        self.assertEqual(status,200);self.assertEqual(self.agent.searches,0)
        self.assertTrue(any(x['type']=='done' for x in events))
        self.assertTrue(any(x.get('stage')=='scope' for x in events))

    def test_capability_changed_after_quote_rejects_before_any_provider(self):
        payload,quote=self.fx.quoted()
        self.fx.server.search_cash_bound='0.005'
        status,result,_=self.fx.post('/api/agent/stream',{**payload,'quote_id':quote['quote_id']})
        self.assertEqual(status,422);self.assertEqual(result['code'],'quote_invalid')
        self.assertEqual(self.agent.executions,0);self.assertEqual(self.agent.searches,0)

    def test_status_latency_is_separate_from_body_latency(self):
        payload,quote=self.fx.quoted()
        self.fx.post('/api/agent/stream',{**payload,'quote_id':quote['quote_id']})
        metrics=self.fx.server.quality.aggregate()
        self.assertEqual(metrics['first_status']['samples'],1)
        self.assertEqual(metrics['first_helpful_content']['samples'],1)
        self.assertLessEqual(metrics['first_status']['p50_ms'],metrics['first_helpful_content']['p50_ms'])
        self.assertEqual(metrics['delivery_modes'],{'buffered_review':1})


if __name__ == '__main__':
    unittest.main()
