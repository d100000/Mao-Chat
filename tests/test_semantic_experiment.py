import tempfile
import unittest
from agent.semantic_experiment import LocalSemanticExperiment
from agent.principles import PRINCIPLES,search_principles


class SemanticExperimentTests(unittest.TestCase):
    def setUp(self):self.ids={x for row in PRINCIPLES for x in row['evidence_ids']}

    def test_disabled_missing_and_load_failure_are_exact_lexical_fallback(self):
        query='团队凭感觉做决定';expected=search_principles(query,self.ids)
        for model in (LocalSemanticExperiment(),LocalSemanticExperiment('/nonexistent/local-model')):
            self.assertEqual(model.search(query,self.ids),expected)
        with tempfile.TemporaryDirectory() as directory:
            calls=[]
            def fail(*a,**kw):calls.append(kw);raise RuntimeError('load failure')
            model=LocalSemanticExperiment(directory,loader=fail)
            self.assertEqual(model.search(query,self.ids),expected);self.assertEqual(model.status,'load_failed')
            self.assertEqual(model.search(query,self.ids),expected);self.assertEqual(len(calls),1)
            self.assertTrue(calls[0]['local_files_only']);self.assertFalse(calls[0]['trust_remote_code']);self.assertFalse(calls[0]['token'])

    def test_encoder_only_sees_current_qualified_candidates_and_never_grants_quote(self):
        texts=[]
        class Encoder:
            def encode(self,values,**kw):texts.extend(values);return [[1.0,0.0] for _ in values]
        with tempfile.TemporaryDirectory() as directory:
            model=LocalSemanticExperiment(directory,loader=lambda *a,**kw:Encoder())
            ids=self.ids-{'VE11'};result=model.search('身体快熬坏了',ids)
            self.assertNotIn('人民利益与人的实际代价',' '.join(texts))
            self.assertTrue(all(set(r['evidence_ids']).issubset(ids) and not r['quote_eligible'] for r in result))
            self.assertEqual(model.status,'ready')

    def test_encoding_failure_does_not_break_lexical_queries(self):
        class Encoder:
            def encode(self,*a,**kw):return [[float('nan')]]
        with tempfile.TemporaryDirectory() as directory:
            model=LocalSemanticExperiment(directory,loader=lambda *a,**kw:Encoder())
            self.assertEqual(model.search('调查事实',self.ids),search_principles('调查事实',self.ids))
            self.assertEqual(model.status,'encoding_failed')
