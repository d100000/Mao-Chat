import json
from pathlib import Path
import tempfile
import unittest

from agent.claims import check_answer_claims
from agent.dialogue import understanding, UNDERSTANDING_PARAMETERS
from agent.firecrawl import FirecrawlConfig
from agent.llm import LLMConfig, LLMResponse, LLMError
from agent.runtime import Agent, AgentConfig
from agent.db import connect
from agent.telemetry import telemetry_scope, record_model_call


def source(**extra):
    return {'evidence_id': 'C1', 'record_type': 'evidence_card', 'source_role': 'author_work',
            'reviewed': True, 'quote_verified': True, 'quote_eligible': True,
            'verification_status': 'reviewed', 'excerpt': '认识依赖实践。判断应当接受现实检验。',
            'earliest_wording_date': '1950-12-29', 'known_to_character_from': '1937-07-31', **extra}


def tools(row):
    return [{'name': 'method_search', 'result': {'results': [row], 'untrusted': False}}]


class ClaimChecksTests(unittest.TestCase):
    def test_exact_continuous_excerpt_passes_but_known_id_does_not_authorize_changed_words(self):
        checked = check_answer_claims('毛泽东写道：“认识依赖实践。”【C1】', tools(source()))
        self.assertTrue(checked['passed'])
        self.assertEqual(checked['quote_checks'][0]['status'], 'matched_excerpt')
        forged = check_answer_claims('毛泽东说：“努力就必然成功。”【C1】', tools(source()))
        self.assertFalse(forged['passed'])
        self.assertEqual(forged['quote_checks'][0]['status'], 'text_mismatch')
        for quoted in ['‘努力就必然成功。’', "'努力就必然成功。'"]:
            self.assertFalse(check_answer_claims('毛泽东说：' + quoted + '【C1】', tools(source()))['passed'])
        self.assertIn('转述', checked['coverage'])
        self.assertEqual(checked['claims'][0]['status'], 'source_found_needs_review')

    def test_user_supplied_words_do_not_authorize_an_attached_author_source(self):
        context=['请核对“伪造的内容”；我的目标是整理文档。']
        self.assertFalse(check_answer_claims('“伪造的内容”【C1】',tools(source()),user_texts=context)['passed'])
        self.assertTrue(check_answer_claims('用户原话：“我的目标是整理文档”。',tools(source()),user_texts=context)['passed'])
        self.assertTrue(check_answer_claims('“认识依赖实践。”【C1】',tools(source()),user_texts=['认识依赖实践。'])['passed'])

    def test_summary_method_and_biographical_records_never_grant_quote_qualification(self):
        text = '原文写道：“认识依赖实践。”【C1】'
        for row in [source(quote_verified=False, quote_eligible=False),
                    source(record_type='principle_note'), source(source_role='biographical_record'),
                    source(verification_status='method_note')]:
            with self.subTest(row=row):
                self.assertFalse(check_answer_claims(text, tools(row))['passed'])
        web = [{'result': {'untrusted': True, 'results': [source()]}}]
        self.assertFalse(check_answer_claims(text, web)['passed'])

    def test_historical_wording_and_knowledge_dates_are_checked_independently(self):
        text = '毛泽东说：“认识依赖实践。”【C1】'
        self.assertFalse(check_answer_claims(text, tools(source()), 'historical', '1938-06-30')['passed'])
        self.assertTrue(check_answer_claims(text, tools(source()), 'historical', '1951-01-01')['passed'])
        self.assertFalse(check_answer_claims(text, tools(source(known_to_character_from='1952-01-01')), 'historical', '1951-01-01')['passed'])
        self.assertFalse(check_answer_claims(text, tools(source(earliest_wording_date=None)), 'historical', '1951-01-01')['passed'])

    def test_layout_whitespace_is_tolerated_without_merging_english_words(self):
        self.assertTrue(check_answer_claims('原文：“认识依赖实践。”【C1】', tools(source(excerpt='认识依赖\n实践。')))['passed'])
        self.assertFalse(check_answer_claims('原文：“Aplan is necessary.”【C1】', tools(source(excerpt='A plan is necessary.')))['passed'])
        self.assertFalse(check_answer_claims('毛主席：“天道酬勤”【C1】', tools(source()))['passed'])
        self.assertFalse(check_answer_claims('“天道酬勤”【C1】', tools(source()))['passed'])

    def test_user_words_modern_questions_and_explicitly_disavowed_misattributions_are_not_author_quotes(self):
        examples = ['你说“我很累，不想再扛了”，我们先听你讲。【C1】',
                    '现代建议：可以问客户：“您说的稳定性具体指什么？”【C1】',
                    '网上误传毛泽东说：“努力就必然成功。”这不是原话。',
                    '把“稳定性”具体化，再决定下一步。【C1】',
                    '“实践”【C1】这个概念需要结合条件理解。']
        for text in examples:
            with self.subTest(text=text):
                result = check_answer_claims(text, tools(source(quote_eligible=False, quote_verified=False)))
                self.assertTrue(result['passed'])
                self.assertEqual(result['quote_checks'], [])

    def test_paraphrase_and_modern_inference_are_reviewable_not_asserted_true(self):
        checked = check_answer_claims('现代建议：本周先核对现状。【C1】\n当前判断还需要核对。【MISSING】', tools(source()))
        self.assertTrue(checked['passed'])
        self.assertEqual(checked['claims'][0]['kind'], 'modern_inference')
        self.assertEqual(checked['claims'][1]['status'], 'source_missing')
        self.assertEqual(len(checked['claims'][0]['source_fingerprints']['C1']), 64)
        self.assertEqual(check_answer_claims('毛泽东原话是：“努力就必然成功。”', [])['passed'], False)

    def test_quoted_modern_interpretation_is_not_promoted_to_author_wording(self):
        note = {'principle_id': 'principle:practice-and-revision', 'record_type': 'principle_note',
                'verification_status': 'principle_note', 'quote_eligible': False,
                'research_interpretation': '这是一项待检验的现代建议'}
        results = [{'result': {'principles': [note]}}]
        checked = check_answer_claims('先把“什么情况需要改变判断”【principle:practice-and-revision】说清楚，再决定是否尝试。', results)
        self.assertTrue(checked['passed'])
        self.assertEqual(checked['quote_checks'], [])
        self.assertEqual(checked['claims'][0]['status'], 'source_found_needs_review')
        # The same interpretation cannot authorize a quotation attributed to
        # Mao, and an invented interpretation ID gains no qualification.
        self.assertFalse(check_answer_claims('毛泽东说：“什么情况需要改变判断”【principle:practice-and-revision】', results)['passed'])
        self.assertFalse(check_answer_claims('“什么情况需要改变判断”【principle:invented】', results)['passed'])

    def test_captured_user_goal_and_budget_quotes_are_not_historical_attributions(self):
        question = '当前阶段目标是两周内整理项目文档，没有额外预算。请只根据我说的情况分析。'
        answer = '原话：“当前阶段目标是两周内整理项目文档”、“没有额外预算”；不推断长期方向。'
        checked = check_answer_claims(answer, [], user_texts=[question])
        self.assertTrue(checked['passed'])
        self.assertEqual(checked['quote_checks'], [])
        # Generic "原话" stays conservative when there is no actual user
        # utterance to establish what the quotation is referring to.
        self.assertFalse(check_answer_claims(answer, [])['passed'])

        class Client:
            def chat(self, messages, **kwargs): return LLMResponse(content=answer)
        class Knowledge:
            def search(self, *args, **kwargs): return {'results': []}
            def trace_quote(self, **kwargs): return {'results': []}
            def trace_event(self, **kwargs): return {'results': []}
            def store_web_sources(self, result): pass
        with tempfile.TemporaryDirectory() as td:
            cfg = AgentConfig(LLMConfig(api_key_env='unused', keychain_service=''), FirecrawlConfig(api_key_env='unused'), Path(td)/'db', Path(td)/'missing', Path(td))
            agent = Agent(cfg, client=Client(), knowledge=Knowledge(), firecrawl=object())
            result = agent.ask(question, mode='research')
            self.assertTrue(result['ok'])
            self.assertEqual(result['claim_checks']['quote_checks'], [])

    def test_user_supplied_fake_mao_words_do_not_exempt_explicit_author_attribution(self):
        user_text = '网上有人把“努力就必然成功。”归给毛泽东，我觉得可能是伪引语。'
        for prefix in ['毛泽东说：', '原著写道：', '《实践论》写道：']:
            with self.subTest(prefix=prefix):
                checked = check_answer_claims(prefix + '“努力就必然成功。”【C1】', tools(source()), user_texts=[user_text])
                self.assertFalse(checked['passed'])
                self.assertEqual(checked['quote_checks'][0]['status'], 'text_mismatch')

    def test_forty_valid_quotes_cannot_hide_a_forty_first_forged_quote(self):
        forty = '\n'.join(['毛泽东写道：“认识依赖实践。”【C1】'] * 40)
        self.assertTrue(check_answer_claims(forty, tools(source()))['passed'])
        checked = check_answer_claims(forty + '\n毛泽东说：“努力就必然成功。”【C1】', tools(source()))
        self.assertFalse(checked['passed'])
        self.assertLessEqual(len(checked['quote_checks']), 40)
        self.assertEqual(checked['quote_checks'][-1]['status'], 'check_limit_reached')

    def test_many_modern_or_user_quotes_do_not_consume_the_historical_quote_budget(self):
        modern = '\n'.join(['现代建议：可以问客户：“您具体想了解什么？”'] * 45)
        user_text = '我希望先整理文档，没有额外预算。'
        user_quotes = '\n'.join(['原话：“没有额外预算”。'] * 45)
        checked = check_answer_claims(modern + '\n' + user_quotes + '\n毛泽东写道：“认识依赖实践。”【C1】',
                                      tools(source()), user_texts=[user_text])
        self.assertTrue(checked['passed'])
        self.assertEqual(len(checked['quote_checks']), 1)
        self.assertEqual(checked['quote_checks'][0]['status'], 'matched_excerpt')

    def test_normal_stream_blocks_forged_quotation_before_any_content_and_exports_successful_checks(self):
        class Knowledge:
            def search(self, query='', **kwargs): return {'results': [source()], 'untrusted': False}
            def trace_quote(self, **kwargs): return {'results': []}
            def trace_event(self, **kwargs): return {'results': []}
            def store_web_sources(self, result): pass
        class Client:
            def __init__(self, answer): self.answer = answer; self.calls = 0
            def chat(self, messages, tools=None):
                self.calls += 1
                if self.calls == 1:
                    return LLMResponse(tool_calls=[{'id': 'one', 'function': {'name': 'local_search', 'arguments': '{"query":"实践"}'}}])
                return LLMResponse(content=self.answer)
        with tempfile.TemporaryDirectory() as td:
            cfg = AgentConfig(LLMConfig(api_key_env='unused', keychain_service=''), FirecrawlConfig(api_key_env='unused'), Path(td)/'db', Path(td)/'missing', Path(td))
            bad = Agent(cfg, client=Client('毛泽东说：“努力就必然成功。”【C1】'), knowledge=Knowledge(), firecrawl=object())
            events = list(bad.stream('核对原文', mode='research'))
            self.assertFalse(any(x['type'] == 'content' for x in events))
            self.assertTrue(any(x['type'] == 'error' and '逐字' in x['error'] for x in events))
            good = Agent(cfg, client=Client('原文写道：“认识依赖实践。”【C1】'), knowledge=Knowledge(), firecrawl=object())
            events = list(good.stream('核对原文', mode='research'))
            meta = next(x for x in events if x['type'] == 'answer_meta')
            self.assertEqual(meta['claim_checks']['quote_checks'][0]['status'], 'matched_excerpt')
            self.assertEqual(meta['used_evidence_ids'], ['C1'])

    def test_optional_statement_sources_preserve_exact_quotes_without_granting_confirmation(self):
        value = understanding(phase='clarify', summary='先保住客户', stage_goal='保住客户',
                              statement_sources=[{'field': 'stage_goal', 'quote': '我想先保住客户'}])
        self.assertEqual(value['statement_sources'][0]['quote'], '我想先保住客户')
        self.assertTrue(value['tentative'])
        self.assertNotIn('statement_sources', UNDERSTANDING_PARAMETERS['required'])
        self.assertEqual(understanding(phase='listen', summary='先听你讲')['statement_sources'], [])
        for item in [{'field': 'uncertainties:0', 'quote': '推测'}, {'field': 'stage_goal', 'quote': '', 'confirmed': True}]:
            with self.assertRaises(ValueError):
                understanding(phase='clarify', summary='暂定', statement_sources=[item])

    def test_runtime_audit_retains_pipeline_source_and_actual_usage_versions(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = AgentConfig(LLMConfig(api_key_env='unused', keychain_service=''), FirecrawlConfig(api_key_env='unused'), Path(td)/'db', Path(td)/'missing', Path(td))
            agent = Agent(cfg, client=object(), knowledge=object(), firecrawl=object())
            with telemetry_scope():
                record_model_call({'prompt_tokens': 7, 'completion_tokens': 3}, 12)
                agent._persist_run('测试问题', 'research', None, '测试回答【C1】', tools(source()), .02)
            with connect(cfg.db_path) as conn:
                row = conn.execute('SELECT * FROM llm_runs').fetchone()
            self.assertIsNotNone(row)
            self.assertEqual((row['input_tokens'], row['output_tokens']), (7, 3))
            metadata = json.loads(row['metadata_json'])
            for key in ('pipeline_version', 'prompt_version', 'knowledge_version'):
                self.assertEqual(len(metadata[key]), 64)
            self.assertEqual(len(metadata['source_fingerprints']['C1']), 64)
            self.assertTrue(metadata['usage']['usage_complete'])
            self.assertNotIn('reasoning', metadata)

    def test_explicit_structured_quote_cannot_bypass_the_same_excerpt_gate(self):
        class Client:
            def chat(self, messages, **kwargs):
                return LLMResponse(content=json.dumps({'answer': '假引语【C1】', 'claims': [
                    {'text': '努力就必然成功。', 'kind': 'quote', 'evidence_ids': ['C1']}]}))
        with tempfile.TemporaryDirectory() as td:
            cfg = AgentConfig(LLMConfig(api_key_env='unused', keychain_service=''), FirecrawlConfig(api_key_env='unused'), Path(td)/'db', Path(td)/'missing', Path(td))
            agent = Agent(cfg, client=Client(), knowledge=object(), firecrawl=object())
            with self.assertRaisesRegex(LLMError, '逐字'):
                agent.structured_draft('核对', evidence=[source()])


if __name__ == '__main__': unittest.main()
