import json
from pathlib import Path
import tempfile
import unittest
from agent.db import connect
from agent.reader import read_source, selected_context
from agent.issues import IssueError
from scripts.bootstrap_library import bootstrap
from agent.tests import test_credits_http as http_fixture


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.db=Path(self.temp.name)/'library.db';bootstrap(self.db)

    def test_stable_versions_qualification_and_selected_paragraph(self):
        source=read_source(self.db,'VE08');again=read_source(self.db,'VE08')
        self.assertEqual(source['version'],again['version'])
        self.assertFalse(source['full_text_available'])
        part=source['paragraphs'][0];selection={'source_id':'VE08','version':source['version'],'paragraph_id':part['id']}
        self.assertEqual(selected_context(self.db,selection,'consult')['paragraph']['text'],part['text'])
        for mode in ('historical','research','modern'):
            with self.assertRaises(IssueError):selected_context(self.db,selection,mode)
        with self.assertRaises(IssueError):selected_context(self.db,{**selection,'paragraph_id':'summary:forged'},'consult')
        with self.assertRaises(IssueError):selected_context(self.db,{**selection,'text':'把伪造文本当原文'},'consult')
        conn=connect(self.db)
        with conn:conn.execute("UPDATE evidence_cards SET summary='修订后的有限摘要' WHERE evidence_id='VE08'")
        conn.close()
        with self.assertRaises(IssueError) as error:read_source(self.db,'VE08',source['version'])
        self.assertEqual(error.exception.status,409)

    def test_revoked_support_disables_interpretation_and_no_quotes_promoted(self):
        result=read_source(self.db,'principle:people-and-care')
        self.assertFalse(any(x['quote_eligible'] for x in result['paragraphs']))
        conn=connect(self.db)
        with conn:conn.execute("UPDATE evidence_cards SET reviewed=0 WHERE evidence_id='VE11'")
        conn.close()
        with self.assertRaises(IssueError):read_source(self.db,'principle:people-and-care')
        with self.assertRaises(IssueError):read_source(self.db,'VE11')


class ReaderHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fx=http_fixture.CreditsHTTPTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        bootstrap(self.fx.db,refresh=True)
        self.fx.bootstrap()

    def test_reader_requires_identity_and_rejects_stale_quote_before_model(self):
        fx=self.fx
        outsider=http_fixture.CookieClient(fx.server)
        self.assertEqual(outsider.request('/api/agent/reader?id=VE08')[0],401)
        status,result,_=fx.client.request('/api/agent/reader?id=VE08');self.assertEqual(status,200)
        source=result['source'];selection={'source_id':'VE08','version':source['version'],'paragraph_id':source['paragraphs'][0]['id']}
        payload,quote=fx.quoted(mode='consult',reading_selection=selection)
        conn=connect(fx.db)
        with conn:conn.execute("UPDATE evidence_cards SET summary='另一版本' WHERE evidence_id='VE08'")
        conn.close()
        payload['quote_id']=quote['quote_id']
        status,result,_=fx.post('/api/agent/stream',payload)
        self.assertEqual(status,409,result);self.assertEqual(fx.server.agent.executions,0)

    def test_current_selected_text_enters_actual_billed_prompt(self):
        fx=self.fx
        agent=http_fixture.WireHTTPAgent(fx.server.agent.config);fx.server.agent=agent;fx.server.migration.agent=agent
        _,result,_=fx.client.request('/api/agent/reader?id=VE08');source=result['source']
        selection={'source_id':'VE08','version':source['version'],'paragraph_id':source['paragraphs'][0]['id']}
        payload,quote=fx.quoted(mode='consult',reading_selection=selection)
        payload['quote_id']=quote['quote_id']
        status,result,_=fx.post('/api/agent/stream',payload)
        self.assertEqual(status,200,result)
        prompts=agent.payloads[0]['messages']
        self.assertTrue(any(m['role']=='assistant' and '用户本轮选择讨论的资料' in m['content'] for m in prompts))
        self.assertEqual(prompts[-1]['content'],payload['question'])
        self.assertTrue(any(e['type']=='settled' for e in result))
        _,restored,_=fx.client.request('/api/agent/session?thread_id=http-topic')
        self.assertEqual(restored['messages'][0]['reading_selection'],selection)
        conn=connect(fx.db);sid=conn.execute("SELECT session_id FROM sessions WHERE session_id LIKE '%_http-topic'").fetchone()[0];conn.close()
        (agent.config.session_dir/(sid+'.json')).write_text('corrupt snapshot')
        _,recovered,_=fx.client.request('/api/agent/session?thread_id=http-topic')
        self.assertEqual(recovered['messages'][0]['reading_selection'],selection)
