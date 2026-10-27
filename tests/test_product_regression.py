"""Validate the review corpus, without making model calls or assigning scores."""
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ProductRegressionDatasetTests(unittest.TestCase):
    def test_sixty_distinct_four_to_eight_turn_cases_cover_all_product_risks(self):
        data = json.loads((ROOT / 'tests/product_regression.json').read_text())
        self.assertEqual(data['schema_version'], 1)
        self.assertEqual(len(data['categories']), 12)
        self.assertEqual(len(data['cases']), 60)
        self.assertIn('不是真实用户', data['provenance'])
        self.assertIn('默认单元测试只校验结构', data['execution'])
        self.assertIn('仅有用户文字请求', data['ui_actions'])
        identities, conversations, messages, counts = set(), set(), set(), {}
        for case in data['cases']:
            self.assertNotIn(case['id'], identities)
            identities.add(case['id'])
            self.assertIn(case['category'], data['categories'])
            counts[case['category']] = counts.get(case['category'], 0) + 1
            self.assertTrue(case['tags'])
            self.assertTrue(all(isinstance(x, str) and x for x in case['tags']))
            self.assertTrue(4 <= len(case['turns']) <= 8)
            conversation = []
            for turn in case['turns']:
                self.assertEqual(set(turn), {'user', 'checks'})
                self.assertIsInstance(turn['user'], str)
                self.assertTrue(1 <= len(turn['user']) <= 4000)
                self.assertNotIn(turn['user'], messages)
                messages.add(turn['user'])
                self.assertGreaterEqual(len(turn['checks']), 2)
                self.assertTrue(all(isinstance(x, str) and len(x) >= 8 for x in turn['checks']))
                conversation.append(turn['user'])
            fingerprint = tuple(conversation)
            self.assertNotIn(fingerprint, conversations)
            conversations.add(fingerprint)
            self.assertFalse({'score', 'passed', 'model_answer', 'participant'} & set(case))
        self.assertEqual(set(counts.values()), {5})
        self.assertGreaterEqual(len(messages), 240)

    def test_corpus_includes_correction_privacy_and_scope_regressions(self):
        data = json.loads((ROOT / 'tests/product_regression.json').read_text())
        cases = {x['id']: x for x in data['cases']}
        paraphrase = cases['memory_conflict-02']
        self.assertIn('预算不能增加', paraphrase['turns'][2]['user'])
        self.assertIn('无新来源', paraphrase['turns'][2]['checks'][0])
        short = cases['expression_only-01']
        self.assertIn('三句话', short['turns'][2]['user'])
        self.assertIn('不改阶段目标或行动状态', short['turns'][3]['checks'][0])
        quote = cases['quotation_checks-05']
        self.assertIn('用户拟用现代话语', quote['turns'][0]['checks'][0])
        risk = cases['safety_professional-01']
        self.assertIn('不虚构已报警', risk['turns'][3]['checks'][0])
        privacy = cases['privacy_exit-02']
        self.assertIn('不冒充已完成', privacy['turns'][1]['checks'][0])


if __name__ == '__main__':
    unittest.main()
