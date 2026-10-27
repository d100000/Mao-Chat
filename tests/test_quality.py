import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from agent.quality import QualityStore
from agent.telemetry import telemetry_scope, record_model_call, record_stage, audit_metadata
from agent.runtime import AgentConfig
from agent.llm import LLMConfig, OpenAICompatibleClient
from agent.firecrawl import FirecrawlConfig
from tests import test_issues as fixtures


class QualityTests(unittest.TestCase):
    def test_request_scopes_usage_and_versions_never_include_secrets(self):
        config=AgentConfig(LLMConfig(api_key_env='UNUSED_TEST'),FirecrawlConfig())
        with telemetry_scope():
            record_model_call({'prompt_tokens':12,'completion_tokens':3,'secret':'private'},20)
            record_stage('tool:method_search',10)
            a=audit_metadata(config,[{'result':{'results':[{'evidence_id':'VE08','summary':'source','rank':1}]}}])
            b=audit_metadata(config,[{'result':{'results':[{'evidence_id':'VE08','summary':'source','rank':9}]}}])
            self.assertEqual(a['source_fingerprints'],b['source_fingerprints'])
            self.assertEqual(a['usage']['input_tokens'],12)
            self.assertNotIn('private',json.dumps(a))
            with telemetry_scope(): self.assertEqual(audit_metadata(config,[])['usage']['calls'],0)
            self.assertEqual(audit_metadata(config,[])['usage']['calls'],1)
            record_model_call({},2)
            self.assertIsNone(audit_metadata(config,[])['usage']['input_tokens'])
        self.assertEqual(audit_metadata(config,[])['usage']['calls'],0)

    def test_client_collects_reported_usage_once_without_a_second_request(self):
        from agent.tests.test_runtime_mock import FakeResponse
        import os
        body=b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: {"usage":{"prompt_tokens":12,"completion_tokens":3},"choices":[]}\n\ndata: [DONE]\n\n'
        calls=[]
        def opener(req,timeout=None):
            calls.append(req)
            return FakeResponse(body)
        with patch.dict(os.environ,{'TEST_USAGE_KEY':'placeholder'}),telemetry_scope():
            config=LLMConfig(api_key_env='TEST_USAGE_KEY',keychain_service='')
            response=OpenAICompatibleClient(config,opener=opener).chat([{'role':'user','content':'test'}])
            self.assertEqual(response.content,'ok')
            measured=audit_metadata(AgentConfig(config,FirecrawlConfig()),[])['usage']
            self.assertEqual((measured['calls'],measured['input_tokens'],measured['output_tokens']),(1,12,3))
            self.assertEqual(len(calls),1)

    def test_provider_error_cannot_echo_a_shared_credential_to_the_user(self):
        from urllib.error import HTTPError
        import io,os
        def opener(req,timeout=None):
            raise HTTPError(req.full_url,401,'bad key',{},io.BytesIO(b'Incorrect API key: private-shared-test-value'))
        with patch.dict(os.environ,{'TEST_USAGE_KEY':'private-shared-test-value'}):
            client=OpenAICompatibleClient(LLMConfig(api_key_env='TEST_USAGE_KEY',keychain_service=''),opener=opener)
            with self.assertRaises(Exception) as error:client.chat([{'role':'user','content':'test'}])
            self.assertNotIn('private-shared-test-value',str(error.exception))
            self.assertIn('[redacted]',str(error.exception))

    def test_aggregate_unknown_cost_and_feedback_upsert_are_honest(self):
        with tempfile.TemporaryDirectory() as td:
            store=QualityStore(Path(td)/'test.db')
            empty=store.aggregate();self.assertIsNone(empty['latency']['p50_ms'])
            store.record_run(1,'issue','a','ok',250,240,{'usage':{'calls':1,'usage_complete':False},'answer':'private'})
            store.record_run(1,'issue','b','error',900,None,{},reason='provider')
            store.feedback(1,'issue','a','fact_error','original')
            store.feedback(1,'issue','a','helpful','changed')
            data=store.aggregate()
            self.assertEqual(data['runs'],2);self.assertEqual(data['completed'],1)
            self.assertEqual(data['latency']['p50_ms'],250)
            self.assertEqual(data['feedback']['helpful'],1);self.assertEqual(data['feedback']['fact_error'],0)
            self.assertIsNone(data['usage']['reported_input_tokens']);self.assertIsNone(data['usage']['cost'])
            self.assertNotIn('private',json.dumps(data));self.assertNotIn('changed',json.dumps(data))
            with self.assertRaises(ValueError):store.aggregate(10**9)


class QualityHTTPTests(unittest.TestCase):
    setUp=fixtures.IssueHTTPTests.setUp
    stop=fixtures.IssueHTTPTests.stop
    req=fixtures.IssueHTTPTests.req
    stream=fixtures.IssueHTTPTests.stream
    fake=fixtures.IssueHTTPTests.fake

    def test_feedback_is_owned_bound_to_a_real_answer_and_deleted(self):
        with patch.object(self.server.agent,'stream',side_effect=self.fake): self.stream()
        _,_,body=self.req('/api/agent/session?thread_id=issue')
        data=json.loads(body);turn=data['metadata']['latest_turn_id']
        self.assertEqual(data['messages'][-1]['turn_id'],turn)
        payload={'thread_id':'issue','turn_id':turn,'kind':'fact_error','comment':'目标理解错了'}
        self.assertEqual(self.req('/api/agent/feedback',payload,user=None)[0],401)
        self.assertEqual(self.req('/api/agent/feedback',payload,user='two')[0],404)
        self.assertEqual(self.req('/api/agent/feedback',payload,origin='https://evil.test')[0],403)
        self.assertEqual(self.req('/api/agent/feedback',{**payload,'turn_id':'f'*32})[0],404)
        self.assertEqual(self.req('/api/agent/feedback',payload)[0],200)
        with patch.object(self.server.agent,'stream',side_effect=self.fake): self.stream('请继续')
        self.assertEqual(self.req('/api/agent/feedback',{**payload,'kind':'helpful'})[0],200)
        _,_,exported=self.req('/api/agent/session/export?thread_id=issue')
        exported=json.loads(exported);self.assertEqual(exported['feedback'][0]['kind'],'helpful')
        self.assertTrue(all(x['status']=='proposed' for x in exported['metadata']['actions']))
        self.assertEqual(self.req('/api/agent/session?thread_id=issue',method='DELETE')[0],200)
        self.assertEqual(self.server.quality.export_feedback(1,'issue'),[])
        self.assertEqual(self.server.quality.aggregate()['runs'],0)

    def test_delete_after_save_before_ack_does_not_publish_success(self):
        original=self.server.issues.complete_stream
        def deleting(*args,**kwargs):
            result=original(*args,**kwargs)
            self.server.issues.delete(args[0],args[1],args[2])
            return result
        with patch.object(self.server.agent,'stream',side_effect=self.fake),patch.object(self.server.issues,'complete_stream',side_effect=deleting):
            _,_,body=self.stream()
        self.assertNotIn('"type":"done"',body)
        self.assertNotIn('"type":"session_meta"',body)
        self.assertIn('"deleted":true',body)
        self.assertEqual(self.req('/api/agent/session?thread_id=issue')[0],404)
        self.assertEqual(self.server.quality.aggregate()['runs'],0)

    def test_saved_issue_failure_is_counted_without_storing_provider_error_text(self):
        with patch.object(self.server.agent,'stream',side_effect=self.fake):self.stream()
        def failed(*args,**kwargs):
            yield {'type':'error','error':'LLM 网络请求失败: private diagnostic'}
        with patch.object(self.server.agent,'stream',side_effect=failed):self.stream('重试')
        stats=self.server.quality.aggregate()
        self.assertEqual(stats['completed'],1);self.assertEqual(stats['errors'],1)
        self.assertEqual(stats['failure_reasons'],{'provider':1})
        self.assertNotIn('private diagnostic',json.dumps(stats))
        self.assertEqual(self.req('/api/agent/session?thread_id=issue',method='DELETE')[0],200)
        self.assertEqual(self.server.quality.aggregate()['runs'],0)

    def test_measurement_failure_does_not_fail_a_saved_answer(self):
        import sqlite3
        with patch.object(self.server.agent,'stream',side_effect=self.fake),patch.object(self.server.quality,'connect',side_effect=sqlite3.OperationalError('test measurement outage')):
            status,_,body=self.stream()
        self.assertEqual(status,200)
        self.assertIn('"type":"done"',body)
        self.assertNotIn('"type":"error"',body)
        self.assertEqual(self.req('/api/agent/session?thread_id=issue')[0],200)

    def test_admin_gets_only_aggregates_temporary_writes_nothing(self):
        self.assertEqual(self.req('/api/admin/quality')[0],401)
        with patch.object(self.server.agent,'stream',side_effect=self.fake): self.stream(temporary=True)
        self.assertEqual(self.server.quality.aggregate()['runs'],0)
        with patch.object(self.server.auth_service,'validate_admin_session',return_value={'ok':True,'admin':{'admin_id':1}}):
            status,_,body=self.req('/api/admin/quality')
            self.assertEqual(status,200);self.assertEqual(json.loads(body)['quality']['runs'],0)
            self.assertEqual(self.req('/api/admin/quality?days=9999')[0],400)
