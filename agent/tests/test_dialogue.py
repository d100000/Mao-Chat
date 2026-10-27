"""Consultation behavior: user-correctable understanding and real grounding gates."""
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from agent.db import connect, ensure_schema
from agent.dialogue import CuratedMethodLibrary, answer_format_limits, format_violations, understanding
from agent.firecrawl import FirecrawlConfig
from agent.llm import LLMConfig, LLMResponse
from agent.runtime import Agent, AgentConfig


class Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def chat(self, messages, tools=None, response_format=None):
        self.calls.append({'messages': list(messages), 'tools': tools})
        return next(self.responses)


class StreamingClient(Client):
    def stream_chat(self, messages, tools=None, response_format=None):
        response = self.chat(messages, tools, response_format)
        if response.reasoning:
            yield {'type': 'reasoning', 'text': response.reasoning}
        if response.tool_calls:
            yield {'type': 'tool_call_delta', 'tool_calls': response.tool_calls}
        if response.content:
            midpoint = len(response.content) // 2
            yield {'type': 'content', 'text': response.content[:midpoint]}
            yield {'type': 'content', 'text': response.content[midpoint:]}


def call(name, data):
    return {'id': name + '-1', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(data, ensure_ascii=False)}}


def interpretation(phase='clarify', **changes):
    data = {'phase': phase, 'summary': '用户正面临一个现实选择', 'goal': '', 'known_facts': ['用户希望理清下一步'], 'constraints': [], 'uncertainties': ['希望改变什么尚不明确'], 'next_question': '你最希望先改善哪一件事？'}
    data.update(changes)
    return data


class DialogueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='mao-consult-')
        self.root = Path(self.tmp.name)
        self.db = self.root / 'agent.db'
        self.conn = connect(self.db)
        ensure_schema(self.conn)
        self.conn.execute("INSERT INTO evidence_cards(evidence_id,topic,article_title,summary,excerpt,reviewed,quote_verified,raw_json) VALUES(?,?,?,?,?,?,?,?)", ('C1', '调查研究', '农村调查', '先接触实际材料，再形成认识。', '先做调查', 1, 1, json.dumps({'pdf_pages': [12], 'quotation_scope': '已核短片段'})))
        self.conn.execute("INSERT INTO evidence_cards(evidence_id,topic,article_title,summary,excerpt,reviewed,quote_verified,raw_json) VALUES(?,?,?,?,?,?,?,?)", ('C2', '调查研究', '未审核稿', '不该被检索到的结论', '虚构名言', 0, 1, '{}'))
        self.conn.execute("INSERT INTO evidence_cards(evidence_id,topic,article_title,summary,excerpt,reviewed,quote_verified,raw_json) VALUES(?,?,?,?,?,?,?,?)", ('C3', '调查研究', '调查转述', '可转述的已核摘要', '未核措辞不提供', 1, 0, '{}'))
        self.conn.execute("INSERT INTO philosophy_rules(rule_id,label,decision_rule,conditions,source_locator,verification_status) VALUES(?,?,?,?,?,?)", ('worldview:1', '调查与判断', '先调查再判断', '不把方法变成拒绝回应的借口', '原著位置', 'candidate'))
        self.conn.execute("INSERT INTO events(event_id,title,description,verification_status,metadata_json) VALUES(?,?,?,?,?)", ('event:E1', '调查活动', '公开调查事实', 'reviewed', json.dumps({'public_event_verified': True})))
        self.conn.execute("INSERT INTO events(event_id,title,description,verification_status,metadata_json) VALUES(?,?,?,?,?)", ('event:E2', '调查传闻', '没有公开核验', 'reviewed', '{}'))
        self.conn.commit()
        self.config = AgentConfig(LLMConfig(api_key_env='unused'), FirecrawlConfig(api_key_env='unused'), self.db, self.root/'corpus.db', self.root/'sessions', max_steps=6)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def agent(self, responses):
        self.client = Client(responses)
        return Agent(self.config, client=self.client)

    def test_understanding_is_tentative_bounded_and_missing_facts_stay_unknown(self):
        data = understanding(**interpretation(summary='x' * 1000))
        self.assertTrue(data['tentative'])
        self.assertEqual(data['goal'], '')
        self.assertEqual(data['constraints'], [])
        self.assertLessEqual(len(data['summary']), 600)
        with self.assertRaises(ValueError):
            understanding(**interpretation(constraints='猜测用户资金不足'))

    def test_direction_stage_goal_and_current_request_are_independent(self):
        state = understanding(**interpretation(
            'review', long_term_direction='兼顾健康与收入', stage_goal='六周留住三家客户',
            goal='兼容字段里误放了本周实验', turn_request='请压成三句话', intent='review'))
        self.assertEqual(state['long_term_direction'], '兼顾健康与收入')
        self.assertEqual(state['stage_goal'], '六周留住三家客户')
        self.assertEqual(state['goal'], state['stage_goal'])
        self.assertEqual(state['turn_request'], '请压成三句话')
        self.assertIsNone(state['proposed_action'])
        old = understanding(**interpretation(goal='旧会话里明确的阶段目标'))
        self.assertEqual(old['stage_goal'], '旧会话里明确的阶段目标')
        unknown = understanding(**interpretation(long_term_direction='', stage_goal='', turn_request='先听我说'))
        self.assertEqual(unknown['stage_goal'], '')
        with self.assertRaises(ValueError):
            understanding(**interpretation(stage_goal=['错误格式']))

    def test_action_proposals_cannot_supply_user_acceptance_or_completed_results(self):
        proposal = {'title': '核对一个实际问题', 'steps': ['记录最近一次现象'],
                    'continue_signal': '得到具体事实', 'adjust_signal': '资料仍不足', 'review_hint': '方便时回来聊'}
        state = understanding(**interpretation('analyze', proposed_action=proposal))
        self.assertEqual(state['proposed_action'], proposal)
        self.assertNotIn('status', state['proposed_action'])
        for field, value in [('status', 'accepted'), ('completed_at', '今天'), ('result', '已经有效')]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                understanding(**interpretation(proposed_action={**proposal, field: value}))
        with self.assertRaises(ValueError):
            understanding(**interpretation(proposed_action={'title': '无步骤', 'steps': []}))

    def test_listening_turn_suppresses_action_proposal_and_does_not_force_retrieval(self):
        user = '我现在不想听办法，只想说说最近的事。'
        public = interpretation('analyze', intent='listen', summary='用户明确只想倾诉',
                                stage_goal='', turn_request='只想说说', goal='', next_question='',
                                proposed_action={'title': '不应布置的实验', 'steps': ['做记录']})
        agent = self.agent([
            LLMResponse(tool_calls=[call('understand_user', public)]),
            LLMResponse(content='可以，你可以按自己的节奏说。'),
            LLMResponse(content='可以，先不找办法。你愿意从哪里说起都可以。')])
        result = agent.ask(user, 'consult', network_enabled=False)
        self.assertTrue(result['ok'])
        self.assertEqual(result['analysis']['phase'], 'listen')
        self.assertEqual(result['analysis']['intent'], 'listen')
        self.assertIsNone(result['analysis']['proposed_action'])
        self.assertEqual([tool['name'] for tool in result['tools']], ['understand_user'])
        self.assertNotIn('史料缺口', result['content'])
        self.assertEqual(result['used_evidence_ids'], [])
        self.assertIsNone(self.client.calls[-1]['tools'])

    def test_only_final_cited_sources_are_adopted_and_audited(self):
        agent = self.agent([
            LLMResponse(tool_calls=[call('understand_user', interpretation('analyze')), call('method_search', {'query': '调查研究'})]),
            LLMResponse(content='草稿只提了一条摘要。【C3】'),
            LLMResponse(content='现代建议：先核实实际情况。【evidence:C1：已核出处】')])
        events = list(agent.stream('怎样先弄清眼前情况？', 'consult'))
        metadata = next(event for event in events if event['type'] == 'answer_meta')
        self.assertEqual(metadata['used_evidence_ids'], ['C1'])
        self.assertNotIn('C3', metadata['used_evidence_ids'])
        self.assertGreater(len(Agent._evidence_ids([{'result': event['result']} for event in events if event['type'] == 'tool'])), 1)
        self.assertLess(next(index for index, event in enumerate(events) if event['type'] == 'answer_meta'),
                        next(index for index, event in enumerate(events) if event['type'] == 'done'))

    def test_used_source_aliases_and_page_locators_do_not_promote_linked_sources(self):
        tools = [{'name': 'mixed', 'result': {'results': [
            {'evidence_id': 'C1', 'quote_id': 'quote:C1'},
            {'evidence_id': 'UNCITED'},
            {'event_id': 'event:E1', 'evidence_id': 'LINKED', 'record_type': 'historical_event'},
            {'anchor': '01_selected_works_v1:p0012', 'record_type': 'page'},
        ], 'method_notes': [{'rule_id': 'worldview:1'}]}}]
        text = '【quote:quote:C1】【rule:worldview:1】【event:E1】【LINKED】【FAKE】【01_selected_works_v1:p0012】'
        self.assertEqual(Agent.used_evidence_ids(text, tools), ['01_selected_works_v1:p0012', 'C1', 'event:E1', 'worldview:1'])
        self.assertEqual(Agent._content_evidence_markers('【1937年】【当前理解】'), [])

    def test_transient_web_cache_calls_provider_without_storing_and_resets_after_error(self):
        class Web:
            def __init__(self): self.queries = []
            def search(self, query, limit=None):
                self.queries.append(query)
                return {'query': query, 'results': [{'url': 'https://example.org/test', 'title': '候选', 'markdown': '原文' * 7000}], 'untrusted': True}
        agent = self.agent([])
        agent.firecrawl = Web()
        with self.assertRaises(RuntimeError):
            with agent.transient_web_cache():
                with agent.transient_web_cache():
                    preview = agent._firecrawl_search('临时问题')
                self.assertFalse(preview['cache_persisted'])
                self.assertTrue(preview['results'][0]['content_truncated'])
                self.assertIn('未写入共享网页缓存', preview['raw']['notice'])
                self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM web_sources').fetchone()[0], 0)
                raise RuntimeError('测试上下文异常退出')
        normal = agent._firecrawl_search('普通研究问题')
        self.assertTrue(normal['cache_persisted'])
        self.assertEqual(agent.firecrawl.queries, ['临时问题', '普通研究问题'])
        self.assertEqual([x[0] for x in self.conn.execute('SELECT query FROM web_sources')], ['普通研究问题'])

    def test_transient_web_cache_is_isolated_between_concurrent_requests(self):
        class Web:
            def search(self, query, limit=None):
                return {'query': query, 'results': [{'url': 'https://example.org/' + query, 'markdown': '候选网页'}], 'untrusted': True}
        agent = self.agent([])
        agent.firecrawl = Web()
        barrier = threading.Barrier(2)
        results, failures = {}, []
        def worker(temporary):
            try:
                if temporary:
                    with agent.transient_web_cache():
                        barrier.wait(timeout=3)
                        results['temporary'] = agent._firecrawl_search('temporary')
                else:
                    barrier.wait(timeout=3)
                    results['research'] = agent._firecrawl_search('research')
            except Exception as exc:
                failures.append(exc)
        threads = [threading.Thread(target=worker, args=(flag,)) for flag in (True, False)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=5)
        self.assertEqual(failures, [])
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertFalse(results['temporary']['cache_persisted'])
        self.assertTrue(results['research']['cache_persisted'])
        self.assertEqual([x[0] for x in self.conn.execute('SELECT query FROM web_sources')], ['research'])

    def test_saved_message_audit_preserves_metadata_without_duplicate_transcript(self):
        agent = self.agent([])
        text = '现代建议，依据一条核验卡。【C1】'
        messages = [{'role': 'user', 'content': '帮我核对'}, {'role': 'assistant', 'content': text}]
        metadata = {'mode': 'consult', 'analysis': understanding(**interpretation()),
                    'actions': [{'id': 'chosen', 'status': 'accepted'}], 'remember': True}
        agent.save_session('owned', messages, metadata)
        tools = [{'name': 'method_search', 'result': {'results': [
            {'evidence_id': 'C1', 'source_id': 'v1', 'pdf_page': 12, 'reviewed': True},
            {'evidence_id': 'C3', 'source_id': 'v1', 'pdf_page': 13, 'reviewed': True}]}}]
        for _ in range(2):
            agent._persist_run('帮我核对', 'consult', None, text, tools, .1, session_id='owned', message_saved=True)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM messages WHERE session_id='owned'").fetchone()[0], 2)
        meta = json.loads(self.conn.execute("SELECT metadata_json FROM sessions WHERE session_id='owned'").fetchone()[0])
        self.assertEqual(meta, metadata)
        self.assertEqual([x[0] for x in self.conn.execute('SELECT evidence_id FROM citations')], ['C1'])
        run = json.loads(self.conn.execute('SELECT metadata_json FROM llm_runs ORDER BY run_id DESC LIMIT 1').fetchone()[0])
        self.assertEqual(run['evidence_ids'], ['C1'])
        self.assertEqual(run['retrieved_evidence_ids'], ['C1', 'C3'])
        # A later saved turn retains provenance for exact old answers instead
        # of losing it through the snapshot replacement's FK cascade.
        agent.save_session('owned', messages + [{'role': 'user', 'content': '继续'}, {'role': 'assistant', 'content': '新的回应'}], metadata)
        rows = self.conn.execute('SELECT m.content,c.evidence_id FROM citations c JOIN messages m ON m.message_id=c.message_id').fetchall()
        self.assertEqual([(row[0], row[1]) for row in rows], [(text, 'C1')])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM messages WHERE session_id='owned'").fetchone()[0], 4)

    def test_only_explicit_length_requests_are_extracted_and_quote_punctuation_is_ignored(self):
        self.assertEqual(answer_format_limits('我有350元预算，另外有三句话的会议记录。'), {})
        self.assertEqual(answer_format_limits('请用不超过三百五十字说清楚。'), {'max_chars': 350})
        limits = answer_format_limits('把做法压成三句话，不超过 200 字，句内引用。')
        self.assertEqual(limits, {'max_chars': 200, 'sentences': 3})
        text = '先问“具体担心什么？”并记原话【C1】。对照实际负担。若仍不清楚就暂缓。'
        self.assertEqual(format_violations(text, limits), [])
        self.assertTrue(format_violations(text + '另加一个总结。', limits))
        self.assertTrue(format_violations('只有一句话。', limits))

    def test_explicit_short_reply_gets_one_bounded_format_repair_without_state_changes(self):
        public = interpretation('analyze', stage_goal='六周留住客户', turn_request='三句话且80字内',
                                constraints=['两人团队', '无额外预算'])
        final = '先问客户稳定性的具体表现【C1】。按两人和无额外预算比较负担。缺材料就暂缓承诺。'
        agent = self.agent([
            LLMResponse(tool_calls=[call('understand_user', public), call('method_search', {'query': '调查研究'})]),
            LLMResponse(content='初稿不直接发布。'),
            LLMResponse(content='第一点。第二点。第三点。第四点和长篇附录仍违反要求。'),
            LLMResponse(content=final, reasoning='格式修复过程也不公开'),
        ])
        events = list(agent.stream('请压成三句话，不超过80字。', 'consult'))
        self.assertEqual(''.join(event['text'] for event in events if event['type'] == 'content'), final)
        self.assertEqual([event['analysis'] for event in events if event['type'] == 'analysis'], [understanding(**public)])
        self.assertEqual([event['steps'] for event in events if event['type'] == 'done'], [4])
        self.assertEqual([event['text'] for event in events if event['type'] == 'status'][-2:], ['正在整理建议，并逐段核对引语和出处。', '正在按你要求压缩表达…'])
        self.assertEqual(len(self.client.calls), 4)
        self.assertTrue(all(call['tools'] is None for call in self.client.calls[-2:]))
        self.assertNotIn('格式修复过程', str(events))
        self.assertEqual(next(event['used_evidence_ids'] for event in events if event['type'] == 'answer_meta'), ['C1'])

    def test_format_repair_cannot_loop_or_publish_an_oversized_answer(self):
        for budget, responses in (
            (3, [LLMResponse(content='仍然太长的一段答复。')]),
            (6, [LLMResponse(content='仍然太长的一段答复。'), LLMResponse(content='修复后仍然太长的一段答复。')]),
        ):
            with self.subTest(budget=budget):
                self.config.max_steps = budget
                agent = self.agent([LLMResponse(tool_calls=[call('understand_user', interpretation())]),
                                    LLMResponse(content='需要复核。'), *responses])
                result = agent.ask('不超过5字。', 'consult')
                self.assertFalse(result['ok'])
                self.assertEqual(result['content'], '')
                self.assertIn('未满足本轮字数或句数', result['error'])
                self.assertLessEqual(len(self.client.calls), 4)

    def test_length_budget_never_silently_hides_an_unknown_source_warning(self):
        agent = self.agent([
            LLMResponse(tool_calls=[call('understand_user', interpretation())]),
            LLMResponse(content='简短草稿。'),
            LLMResponse(content='先核实。【FAKE01】'),
        ])
        result = agent.ask('请不超过30字回答。', 'consult')
        self.assertFalse(result['ok'])
        self.assertEqual(result['content'], '')
        self.assertIn('引用提示', result['error'])
        lookup = next(tool for tool in result['tools'] if tool['name'] == 'verify_citations')
        self.assertEqual(lookup['result']['checks'][0]['status'], 'not_found')

    def test_curated_method_retrieval_does_not_promote_notes_or_unverified_quotes(self):
        result = CuratedMethodLibrary(self.db).search('调查研究', limit=6)
        rows = {x.get('evidence_id') or x.get('event_id'): x for x in result['results']}
        self.assertIn('C1', rows)
        self.assertNotIn('C2', rows)
        self.assertTrue(rows['C1']['quote_eligible'])
        self.assertEqual(rows['C1']['excerpt'], '先做调查')
        self.assertFalse(rows['C3']['quote_eligible'])
        self.assertEqual(rows['C3']['excerpt'], '')
        self.assertIn('event:E1', rows)
        self.assertNotIn('event:E2', rows)
        self.assertTrue(result['method_notes'])
        self.assertEqual(result['method_notes'][0]['verification_status'], 'method_note')
        self.assertIn('不是逐字', result['method_notes'][0]['notice'])
        self.assertIn('没有明确已核关系', result['history_gap'])
        self.assertLessEqual(len(CuratedMethodLibrary(self.db).search('调查', 10**9)['results']), 6)
        self.assertLessEqual(len(CuratedMethodLibrary(self.db).search('调查', -1)['results']), 1)

    def test_no_match_and_missing_library_report_a_gap(self):
        result = CuratedMethodLibrary(self.db).search('量子火箭')
        self.assertEqual(result['status'], 'not_found')
        self.assertEqual(result['results'], [])
        missing = CuratedMethodLibrary(self.root/'missing.db').search('调查')
        self.assertEqual(missing['status'], 'not_found')
        self.assertFalse((self.root/'missing.db').exists())

    def test_vague_turn_can_clarify_without_searching_or_hardcoding_profile(self):
        agent = self.agent([
            LLMResponse(tool_calls=[call('understand_user', interpretation())]),
            LLMResponse(content='可以先把眼下最想改变的一件事写下来。你最希望先改善什么？'),
            LLMResponse(content='可以先把眼下最想改变的一件事写下来。你最希望先改善什么？')])
        result = agent.ask('最近不知道下一步该怎么走。', 'consult', network_enabled=False)
        self.assertTrue(result['ok'])
        self.assertEqual(result['analysis']['phase'], 'clarify')
        self.assertEqual(result['analysis']['goal'], '')
        self.assertEqual([x['name'] for x in result['tools']], ['understand_user'])
        self.assertNotIn('史料缺口', result['content'])
        self.assertIn('不重复问已经答过', self.client.calls[0]['messages'][0]['content'])
        self.assertEqual(self.client.calls[0]['messages'][-1]['content'], '最近不知道下一步该怎么走。')

    def test_consult_policy_preserves_user_fact_and_assistant_hypothesis_boundary(self):
        agent = self.agent([])
        history = [{'role': 'assistant', 'content': '也许你希望扩大规模，可以先访谈。'}]
        messages = agent._messages('访谈后，我想先服务好现有客户。', 'consult', None, history)
        policy = messages[0]['content']
        for requirement in (
            '严格区分用户明确事实与模型推测',
            '不得向用户经历添加未提供的时间、数量、主体、行动顺序或动机',
            '用户未提供时间和顺序时，只用“此前”“这次反馈”等不具体的指代',
            '前轮助手提出的假设只能称为“先前理解”或“此前的假设”',
            '不能写成“用户旧目标”',
            '只有用户明确表达过的目标才能这样回顾',
            '除非用户明确确认已经尝试，不得把助手建议写成用户已采纳或已实施的行动',
            '明确标为待核假设并允许用户否定',
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, policy)
        self.assertEqual(messages[1], history[0])
        self.assertEqual(messages[-1]['content'], '访谈后，我想先服务好现有客户。')

    def test_analysis_cannot_skip_understanding_or_method_grounding(self):
        agent = self.agent([
            LLMResponse(content='直接替用户决定。'),
            LLMResponse(tool_calls=[call('understand_user', interpretation('analyze', goal='改善现有流程', uncertainties=[]))]),
            LLMResponse(content='未经检索的史料断言。'),
            LLMResponse(tool_calls=[call('method_search', {'query': '调查研究', 'limit': 4})]),
            LLMResponse(content='依据调查方法先补材料。【C1】现代建议：先访谈三位实际使用者，本周比较反馈，再调整。'),
            LLMResponse(content='依据调查方法先补材料。【C1】现代建议：先访谈三位实际使用者，本周比较反馈，再调整。')])
        events = list(agent.stream('我已明确希望改善现有流程，需要一个可以先尝试的步骤。', 'consult', network_enabled=False))
        answer = ''.join(x['text'] for x in events if x['type'] == 'content')
        self.assertNotIn('直接替用户决定', answer)
        self.assertNotIn('未经检索的史料断言', answer)
        self.assertIn('现代建议', answer)
        self.assertTrue(any(x['type'] == 'analysis' and x['analysis']['phase'] == 'analyze' for x in events))
        self.assertTrue(any(x['type'] == 'tool' and x['name'] == 'method_search' for x in events))
        self.assertTrue([x for x in events if x['type'] == 'content'][0]['evidence_gate']['consult_grounded'])

    def test_user_correction_is_retained_and_review_updates_understanding(self):
        previous = [{'role': 'user', 'content': '我最早想扩大规模。'}, {'role': 'assistant', 'content': '可以先了解实际需求。'}]
        latest = '我更正一下，不想扩大了；我希望先服务好现有客户。'
        corrected = interpretation('review', goal='先服务好现有客户', known_facts=['用户已纠正扩大规模的目标'], uncertainties=[], next_question='')
        agent = self.agent([
            LLMResponse(tool_calls=[call('understand_user', corrected), call('method_search', {'query': '调查研究'})]),
            LLMResponse(content='按你更新后的目标，先访问现有客户。【C1】现代建议：记录一个最常见问题，一周后复盘。'),
            LLMResponse(content='按你更新后的目标，先访问现有客户。【C1】现代建议：记录一个最常见问题，一周后复盘。')])
        result = agent.ask(latest, 'consult', history=previous, network_enabled=False)
        self.assertTrue(result['ok'])
        self.assertEqual(result['analysis']['goal'], '先服务好现有客户')
        self.assertEqual(result['analysis']['phase'], 'review')
        first = self.client.calls[0]['messages']
        self.assertEqual(first[1:3], previous)
        self.assertEqual(first[-1]['content'], latest)
        self.assertIn('以最新原话更新理解', first[0]['content'])

    def test_missing_evidence_cannot_be_published_as_grounded_history(self):
        agent = self.agent([
            LLMResponse(tool_calls=[call('understand_user', interpretation('analyze')), call('method_search', {'query': '量子火箭'})]),
            LLMResponse(content='现代建议：先列清需要核实的现实条件。'),
            LLMResponse(content='现代建议：先列清需要核实的现实条件。')])
        result = agent.ask('量子火箭的选择', 'consult')
        self.assertTrue(result['ok'])
        self.assertTrue(result['content'].startswith('史料缺口'))
        self.assertFalse(result['evidence_gate']['consult_grounded'])

    def test_final_review_withholds_bad_draft_and_preserves_public_understanding(self):
        public = interpretation('analyze', summary='用户需要在有限条件下安排工作',
                                known_facts=['用户有三家客户', '有两位提出催促的人'],
                                uncertainties=['催促者与客户是否重合未知', '方向的具体内容未知'])
        draft = '你上周遇到三家客户加两位催促者，因此有四个方向，应筛掉两个。'
        final = '催促者是否来自现有客户还不清楚，暂不合并计数。现代建议：先核实每项诉求和提出者，再比较。【C1】'
        agent = self.agent([
            LLMResponse(tool_calls=[call('understand_user', public), call('method_search', {'query': '调查研究'})]),
            LLMResponse(content=draft),
            LLMResponse(content=final, reasoning='这里是复核时的私有审稿过程',
                        raw={'choices': [{'message': {'reasoning_content': '这里是复核时的私有审稿过程'}}]}),
        ])
        history = [{'role': 'assistant', 'content': '我先前猜测你想扩大规模。'}]
        events = list(agent.stream('目前有三家客户，另有两位在催我。', 'consult', history=history, show_reasoning=True))
        self.assertEqual(''.join(x['text'] for x in events if x['type'] == 'content'), final)
        self.assertNotIn(draft, ''.join(x.get('text', '') for x in events))
        analyses = [x['analysis'] for x in events if x['type'] == 'analysis']
        self.assertEqual(analyses, [understanding(**public)])
        self.assertFalse(any(x['type'] == 'reasoning' for x in events))
        self.assertTrue(all(not x.get('reasoning') for x in events if x['type'] == 'content'))
        self.assertNotIn('私有审稿过程', str(events))
        self.assertEqual(len(self.client.calls), 3)
        self.assertIsNone(self.client.calls[-1]['tools'])
        review_messages = self.client.calls[-1]['messages']
        self.assertEqual(review_messages[-2], {'role': 'assistant', 'content': draft})
        self.assertEqual(review_messages[-1]['role'], 'system')
        review_instruction = review_messages[-1]['content']
        for rule in ('唯一一次最终答复复核', 'user 角色原话', '均不算用户事实',
                     'uncertainties 必须保持未知', '确定断言必须删除或改为条件分支',
                     '实体数不等于方向或选项数', '不能推出必须筛除多少项',
                     '本轮工具已经返回的出处', '不可在此次调用中更改'):
            with self.subTest(rule=rule):
                self.assertIn(rule, review_instruction)
        self.assertTrue(review_instruction.endswith(json.dumps(analyses[0], ensure_ascii=False)))
        self.assertEqual([x['stage'] for x in events if x['type'] == 'status'], ['understand','local','relate','review'])
        self.assertEqual([x['steps'] for x in events if x['type'] == 'done'], [3])

    def test_streamed_clarification_draft_is_reviewed_and_final_citations_still_gated(self):
        client = StreamingClient([
            LLMResponse(tool_calls=[call('understand_user', interpretation())]),
            LLMResponse(content='你上周已经辞职，所以应当立即创业。'),
            LLMResponse(content='现代建议：先写下最想改变的一件事。【FAKE01：虚构出处】', reasoning='复核过程不展示'),
        ])
        agent = Agent(self.config, client=client)
        events = list(agent.stream('我不确定接下来该怎么办。', 'consult', show_reasoning=True))
        answer = ''.join(x['text'] for x in events if x['type'] == 'content')
        self.assertNotIn('已经辞职', answer)
        self.assertNotIn('复核过程', str(events))
        gate = next(x['evidence_gate'] for x in events if x['type'] == 'content')
        self.assertEqual(gate['unknown_markers'], ['FAKE01'])
        self.assertEqual(gate['consult_phase'], 'clarify')
        self.assertIn('证据门提示', answer)
        self.assertIsNone(client.calls[-1]['tools'])
        self.assertEqual([x['name'] for x in events if x['type'] == 'tool'], ['understand_user', 'verify_citations'])

    def test_final_review_budget_exhaustion_never_publishes_an_unreviewed_draft(self):
        self.config.max_steps = 2
        agent = self.agent([
            LLMResponse(tool_calls=[call('understand_user', interpretation())]),
            LLMResponse(content='这是一份尚未复核的草稿。'),
            LLMResponse(content='预算之外的内容不得被请求。'),
        ])
        result = agent.ask('帮我理清下一步。', 'consult')
        self.assertFalse(result['ok'])
        self.assertEqual(result['content'], '')
        self.assertIn('未能完成最终复核', result['error'])
        self.assertEqual(len(self.client.calls), self.config.max_steps)

    def test_final_review_cannot_call_tools_mutate_analysis_or_retry(self):
        public = interpretation()
        blocked_responses = (
            LLMResponse(content='不得展示的内容', tool_calls=[call('understand_user', interpretation(goal='模型擅自修改目标'))]),
            LLMResponse(content=''),
        )
        for blocked in blocked_responses:
            with self.subTest(tool_calls=bool(blocked.tool_calls)):
                agent = self.agent([
                    LLMResponse(tool_calls=[call('understand_user', public)]),
                    LLMResponse(content='尚未通过复核的草稿。'),
                    blocked,
                    LLMResponse(content='不能无限重试到这一步。'),
                ])
                result = agent.ask('帮我理清下一步。', 'consult')
                self.assertFalse(result['ok'])
                self.assertEqual(result['content'], '')
                self.assertEqual(result['analysis'], understanding(**public))
                self.assertEqual([x['name'] for x in result['tools']], ['understand_user'])
                self.assertEqual(len(self.client.calls), 3)
                self.assertIsNone(self.client.calls[-1]['tools'])

    def test_existing_modes_do_not_add_a_final_review_call(self):
        for mode in ('research', 'modern', 'historical'):
            with self.subTest(mode=mode):
                agent = self.agent([LLMResponse(content='既有模式直接返回的答复。【C1】')])
                agent.method_library.verify_citations = lambda *args, **kwargs: self.fail('旧模式不得自动追溯引用')
                result = agent.ask('分析原文。', mode, as_of='1937-01-01' if mode == 'historical' else None)
                self.assertTrue(result['ok'])
                self.assertTrue(result['content'].startswith('既有模式直接返回的答复。'))
                self.assertEqual(result['evidence_gate']['unknown_markers'], ['C1'])
                self.assertEqual(len(self.client.calls), 1)
                self.assertIsNotNone(self.client.calls[0]['tools'])

    def test_exact_citation_lookup_reuses_method_search_qualification_and_fields(self):
        library = CuratedMethodLibrary(self.db)
        searched = library.search('调查研究', 6)
        traced = library.verify_citations(['evidence:C1', 'C3', 'rule:worldview:1', 'event:event:E1'])
        originals = {x.get('evidence_id') or x.get('event_id'): x for x in searched['results']}
        for row in traced['results']:
            original = originals[row.get('evidence_id') or row.get('event_id')]
            self.assertEqual({k: v for k, v in row.items() if k != 'retrieval_score'},
                             {k: v for k, v in original.items() if k != 'retrieval_score'})
        self.assertEqual(traced['method_notes'][0]['rule_id'], 'worldview:1')
        self.assertEqual(traced['method_notes'][0]['verification_status'], 'method_note')
        self.assertFalse(traced['method_notes'][0]['quote_eligible'])
        self.assertEqual([x['status'] for x in traced['checks']], ['matched'] * 4)
        self.assertEqual(next(x for x in traced['results'] if x.get('evidence_id') == 'C3')['excerpt'], '')

    def test_old_citation_is_exactly_resolved_in_current_turn_without_another_model_call(self):
        self.conn.execute("INSERT INTO sources(source_id,title) VALUES('v1','卷一')")
        self.conn.execute("UPDATE evidence_cards SET source_id='v1' WHERE evidence_id='C1'")
        self.conn.commit()
        self.config.max_steps = 3
        agent = self.agent([
            LLMResponse(tool_calls=[call('understand_user', interpretation('review')), call('method_search', {'query': '量子火箭'})]),
            LLMResponse(content='依据此前的调查方法继续核实。'),
            LLMResponse(content='现代建议：先把尚未确认的诉求列成访谈提纲。【evidence:C1：农村调查，PDF12】'),
        ])
        result = agent.ask('我试着访谈了，接下来如何整理？', 'consult',
                           history=[{'role': 'assistant', 'content': '此前提过调查方法。【C1】'}])
        self.assertTrue(result['ok'])
        self.assertEqual(result['evidence_gate']['unknown_markers'], [])
        self.assertTrue(result['evidence_gate']['consult_grounded'])
        self.assertTrue(result['evidence_gate']['consult_quote_eligible'])
        self.assertNotIn('史料缺口', result['content'])
        lookup = next(x for x in result['tools'] if x['name'] == 'verify_citations')['result']
        self.assertEqual(lookup['requested_markers'], ['evidence:C1'])
        self.assertEqual(lookup['results'][0]['evidence_id'], 'C1')
        self.assertEqual(lookup['results'][0]['anchors'], ['v1:p0012'])
        self.assertEqual(lookup['checks'][0]['status'], 'matched')
        citations = Agent._citation_records(result['tools'])
        self.assertTrue(any(x['evidence_id'] == 'C1' and x['pdf_page'] == 12 for x in citations))
        self.assertEqual(len(self.client.calls), 3)

    def test_fake_unreviewed_and_wrong_namespace_citations_remain_unknown(self):
        markers = ['C1', 'C2', 'FAKE01', 'event:event:E2', 'rule:C1', 'evidence:worldview:1']
        library = CuratedMethodLibrary(self.db)
        lookup = library.verify_citations(markers)
        self.assertEqual([x['evidence_id'] for x in lookup['results']], ['C1'])
        self.assertEqual([x['status'] for x in lookup['checks']], ['matched'] + ['not_found'] * 5)
        text = ''.join('【' + marker + '：说明】' for marker in markers)
        _, gate = Agent._apply_evidence_gate(text, [{'name': 'verify_citations', 'result': lookup}], 'consult', None)
        self.assertEqual(gate['unknown_markers'], sorted(markers[1:]))
        self.assertTrue(gate['modified'])
        self.conn.execute("INSERT INTO evidence_cards(evidence_id,reviewed) VALUES('evidence:SHADOW',0)")
        self.conn.execute("INSERT INTO evidence_cards(evidence_id,reviewed) VALUES('SHADOW',1)")
        self.conn.commit()
        self.assertEqual(library.verify_citations(['evidence:SHADOW'])['results'], [])

    def test_citation_lookup_is_bounded_to_six_ids_and_leaves_the_rest_unknown(self):
        for index in range(1, 8):
            self.conn.execute("INSERT INTO evidence_cards(evidence_id,summary,reviewed,quote_verified) VALUES(?,?,?,?)",
                              ('R' + str(index), '已核摘要', 1, 0))
        self.conn.commit()
        markers = ['R' + str(index) for index in range(1, 8)]
        direct = CuratedMethodLibrary(self.db).verify_citations(markers, limit=10**9)
        self.assertEqual(direct['requested_markers'], markers[:6])
        self.assertEqual(len(direct['results']), 6)
        text = ''.join('【' + marker + '】' for marker in markers)
        agent = self.agent([
            LLMResponse(tool_calls=[call('understand_user', interpretation())]),
            LLMResponse(content='待复核草稿。'),
            LLMResponse(content=text),
        ])
        result = agent.ask('帮我理解一下。', 'consult')
        self.assertTrue(result['ok'])
        self.assertEqual(result['evidence_gate']['unknown_markers'], ['R7'])
        self.assertEqual(len([x for x in result['tools'] if x['name'] == 'verify_citations']), 1)
        self.assertEqual(len(self.client.calls), 3)

    def test_exact_notes_and_public_events_do_not_grant_author_or_quote_qualification(self):
        self.conn.execute("INSERT INTO events(event_id,evidence_id,title,description,verification_status,metadata_json) VALUES(?,?,?,?,?,?)",
                          ('TL01', 'C2', '已核公开事件', '只有事件事实', 'reviewed', json.dumps({'public_event_verified': True})))
        self.conn.commit()
        agent = self.agent([
            LLMResponse(tool_calls=[call('understand_user', interpretation('analyze')), call('method_search', {'query': '量子火箭'})]),
            LLMResponse(content='待复核草稿。'),
            LLMResponse(content='研究者的方法概括【rule:worldview:1】；独立事件背景【event:TL01】；未核作者卡【C2】。'),
        ])
        result = agent.ask('如何看待这个选择？', 'consult')
        self.assertTrue(result['ok'])
        self.assertEqual(result['evidence_gate']['unknown_markers'], ['C2'])
        self.assertFalse(result['evidence_gate']['consult_grounded'])
        self.assertFalse(result['evidence_gate']['consult_quote_eligible'])
        self.assertTrue(result['evidence_gate']['consult_event_context'])
        lookup = next(x for x in result['tools'] if x['name'] == 'verify_citations')['result']
        self.assertEqual(lookup['results'][0]['event_id'], 'TL01')
        self.assertFalse(lookup['results'][0]['quote_eligible'])
        self.assertEqual(lookup['method_notes'][0]['verification_status'], 'method_note')
        self.assertFalse(lookup['method_notes'][0]['quote_eligible'])
        self.assertIn('TL01', Agent._evidence_ids(result['tools']))
        self.assertNotIn('C2', Agent._evidence_ids(result['tools']))
        self.assertTrue(any(x['evidence_id'] == 'TL01' for x in Agent._citation_records(result['tools'])))
        self.assertFalse(any(x['evidence_id'] == 'C2' for x in Agent._citation_records(result['tools'])))

    def test_unavailable_citation_library_reports_gap_without_creating_database(self):
        missing_path = self.root / 'absent.db'
        result = CuratedMethodLibrary(missing_path).verify_citations(['C1'])
        self.assertEqual(result['status'], 'not_found')
        self.assertEqual(result['results'], [])
        self.assertEqual(result['checks'][0]['status'], 'not_found')
        self.assertFalse(missing_path.exists())

    def test_method_notes_are_citable_summaries_and_all_page_locators_are_audited(self):
        tools = [{'name': 'method_search', 'result': {
            'results': [{'record_type': 'evidence_card', 'evidence_id': 'C1', 'source_id': 'v1', 'reviewed': True,
                         'anchors': ['v1:p0012', 'v1:p0013'], 'pdf_pages': [12, 13]}],
            'method_notes': [{'rule_id': 'worldview:1', 'source_locator': '卷1 PDF12–13', 'verification_status': 'method_note'}]}}]
        content, gate = Agent._apply_evidence_gate('研究者方法概括【worldview:1】，原著摘要【C1】。', tools, 'consult', None)
        self.assertFalse(gate['unknown_markers'])
        self.assertIn('worldview:1', Agent._evidence_ids(tools))
        citations = Agent._citation_records(tools)
        self.assertEqual([x['pdf_page'] for x in citations if x['evidence_id'] == 'C1'], [12, 13])
        note = next(x for x in citations if x['evidence_id'] == 'worldview:1')
        self.assertEqual(note['verification_status'], 'candidate')
        self.assertEqual(note['citation_text'], '卷1 PDF12–13')

    def test_compound_citation_ids_receive_the_same_audit_as_bare_ids(self):
        tools = [{'name': 'method_search', 'result': {
            'results': [{'evidence_id': 'VE08', 'reviewed': True}],
            'method_notes': [{'rule_id': 'worldview:11', 'verification_status': 'method_note'}],
        }}]
        cases = (
            ('【VE08：《篇名》，1937年，v1:p0012】', []),
            ('【VE08, title, 1937, p12】', []),
            ('【VE08｜篇名，日期，PDF12】', []),
            ('【FAKE01｜不存在的篇名，PDF12】', ['FAKE01']),
            ('【VE08转述，篇名】', []),
            ('【worldview:99｜虚构方法概括】', ['worldview:99']),
            ('【FAKE01：《不存在原著》，1937年，v1:p0012】', ['FAKE01']),
            ('【FAKE01:《不存在原著》，1937年】', ['FAKE01']),
            ('【worldview:11：方法概括，原著位置】', []),
            ('【worldview:99：虚构方法概括】', ['worldview:99']),
            ('【rule:worldview:11，方法概括】', []),
            ('【rule:worldview:99，虚构方法概括】', ['rule:worldview:99']),
            ('【FAKE01】', ['FAKE01']),
            ('【当前理解：用户希望先理清问题】【现代建议，待用户验证】', []),
        )
        for original, unknown in cases:
            with self.subTest(content=original):
                content, gate = Agent._apply_evidence_gate(original, tools, 'consult', None)
                self.assertEqual(gate['unknown_markers'], unknown)
                self.assertEqual(gate['modified'], bool(unknown))
                self.assertEqual('证据门提示' in content, bool(unknown))

    def test_historical_event_alone_does_not_grant_quote_or_author_grounding(self):
        agent = self.agent([
            LLMResponse(tool_calls=[call('understand_user', interpretation('analyze')), call('method_search', {'query': '公开活动'})]),
            LLMResponse(content='现代建议：先确认现实条件。'),
            LLMResponse(content='现代建议：先确认现实条件。')])
        result = agent.ask('希望考虑一个活动方案', 'consult')
        self.assertTrue(result['evidence_gate']['consult_event_context'])
        self.assertFalse(result['evidence_gate']['consult_grounded'])
        self.assertFalse(result['evidence_gate']['consult_quote_eligible'])
        self.assertTrue(result['content'].startswith('史料缺口'))

    def test_historical_and_research_tools_do_not_expose_consultation_bypass(self):
        agent = self.agent([])
        for mode in ('research', 'historical', 'modern'):
            names = {t.name for t in agent.tools(mode, network_enabled=False)}
            self.assertNotIn('method_search', names)
            self.assertNotIn('understand_user', names)
        names = {t.name for t in agent.tools('consult', network_enabled=False)}
        self.assertTrue({'method_search', 'understand_user'}.issubset(names))
        self.assertNotIn('firecrawl_search', names)


if __name__ == '__main__':
    unittest.main()
