import tempfile
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from agent.db import connect
from agent.dialogue import CuratedMethodLibrary
from agent.issues import IssueError
from agent.knowledge_admin import KnowledgeStore
from agent.knowledge_versions import knowledge_revision
from agent.reader import read_source
from scripts.bootstrap_library import bootstrap


class KnowledgeReleaseTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.db=Path(tmp.name)/'library.db';bootstrap(self.db);self.store=KnowledgeStore(self.db)

    def reviewed(self, identity='VE11', **values):
        result=self.store.create(identity,{'summary':'编辑核验后的有限摘要，测试替身不是新增历史主张。',**values},1)
        draft=result['draft_id']
        self.store.review(draft,actor=1,approved=True,quote_verified=False,scope='测试人工核验摘要',reason='测试编辑核对公开来源',source_checked=True)
        return draft

    def test_candidate_cannot_grant_eligibility_and_preview_required(self):
        draft=self.store.create('ED_TEST',{'article_title':'测试候选','summary':'不能作为已核史实','source_urls':['https://example.com/test']},1)['draft_id']
        with self.assertRaises(IssueError):read_source(self.db,'ED_TEST')
        preview=self.store.preview(draft)
        self.assertFalse(preview['after']['reviewed'])
        with self.assertRaises(IssueError):self.store.publish(draft,preview['preview_token'],1)
        with self.assertRaises(IssueError):self.store.create('ED_TEST',{'reviewed':True},1)
        with self.assertRaises(IssueError):self.store.review(draft,actor=1,approved=True,quote_verified=False,scope='摘要',reason='未确认',source_checked=False)

    def test_publish_is_atomic_idempotent_and_rollback_survives_restart(self):
        old=knowledge_revision(self.db);draft=self.reviewed();preview=self.store.preview(draft)
        result=self.store.publish(draft,preview['preview_token'],1)
        self.assertNotEqual(old,result['revision'])
        self.assertEqual(result['manifest']['reading_revision'],result['manifest']['retrieval_revision'])
        self.assertEqual(self.store.publish(draft,preview['preview_token'],1)['release_id'],result['release_id'])
        self.assertEqual(CuratedMethodLibrary(self.db).verify_citations(['principle:people-and-care'])['principles'],[])
        self.assertFalse(read_source(self.db,'VE11')['paragraphs'][0]['quote_eligible'])
        rollback=KnowledgeStore(self.db).rollback(result['release_id'],result['revision'],2)
        self.assertEqual(rollback['revision'],old)
        self.assertTrue(CuratedMethodLibrary(self.db).verify_citations(['principle:people-and-care'])['principles'])
        with self.assertRaises(IssueError):self.store.rollback(result['release_id'],result['revision'],2)

    def test_index_failure_preserves_library_indexes_reader_and_draft(self):
        old=read_source(self.db,'VE11');revision=knowledge_revision(self.db);draft=self.reviewed();preview=self.store.preview(draft)
        def fail(conn, **kw):
            conn.execute('DROP TABLE quotes_fts')
            raise RuntimeError('injected temporary index build failure')
        with patch('agent.knowledge_admin.build_indexes',side_effect=fail):
            with self.assertRaises(RuntimeError):self.store.publish(draft,preview['preview_token'],1)
        self.assertEqual(revision,knowledge_revision(self.db))
        self.assertEqual(old,read_source(self.db,'VE11'))
        conn=connect(self.db)
        self.assertGreater(conn.execute('SELECT count(*) FROM quotes_fts').fetchone()[0],0);conn.close()
        self.assertEqual(self.store.preview(draft)['status'],'reviewed')
        self.store.publish(draft,preview['preview_token'],1)

    def test_stale_preview_or_qualification_update_requires_new_review(self):
        draft=self.reviewed();preview=self.store.preview(draft)
        self.store.review(draft,actor=1,approved=False,quote_verified=False,scope='撤回核验',reason='发现核验范围不充分',source_checked=False)
        with self.assertRaises(IssueError):self.store.publish(draft,preview['preview_token'],1)
        result=self.store.publish(draft,self.store.preview(draft)['preview_token'],1)
        with self.assertRaises(IssueError):read_source(self.db,'VE11')
        self.assertTrue(result['revision'])


class KnowledgeHTTPTests(unittest.TestCase):
    def setUp(self):
        from agent.tests import test_credits_http as fixtures
        self.fx=fixtures.CreditsHTTPTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        bootstrap(self.fx.db,refresh=True)
        service=self.fx.server.auth_service
        service.bootstrap_admin('library_admin','library-test-password-123')
        login=service.admin_login('library_admin','library-test-password-123',require_captcha=False)
        self.admin=fixtures.CookieClient(self.fx.server)
        self.admin.cookies['mao_admin_session']=login['session_token']

    def test_admin_only_publication_with_origin_and_preview_checks(self):
        self.fx.bootstrap()
        self.assertEqual(self.fx.client.request('/api/admin/knowledge')[0],401)
        body={'source_id':'VE08','patch':{'summary':'隔离测试修订'}}
        self.assertEqual(self.fx.post('/api/admin/knowledge/draft',body)[0],401)
        revision=knowledge_revision(self.fx.db)
        status,_,_=self.admin.request('/api/admin/knowledge/draft','POST',body,headers={'Origin':'https://untrusted.invalid'})
        self.assertEqual(status,403);self.assertEqual(knowledge_revision(self.fx.db),revision)
        status,result,_=self.admin.request('/api/admin/knowledge/draft','POST',body)
        self.assertEqual(status,200,result)
        draft=result['draft_id']
        status,result,_=self.admin.request('/api/admin/knowledge/publish','POST',{'draft_id':draft,'preview_token':'forged'})
        self.assertEqual(status,409,result);self.assertEqual(knowledge_revision(self.fx.db),revision)
        status,overview,_=self.admin.request('/api/admin/knowledge');self.assertEqual(status,200)
        self.assertFalse(overview['semantic']['enabled'])
        self.assertFalse(any(k in json.dumps(overview) for k in ('session_token','password','user_id')))
