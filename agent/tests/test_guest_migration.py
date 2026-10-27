import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent.db import connect,ensure_schema
from agent.firecrawl import FirecrawlConfig
from agent.guest_migration import GuestMigrationStore
from agent.issues import IssueStore
from agent.llm import LLMConfig
from agent.points import PointsStore,PointsError
from agent.quality import QualityStore
from agent.runtime import Agent,AgentConfig


class GuestMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='jiaoyuan-migration-')
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.db=self.root/'agent.db'
        conn=connect(self.db); ensure_schema(conn); conn.close()
        self.agent=Agent(AgentConfig(LLMConfig(api_key_env='UNUSED_MIGRATION_TEST'),FirecrawlConfig(api_key_env='UNUSED_MIGRATION_WEB_TEST'),self.db,self.root/'corpus.db',self.root/'sessions'))
        self.points=PointsStore(self.db)
        self.issues=IssueStore(self.db,self.agent.config.session_dir)
        self.quality=QualityStore(self.db)
        self.migration=GuestMigrationStore(self.points,self.issues,self.agent)
        self.bootstrap=self.points.bootstrap(); self.guest=self.bootstrap['actor']
        self.user=self.points.user_actor(17)
        self.points.grant_guest(self.guest)

    def rows(self,sql,args=()):
        with sqlite3.connect(self.db) as conn:
            conn.row_factory=sqlite3.Row
            return [dict(x) for x in conn.execute(sql,args)]

    def save(self,owner=None,tid='topic',question='一个尚待澄清的困惑'):
        owner=self.guest['storage_id'] if owner is None else owner
        snap=self.issues.begin_stream(owner,tid,self.agent)
        messages=snap['messages']+[{'role':'user','content':question},{'role':'assistant','content':'先看实际情况。【VE08】','evidence_ids':['VE08']}]
        meta=self.issues.complete_stream(owner,tid,self.agent,messages,{'mode':'consult','as_of':None,'used_evidence_ids':['VE08'],'tools':[]},question,lambda:None,stream_token=snap['stream_token'])
        self.issues.end_stream(owner,tid,snap['stream_token'])
        return meta

    def test_claim_moves_content_and_preserves_message_citation_and_feedback_ids(self):
        meta=self.save(); old_sid=self.issues.session_id(self.guest['storage_id'],'topic')
        mid=self.rows('SELECT message_id FROM messages WHERE session_id=? AND role=?',(old_sid,'assistant'))[0]['message_id']
        with sqlite3.connect(self.db) as conn:
            conn.execute('INSERT INTO citations(message_id,evidence_id,verification_status) VALUES(?,?,?)',(mid,'VE08','verified'))
            conn.execute('INSERT INTO llm_runs(session_id,provider,model) VALUES(?,?,?)',(old_sid,'test','test-model'))
        self.quality.feedback(self.guest['storage_id'],'topic',meta['latest_turn_id'],'helpful','与实际有关')
        self.quality.record_run(self.guest['storage_id'],'topic',meta['latest_turn_id'],'ok',100,20,{})
        result=self.migration.claim(self.guest,self.user,'claim-operation-01')
        self.assertEqual(result['state'],'committed'); self.assertTrue(result['wallet_claimed'])
        self.assertEqual(result['thread_map'],{'topic':'topic'})
        self.assertEqual(self.points.wallet(self.user)['available'],'200.000000')
        self.assertIsNone(self.points.resolve_guest(self.bootstrap['guest_token']))
        self.assertFalse(self.issues._path(self.guest['storage_id'],'topic').exists())
        self.assertEqual(self.rows('SELECT session_id FROM messages WHERE message_id=?',(mid,))[0]['session_id'],'u17_topic')
        self.assertEqual(self.rows('SELECT verification_status FROM citations WHERE message_id=?',(mid,))[0]['verification_status'],'verified')
        self.assertEqual(self.rows('SELECT session_id FROM llm_runs')[0]['session_id'],'u17_topic')
        self.assertEqual(self.rows('SELECT user_id FROM issue_feedback')[0]['user_id'],17)
        self.assertEqual(self.rows('SELECT user_id FROM quality_runs')[0]['user_id'],17)
        payload=json.loads(self.issues._path(17,'topic').read_text())
        self.assertEqual(payload['metadata']['user_id'],17)
        self.assertEqual(payload['messages'][-1]['turn_id'],meta['latest_turn_id'])
        self.assertEqual(self.issues._path(17,'topic').stat().st_mode&0o777,0o600)
        self.assertEqual(self.agent.config.session_dir.stat().st_mode&0o777,0o700)
        duplicate=self.migration.claim(self.guest,self.user,'claim-operation-01')
        self.assertEqual(duplicate['job_id'],result['job_id'])
        self.assertEqual(len(self.rows('SELECT * FROM citations')),1)

    def test_topic_collision_never_overwrites_existing_account_content(self):
        self.save(); self.save(owner=17,question='账户已有的不同问题')
        original=self.issues._path(17,'topic').read_text()
        result=self.migration.claim(self.guest,self.user,'claim-collision-01')
        target=result['thread_map']['topic']
        self.assertNotEqual(target,'topic'); self.assertTrue(target.startswith('guest-'))
        self.assertEqual(self.issues._path(17,'topic').read_text(),original)
        self.assertIn('尚待澄清',self.issues._path(17,target).read_text())
        self.assertEqual(self.migration.claim(self.guest,self.user,'claim-collision-01')['thread_map'],result['thread_map'])

    def test_inflight_stream_finishes_before_content_move_while_wallet_is_already_claimed(self):
        self.save(); owner=self.guest['storage_id']
        snap=self.issues.begin_stream(owner,'topic',self.agent)
        result=self.migration.claim(self.guest,self.user,'claim-active-01')
        self.assertEqual(result['state'],'pending'); self.assertTrue(result['wallet_claimed'])
        self.assertIsNone(self.points.resolve_guest(self.bootstrap['guest_token']))
        with self.assertRaises(PointsError): self.migration.assert_available(owner,'topic')
        messages=snap['messages']+[{'role':'user','content':'补充的新事实'},{'role':'assistant','content':'依据补充继续分析'}]
        self.issues.complete_stream(owner,'topic',self.agent,messages,{'mode':'consult','as_of':None},'补充的新事实',lambda:None,stream_token=snap['stream_token'])
        self.issues.end_stream(owner,'topic',snap['stream_token'])
        final=self.migration.status(result['job_id'],self.user)
        self.assertEqual(final['state'],'committed')
        self.assertEqual(self.issues.load(17,'topic',self.agent)['messages'][-1]['content'],'依据补充继续分析')
        self.assertEqual(self.points.wallet(self.user)['available'],'200.000000')

    def test_crash_after_database_move_recovers_staged_file_and_guards_target(self):
        self.save()
        with patch.object(self.migration,'_publish',side_effect=OSError('simulated crash')):
            result=self.migration.claim(self.guest,self.user,'claim-crash-db-01')
        self.assertEqual(result['state'],'retryable')
        self.assertEqual(len(self.rows("SELECT * FROM messages WHERE session_id='u17_topic'")),2)
        with self.assertRaises(PointsError) as error: self.migration.assert_available(17,'topic')
        self.assertEqual(error.exception.code,'migration_pending')
        restarted=GuestMigrationStore(self.points,IssueStore(self.db,self.agent.config.session_dir),self.agent)
        final=restarted.status(result['job_id'],self.user)
        self.assertEqual(final['state'],'committed'); self.assertTrue(restarted.assert_available(17,'topic'))
        self.assertFalse(self.issues._path(self.guest['storage_id'],'topic').exists())
        self.assertEqual(len(self.rows("SELECT * FROM messages WHERE session_id='u17_topic'")),2)

    def test_crash_before_database_commit_retains_source_for_retry(self):
        self.save(); old_sid=self.issues.session_id(self.guest['storage_id'],'topic')
        with patch.object(self.migration,'_move_database',side_effect=OSError('simulated disk failure')):
            result=self.migration.claim(self.guest,self.user,'claim-crash-before-01')
        self.assertEqual(result['state'],'retryable')
        self.assertEqual(len(self.rows('SELECT * FROM messages WHERE session_id=?',(old_sid,))),2)
        self.assertFalse(self.issues._path(17,'topic').exists())
        self.assertEqual(self.migration.status(result['job_id'],self.user)['state'],'committed')
        self.assertEqual(len(self.rows('SELECT * FROM messages WHERE session_id=?',(old_sid,))),0)

    def test_already_used_wallet_privilege_still_imports_topics_and_preserves_anonymous_remainder(self):
        other=self.points.bootstrap(); self.points.grant_guest(other['actor'])
        self.points.claim_guest(other['actor'],self.user,'prior-wallet-claim')
        self.save()
        result=self.migration.claim(self.guest,self.user,'claim-content-only-01')
        self.assertEqual(result['state'],'committed'); self.assertFalse(result['wallet_claimed'])
        self.assertEqual(self.points.wallet(self.user)['available'],'200.000000')
        anonymous=self.points.resolve_wallet(self.bootstrap['wallet_token'])
        self.assertEqual(self.points.wallet(anonymous)['available'],'100.000000')
        self.assertNotIn('topic',json.dumps(self.points.ledger(anonymous)))

    def test_other_account_and_changed_operation_parameters_are_rejected(self):
        self.save()
        result=self.migration.claim(self.guest,self.user,'claim-owner-01',include_wallet=False)
        with self.assertRaises(PointsError): self.migration.claim(self.guest,self.user,'claim-owner-01',include_wallet=True)
        other=self.points.user_actor(18)
        with self.assertRaises(PointsError): self.migration.claim(self.guest,other,'claim-owner-02')
        with self.assertRaises(PointsError): self.migration.status(result['job_id'],other)
        self.assertEqual(self.issues.list(18,self.agent)['total'],0)

    def test_wallet_only_selection_keeps_content_guest_owned(self):
        self.save()
        result=self.migration.claim(self.guest,self.user,'claim-wallet-only-01',include_conversations=False)
        self.assertEqual(result['state'],'committed')
        self.assertIsNotNone(self.points.resolve_guest(self.bootstrap['guest_token']))
        self.assertTrue(self.issues._path(self.guest['storage_id'],'topic').exists())
        self.assertFalse(self.issues._path(17,'topic').exists())

    def test_temporary_stream_not_imported_and_stale_deleted_snapshot_not_revived(self):
        self.save(tid='deleted')
        self.issues.delete(self.guest['storage_id'],'deleted',self.agent)
        self.issues._path(self.guest['storage_id'],'deleted').write_text(json.dumps({'messages':[{'role':'user','content':'已经删除'}],'metadata':{'title':'不可复活'}}))
        token=self.issues.begin_temporary_stream(self.guest['storage_id'],'temporary')
        result=self.migration.claim(self.guest,self.user,'claim-temporary-01')
        self.assertEqual(result['state'],'pending')
        self.issues.end_stream(self.guest['storage_id'],'temporary',token)
        final=self.migration.status(result['job_id'],self.user)
        self.assertEqual(final['state'],'committed'); self.assertEqual(final['thread_map'],{})
        self.assertFalse(self.issues._path(self.guest['storage_id'],'deleted').exists())
        self.assertEqual(self.issues.list(17,self.agent)['total'],0)

    def test_status_paginates_maps_without_recounting_migrations(self):
        self.save(tid='one'); self.save(tid='two')
        result=self.migration.claim(self.guest,self.user,'claim-pagination-01')
        first=self.migration.status(result['job_id'],self.user,process=False,limit=1)
        self.assertTrue(first['has_more']); self.assertEqual(first['migrated'],2)
        second=self.migration.status(result['job_id'],self.user,limit=1,offset=first['next_offset'])
        self.assertFalse(second['has_more']); self.assertNotEqual(first['thread_map'],second['thread_map'])

    def test_held_request_migrates_wallet_once_and_content_waits_until_settled(self):
        self.save()
        self.points.configure({'daily_cash_limit_cny':'10'})
        self.points.publish_price({'gateway':'https://provider.test/v1','model':'test-model','rates':{'input_uncached':'2','cache_read':'0.5','cache_write':'2','billable_output':'8'}})
        digest=hashlib.sha256(b'question').hexdigest()
        quote=self.points.quote(self.guest,digest,model='test-model',gateway='https://provider.test/v1',input_tokens=1000,output_tokens=100,max_calls=1)
        req=self.points.reserve(self.guest,quote['quote_id'],'migration-request-01',digest)
        result=self.migration.claim(self.guest,self.user,'claim-frozen-request-01')
        self.assertEqual(result['state'],'pending')
        self.assertEqual(self.points.wallet(self.user)['held'],quote['max_points'])
        self.points.before_call(req['request_id'],'call-0001',1000,100)
        self.points.record_call(req['request_id'],'call-0001',{'prompt_tokens':1000,'completion_tokens':100})
        self.points.settle(req['request_id'])
        self.assertEqual(self.migration.status(result['job_id'],self.user)['state'],'committed')
        self.assertEqual(self.points.wallet(self.user)['held'],'0.000000')
        self.assertEqual(self.points.wallet(self.user)['available'],'197.200000')
        self.assertTrue(self.points.audit()['ok'])

    def test_usage_pending_does_not_hold_content_hostage(self):
        self.save()
        self.points.configure({'daily_cash_limit_cny':'10'})
        self.points.publish_price({'gateway':'https://provider.test/v1','model':'test-model','rates':{'input_uncached':'2','cache_read':'0.5','cache_write':'2','billable_output':'8'}})
        digest=hashlib.sha256(b'question').hexdigest()
        quote=self.points.quote(self.guest,digest,model='test-model',gateway='https://provider.test/v1',input_tokens=1000,output_tokens=100,max_calls=1)
        req=self.points.reserve(self.guest,quote['quote_id'],'migration-unknown-01',digest)
        self.points.before_call(req['request_id'],'call-0001',1000,100)
        self.points.record_call(req['request_id'],'call-0001',None)
        self.points.settle(req['request_id'])
        result=self.migration.claim(self.guest,self.user,'claim-usage-pending-01')
        self.assertEqual(result['state'],'committed')
        self.assertEqual(self.points.wallet(self.user)['held'],quote['max_points'])
        self.assertTrue(self.issues.load(17,'topic',self.agent)['exists'])

    def test_resume_after_old_capability_revoked_is_bound_to_account_operation_and_options(self):
        self.save()
        result=self.migration.claim(self.guest,self.user,'claim-resume-only-01',include_wallet=False)
        self.assertIsNone(self.points.resolve_guest(self.bootstrap['guest_token']))
        resumed=self.migration.resume('claim-resume-only-01',self.user,include_wallet=False)
        self.assertEqual(resumed['job_id'],result['job_id'])
        with self.assertRaises(PointsError): self.migration.resume('claim-resume-only-01',self.user,include_wallet=True)
        with self.assertRaises(PointsError): self.migration.resume('claim-resume-only-01',self.points.user_actor(18),include_wallet=False)

    def test_stale_content_cleanup_does_not_delete_wallet_or_active_stream(self):
        self.save(); source=self.guest['storage_id']
        initial=self.points._now()
        self.points.clock=lambda:initial+181*86400
        active=self.issues.begin_stream(source,'topic',self.agent)
        self.assertFalse(self.migration.cleanup_stale(source))
        self.issues.end_stream(source,'topic',active['stream_token'])
        self.assertTrue(self.migration.cleanup_stale(source))
        self.assertFalse(self.issues._path(source,'topic').exists())
        self.assertEqual(len(self.rows('SELECT * FROM points_lots')),1)
        self.assertEqual(len(self.rows('SELECT * FROM points_guests')),1)
        with self.assertRaises(PointsError): self.migration.cleanup_stale(17)

    def test_deleted_prepared_topic_is_skipped_instead_of_revived(self):
        self.save(tid='deleted')
        token=self.issues.begin_temporary_stream(self.guest['storage_id'],'temporary')
        result=self.migration.claim(self.guest,self.user,'claim-deleted-map-01')
        with self.migration._conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            job=self.migration._job(conn,result['job_id'],self.user)
            self.migration._mapping(conn,job,'deleted')
        # Models a deletion already authenticated before the claim commit.
        self.issues.delete(self.guest['storage_id'],'deleted',self.agent)
        self.issues.end_stream(self.guest['storage_id'],'temporary',token)
        final=self.migration.status(result['job_id'],self.user)
        self.assertEqual(final['state'],'committed'); self.assertEqual(final['thread_map'],{})
        self.assertEqual(self.issues.list(17,self.agent)['total'],0)


if __name__=='__main__': unittest.main()
