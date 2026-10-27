"""Editorial principles remain conditional interpretations of reviewed sources."""
import json
from pathlib import Path
import tempfile
import unittest

from agent.db import connect, ensure_schema
from agent.dialogue import CuratedMethodLibrary
from agent.principles import PRINCIPLES, available_principles, search_principles
from agent.runtime import Agent


class PrincipleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='jiaoyuan-principles-')
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'library.db'
        self.conn = connect(self.db)
        self.addCleanup(self.conn.close)
        ensure_schema(self.conn)
        self.library = CuratedMethodLibrary(self.db)

    def card(self, identity, reviewed=1, role='author_work'):
        self.conn.execute(
            'INSERT OR REPLACE INTO evidence_cards(evidence_id,article_title,summary,excerpt,reviewed,quote_verified,source_role,raw_json) VALUES(?,?,?,?,?,?,?,?)',
            (identity, '测试来源', '经核验的有限摘要', '没有逐字资格的措辞', reviewed, 0, role, json.dumps({'pdf_pages': [1]})))
        self.conn.commit()

    def test_stable_principles_keep_their_interpretation_and_historical_boundaries(self):
        identities = [row['principle_id'] for row in PRINCIPLES]
        self.assertEqual(len(identities), 12)
        self.assertEqual(len(set(identities)), 12)
        self.assertTrue(all(value.startswith('principle:') for value in identities))
        all_sources = {value for row in PRINCIPLES for value in row['evidence_ids']}
        available = available_principles(all_sources)
        self.assertEqual(len(available), 12)
        self.assertTrue(all(row['historical_conditions'] and row['unsupported_extensions'] for row in available))
        self.assertTrue(all(row['record_type'] == 'principle_note' and not row['quote_eligible'] for row in available))
        learn = next(row for row in available if row['principle_id'] == 'principle:learn-with-conditions')
        self.assertIn('1976', learn['historical_conditions'])
        self.assertTrue(any('自力更生' in value for value in learn['unsupported_extensions']))
        # Search results are independent copies: a caller cannot mutate the
        # definitions or silently change later users' qualification flags.
        available[0]['evidence_ids'].append('FAKE')
        self.assertNotIn('FAKE', PRINCIPLES[0]['evidence_ids'])

    def test_values_retrieval_requires_a_current_reviewed_author_source(self):
        self.assertEqual(self.library.search('人民健康和加班代价')['principles'], [])
        self.card('VE11', reviewed=0)
        self.assertEqual(self.library.search('人民健康和加班代价')['principles'], [])
        self.card('VE11', role='biographical_record')
        self.assertEqual(self.library.search('人民健康和加班代价')['principles'], [])
        self.card('VE11')
        result = self.library.search('人民健康和加班代价')
        note = next(row for row in result['principles'] if row['principle_id'] == 'principle:people-and-care')
        self.assertEqual(note['evidence_ids'], ['VE11'])
        self.assertFalse(note['quote_eligible'])
        author = next(row for row in result['results'] if row.get('evidence_id') == 'VE11')
        self.assertEqual(author['excerpt'], '')
        self.assertFalse(author['quote_eligible'])
        self.assertIn('减少不必要牺牲', note['author_claim'])
        self.assertTrue(any('个人需求' in x for x in note['unsupported_extensions']))

    def test_every_linked_card_must_remain_reviewed_and_revocation_blocks_old_ids(self):
        self.card('VE13')
        marker = 'principle:participation-and-willingness'
        self.assertEqual(self.library.verify_citations([marker])['checks'][0]['status'], 'not_found')
        self.card('VE03')
        traced = self.library.verify_citations([marker, 'principle:' + marker])
        self.assertEqual([row['status'] for row in traced['checks']], ['matched', 'matched'])
        self.assertEqual(len(traced['principles']), 1)
        self.assertEqual(traced['results'], [])
        self.assertFalse(traced['principles'][0]['quote_eligible'])
        self.card('VE03', reviewed=0)
        self.assertEqual(self.library.verify_citations([marker])['principles'], [])
        self.assertEqual(self.library.verify_citations([marker])['checks'][0]['status'], 'not_found')

    def test_principle_links_do_not_implicitly_authorize_an_author_citation(self):
        self.card('VE11')
        result = self.library.verify_citations(['principle:people-and-care'])
        tools = [{'name': 'verify_citations', 'result': result}]
        _, gate = Agent._apply_evidence_gate('研究者解释【principle:people-and-care】；未返回原著卡【VE11】。', tools, 'consult', None)
        self.assertEqual(gate['unknown_markers'], ['VE11'])
        self.assertEqual(Agent.used_evidence_ids('研究者解释【principle:people-and-care】', tools), ['principle:people-and-care'])

    def test_principle_results_are_bounded_and_do_not_return_for_unrelated_queries(self):
        ids = {value for row in PRINCIPLES for value in row['evidence_ids']}
        for identity in ids:
            self.card(identity)
        result = self.library.search('人民 调查 实践 矛盾 责任 自愿 阶段 学习 分歧', limit=10**9)
        self.assertLessEqual(len(result['principles']), 3)
        self.assertLessEqual(len(result['results']), 6)
        self.assertEqual(search_principles('量子火箭', ids), [])
        self.assertTrue(all(set(row['evidence_ids']).issubset(ids) for row in result['principles']))


if __name__ == '__main__':
    unittest.main()
