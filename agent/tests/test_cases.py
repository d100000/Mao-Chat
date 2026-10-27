from pathlib import Path
import tempfile
import unittest

from agent.cases import CASE_DEFINITIONS
from agent.db import connect
from agent.dialogue import CuratedMethodLibrary
from agent.runtime import Agent
from scripts.bootstrap_library import bootstrap


class HistoricalCaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name)/'library.db'
        bootstrap(self.db)
        self.library = CuratedMethodLibrary(self.db)

    def test_references_keep_verified_facts_and_explicit_missing_outcomes(self):
        self.assertEqual(len(CASE_DEFINITIONS), 10)
        for identity in list(CASE_DEFINITIONS)[:9]:
            with self.subTest(identity=identity):
                result = self.library.verify_citations(['case:' + identity])
                case = result['cases'][0]
                self.assertEqual(case['completion'], 'limited_facts')
                self.assertTrue(case['facts'] and case['unknowns'] and case['non_analogies'])
                self.assertEqual(case['outcome_evidence'], [])
                self.assertFalse(case['quote_eligible'])
        # A publication/version record with no verified participation date
        # remains a research record, not a newly invented public-event case.
        self.assertEqual(self.library.verify_citations(['case:TL10'])['cases'], [])

    def test_case_lookup_fails_closed_after_source_or_event_withdrawal(self):
        with connect(self.db) as conn:
            conn.execute("UPDATE evidence_cards SET reviewed=0 WHERE evidence_id='TL01'")
        self.assertEqual(self.library.verify_citations(['case:TL01'])['cases'], [])
        with connect(self.db) as conn:
            conn.execute("UPDATE evidence_cards SET reviewed=1 WHERE evidence_id='TL01'")
            conn.execute("UPDATE events SET description='未经核验的新说法' WHERE event_id='event:TL01'")
        self.assertEqual(self.library.verify_citations(['case:TL01'])['cases'], [])
        with connect(self.db) as conn:
            conn.execute("UPDATE events SET verification_status='rejected' WHERE event_id='event:TL02'")
        self.assertEqual(self.library.verify_citations(['case:TL02'])['cases'], [])

    def test_method_search_bounds_cases_and_does_not_authorize_linked_author_quotes(self):
        result = self.library.search('湖南 调查 群众', limit=10**9)
        self.assertTrue(result['cases'])
        self.assertLessEqual(len(result['cases']), 2)
        case = result['cases'][0]
        tools = [{'name': 'method_search', 'result': {'cases': [case]}}]
        known = Agent._evidence_markers(tools)
        self.assertIn(case['case_id'], known)
        self.assertNotIn(case['supporting_evidence_ids'][0], known)
        self.assertNotIn(case['event_id'], known)
        self.assertEqual(Agent.used_evidence_ids('有限案例【' + case['case_id'] + '】', tools), [case['case_id']])
        self.assertEqual(self.library.search('量子火箭')['cases'], [])


if __name__ == '__main__': unittest.main()
