from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from agent.points import PointsStore, PointsError, MICRO, normalize_usage, cost_to_micro


class PointsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='jiaoyuan-points-')
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name)/'billing.db'
        self.now = datetime(2026,10,27,8,0,tzinfo=timezone.utc).timestamp()
        self.store = PointsStore(self.db, clock=lambda:self.now)
        self.store.configure({'daily_cash_limit_cny':'10'})
        self.price = self.publish()
        self.bootstrap = self.store.bootstrap()
        self.guest = self.bootstrap['actor']

    def publish(self, **extra):
        payload = {'gateway':'https://provider.test/v1','model':'test-model',
                   'rates':{'input_uncached':'2','cache_read':'0.5','cache_write':'2','billable_output':'8'}}
        payload.update(extra)
        return self.store.publish_price(payload)

    def request(self, actor=None, *, input_tokens=1000, output_tokens=100, max_calls=2, key='request-01', text='test', extra='0'):
        actor = actor or self.guest
        digest = hashlib.sha256(text.encode()).hexdigest()
        q = self.store.quote(actor,digest,model='test-model',gateway='https://provider.test/v1',input_tokens=input_tokens,output_tokens=output_tokens,max_calls=max_calls,cash_extra_cny=extra)
        return self.store.reserve(actor,q['quote_id'],key,digest), q

    def call(self, req, *, call_id='call-0001', input_tokens=1000, output_tokens=100, usage=None, retry=False):
        self.store.before_call(req['request_id'],call_id,input_tokens,output_tokens,is_retry=retry)
        usage = {'prompt_tokens':input_tokens,'completion_tokens':output_tokens} if usage is None else usage
        return self.store.record_call(req['request_id'],call_id,usage)

    def rows(self, query, args=()):
        with sqlite3.connect(self.db) as conn:
            conn.row_factory=sqlite3.Row
            return [dict(r) for r in conn.execute(query,args)]

    def assertAudit(self):
        self.assertTrue(self.store.audit()['ok'])

    def test_bootstrap_does_not_grant_or_store_raw_credentials(self):
        self.assertEqual(self.store.wallet(self.guest)['available'],'0.000000')
        self.assertLess(self.guest['storage_id'],0)
        for table in ['points_guests','points_browsers','points_wallet_capabilities']:
            text=str(self.rows('SELECT * FROM '+table))
            for key in ('guest_token','browser_token','wallet_token'):
                self.assertNotIn(self.bootstrap[key],text)

    def test_guest_grant_once_and_workspaces_share_wallet_only_with_capability(self):
        self.store.grant_guest(self.guest)
        self.store.grant_guest(self.guest)
        b=self.store.bootstrap(browser_token=self.bootstrap['browser_token'],wallet_token=self.bootstrap['wallet_token'],rotate_workspace=True)
        self.assertNotEqual(b['actor']['storage_id'],self.guest['storage_id'])
        self.assertEqual(self.store.grant_guest(b['actor'])['available'],'100.000000')
        self.assertEqual(len(self.rows('SELECT * FROM points_lots')),1)
        self.assertAudit()

    def test_dedupe_marker_cannot_recover_money_or_create_second_grant(self):
        self.store.grant_guest(self.guest)
        b=self.store.bootstrap(browser_token=self.bootstrap['browser_token'])
        self.assertEqual(self.store.wallet(b['actor'])['available'],'0.000000')
        self.assertEqual(self.store.grant_guest(b['actor'])['available'],'0.000000')
        self.assertIsNone(self.store.resolve_guest(self.bootstrap['browser_token']))
        self.assertIsNone(self.store.resolve_wallet(self.bootstrap['browser_token']))

    def test_idle_guest_content_cannot_be_revived_by_bootstrap(self):
        self.now+=181*86400
        self.assertIsNone(self.store.resolve_guest(self.bootstrap['guest_token']))
        rotated=self.store.bootstrap(guest_token=self.bootstrap['guest_token'],browser_token=self.bootstrap['browser_token'],wallet_token=self.bootstrap['wallet_token'])
        self.assertNotEqual(rotated['actor']['storage_id'],self.guest['storage_id'])

    def test_parallel_first_grants_are_unique_across_instances(self):
        def grant(_):
            return PointsStore(self.db,clock=lambda:self.now).grant_guest(self.guest)['available']
        with ThreadPoolExecutor(max_workers=6) as pool:
            self.assertEqual(set(pool.map(grant,range(12))),{'100.000000'})
        self.assertEqual(len(self.rows('SELECT * FROM points_lots')),1)
        self.assertAudit()

    def test_daily_grant_no_signup_double_grant_or_historic_catchup(self):
        user=self.store.user_actor(1)
        self.assertEqual(self.store.wallet(user)['available'],'100.000000')
        self.assertEqual(self.store.wallet(user)['available'],'100.000000')
        self.now += 5*86400
        self.assertEqual(self.store.wallet(user)['available'],'100.000000')
        self.assertEqual(len(self.rows('SELECT * FROM points_lots')),2)
        self.assertAudit()

    def test_daily_grant_date_and_expiry_share_one_clock_snapshot_across_midnight(self):
        user=self.store.user_actor(1)
        before=datetime(2026,10,27,15,59,59,tzinfo=timezone.utc).timestamp()
        after=datetime(2026,10,27,16,0,1,tzinfo=timezone.utc).timestamp()
        midnight=datetime(2026,10,27,16,tzinfo=timezone.utc).timestamp()
        # Credential lookups and ledger timestamps may cross midnight inside
        # one transaction; the date/expiry pair itself must stay consistent.
        ticks=iter([before,before])
        with self.store._tx() as conn:
            wallets=self.store._wallet_ids(conn,user)
            self.store.clock=lambda:next(ticks,after)
            self.store._daily(conn,user,wallets)
        first=self.rows('SELECT grant_key,expires FROM points_lots')[0]
        self.assertEqual(first['grant_key'],'daily:u:1:2026-10-27')
        self.assertEqual(first['expires'],midnight)
        self.store.clock=lambda:after
        wallet=self.store.wallet(user)
        self.assertEqual(wallet['available'],'100.000000')
        self.assertEqual(wallet['held_micro'],0)
        rows=self.rows('SELECT grant_key,available,expired FROM points_lots ORDER BY id')
        self.assertEqual(rows[0]['expired'],100*MICRO)
        self.assertEqual(rows[1]['grant_key'],'daily:u:1:2026-10-28')
        self.assertEqual(sum(row['available'] for row in rows),100*MICRO)
        self.assertAudit()

    def test_pending_eligibility_cannot_bypass_via_wallet_or_reserve(self):
        user=self.store.user_actor(1)
        self.store.set_benefit_pending(user)
        self.assertTrue(self.store.wallet(user)['benefit_pending'])
        self.assertEqual(self.store.wallet(user)['available'],'0.000000')
        with self.assertRaises(PointsError) as caught:
            self.request(user)
        self.assertEqual(caught.exception.status,402)
        self.store.set_benefit_pending(user,False)
        self.assertEqual(self.store.wallet(user)['available'],'100.000000')
        self.store.set_benefit_pending(self.guest)
        with self.assertRaises(PointsError):
            self.store.grant_guest(self.guest)

    def test_trial_expiry_fixed_and_user_daily_expiry_distinct(self):
        self.store.grant_guest(self.guest)
        expiry=self.store.wallet(self.guest)['lots'][0]['expires_at']
        self.now+=86400
        user=self.store.user_actor(1)
        self.store.claim_both(self.guest,user,'claim-0001')
        lots=self.store.wallet(user)['lots']
        self.assertEqual(next(x for x in lots if x['source']=='guest_trial')['expires_at'],expiry)
        self.now=expiry+1
        self.assertEqual(self.store.wallet(user)['available'],'100.000000')
        self.assertAudit()

    def test_real_earliest_expiration_precedes_daily_not_source_order(self):
        # Issue the trial exactly 180 days before tonight's 22:00 expiry.
        self.now=datetime(2026,10,27,14,tzinfo=timezone.utc).timestamp()-180*86400
        self.store.grant_guest(self.guest)
        self.now=datetime(2026,10,27,12,tzinfo=timezone.utc).timestamp()
        user=self.store.user_actor(1)
        self.store.wallet(user)
        self.store.claim_both(self.guest,user,'claim-0001')
        req,_=self.request(user)
        alloc=self.rows('SELECT l.source FROM points_allocations a JOIN points_lots l ON l.id=a.lot_id WHERE a.request_id=?',(req['request_id'],))
        self.assertEqual(alloc[0]['source'],'guest_trial')

    def test_actual_cache_example_is_44_points(self):
        self.publish(adapter='deepseek',cache_read_supported=True)
        self.store.grant_guest(self.guest)
        req,q=self.request(input_tokens=20000,output_tokens=2000,max_calls=1)
        self.call(req,input_tokens=20000,output_tokens=2000,usage={'prompt_tokens':20000,'prompt_cache_hit_tokens':8000,'prompt_cache_miss_tokens':12000,'completion_tokens':2000,'total_tokens':22000,'prompt_tokens_details':{'cached_tokens':8000},'completion_tokens_details':{'reasoning_tokens':500}})
        result=self.store.settle(req['request_id'])
        self.assertEqual(result['charged_points'],'44.000000')
        self.assertEqual(Decimal(result['supplier_cost_cny']),Decimal('.044'))
        self.assertEqual(self.store.wallet(self.guest)['available'],'56.000000')
        self.assertAudit()

    def test_small_costs_are_not_rounded_to_one_point(self):
        self.store.grant_guest(self.guest)
        req,_=self.request(input_tokens=200,output_tokens=0,max_calls=1)
        self.call(req,input_tokens=200,output_tokens=0)
        self.assertEqual(self.store.settle(req['request_id'])['charged_points'],'0.400000')

    def test_round_only_after_aggregating_request(self):
        self.publish(rates={'input_uncached':'0.0003','cache_read':'0','cache_write':'0','billable_output':'0'})
        self.store.grant_guest(self.guest)
        req,_=self.request(input_tokens=1,output_tokens=0,max_calls=2)
        self.call(req,call_id='call-0001',input_tokens=1,output_tokens=0)
        self.call(req,call_id='call-0002',input_tokens=1,output_tokens=0)
        self.assertEqual(self.store.settle(req['request_id'])['charged_micro'],1)
        self.assertEqual(cost_to_micro(Decimal('.0000000004')),1)
        self.assertAudit()

    def test_two_concurrent_reservations_cannot_double_spend(self):
        self.store.grant_guest(self.guest)
        digest='a'*64
        quotes=[self.store.quote(self.guest,digest,model='test-model',gateway='https://provider.test/v1',input_tokens=30000,output_tokens=0,max_calls=1) for _ in range(2)]
        def reserve(i):
            try:
                return self.store.reserve(self.guest,quotes[i]['quote_id'],'request-0'+str(i),digest)['state']
            except PointsError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(reserve,range(2)))
        self.assertEqual(outcomes.count('reserved'),1)
        self.assertEqual(self.store.wallet(self.guest)['held'],'60.000000')
        self.assertAudit()

    def test_reserve_replay_is_idempotent_but_different_payload_is_rejected(self):
        self.store.grant_guest(self.guest)
        req,q=self.request()
        again=self.store.reserve(self.guest,q['quote_id'],'request-01',hashlib.sha256(b'test').hexdigest())
        self.assertTrue(again['duplicate'])
        self.assertEqual(req['request_id'],again['request_id'])
        with self.assertRaises(PointsError) as caught:
            self.store.reserve(self.guest,q['quote_id'],'request-01','b'*64)
        self.assertEqual(caught.exception.code,'idempotency_conflict')
        self.assertAudit()

    def test_no_call_replay_or_dispatch_after_settlement(self):
        self.store.grant_guest(self.guest)
        req,_=self.request()
        self.call(req)
        with self.assertRaises(PointsError):
            self.store.before_call(req['request_id'],'call-0001',1000,100)
        self.store.settle(req['request_id'])
        with self.assertRaises(PointsError):
            self.store.before_call(req['request_id'],'call-0002',1000,100)

    def test_call_bounds_and_model_match_are_checked_before_dispatch(self):
        self.store.grant_guest(self.guest)
        req,_=self.request(max_calls=1)
        for values in [{'input_tokens':1001,'output_tokens':100},{'input_tokens':1000,'output_tokens':101},{'input_tokens':1000,'output_tokens':100,'model':'other'}]:
            with self.assertRaises(PointsError):
                self.store.before_call(req['request_id'],'call-0001',**values)
        self.call(req)
        with self.assertRaises(PointsError):
            self.store.before_call(req['request_id'],'call-0002',1000,100)

    def test_upstream_overage_is_capped_and_blocks_further_calls(self):
        self.store.grant_guest(self.guest)
        req,q=self.request(input_tokens=10,output_tokens=0,max_calls=2)
        self.call(req,input_tokens=10,output_tokens=0,usage={'prompt_tokens':1000,'completion_tokens':0})
        with self.assertRaises(PointsError):
            self.store.before_call(req['request_id'],'call-0002',10,0)
        result=self.store.settle(req['request_id'])
        self.assertEqual(result['charged_points'],q['max_points'])
        self.assertTrue(result['over_budget'])
        self.assertGreater(Decimal(result['platform_cost_cny']),0)
        self.assertAudit()

    def test_system_failure_refunds_holds_and_keeps_supplier_cost(self):
        self.store.grant_guest(self.guest)
        req,_=self.request()
        self.call(req)
        result=self.store.settle(req['request_id'],delivered=False,failure=True)
        self.assertEqual(result['charged_points'],'0.000000')
        self.assertEqual(self.store.wallet(self.guest)['available'],'100.000000')
        self.assertEqual(result['supplier_cost_cny'],result['platform_cost_cny'])
        self.assertAudit()

    def test_user_stop_charges_verified_cost_but_call_before_stop_is_zero(self):
        self.store.grant_guest(self.guest)
        req,_=self.request()
        self.call(req)
        result=self.store.settle(req['request_id'],delivered=False,cancelled=True)
        self.assertEqual(result['charged_points'],'2.800000')
        req,_=self.request(key='request-02')
        self.assertEqual(self.store.settle(req['request_id'],delivered=False,cancelled=True)['charged_micro'],0)

    def test_retry_cost_platform_funded(self):
        self.store.grant_guest(self.guest)
        req,_=self.request()
        self.call(req,call_id='call-0001',retry=True)
        self.call(req,call_id='call-0002')
        result=self.store.settle(req['request_id'])
        self.assertEqual(result['charged_points'],'2.800000')
        self.assertEqual(Decimal(result['supplier_cost_cny']),Decimal('.0056'))
        self.assertEqual(Decimal(result['platform_cost_cny']),Decimal('.0028'))

    def test_missing_usage_holds_then_deadline_releases_user_only(self):
        self.store.grant_guest(self.guest)
        req,_=self.request()
        self.call(req,usage={})
        result=self.store.settle(req['request_id'])
        self.assertEqual(result['state'],'usage_pending')
        self.assertGreater(self.store.wallet(self.guest)['held_micro'],0)
        promised=Decimal(self.store.summary()['cash_committed_cny'])
        self.now+=86401
        self.assertEqual(self.store.wallet(self.guest)['available'],'100.000000')
        result=self.store.request_status(self.guest,req['request_id'])
        self.assertEqual(result['state'],'usage_released')
        # The original day's dispatched unknown promise remains recorded.
        self.assertTrue(self.rows("SELECT * FROM points_calls WHERE state='unknown'"))
        self.assertEqual(Decimal(self.rows('SELECT bound_cost FROM points_calls')[0]['bound_cost']),Decimal('.0028'))
        self.store.record_call(req['request_id'],'call-0001',{'prompt_tokens':900,'completion_tokens':90})
        late=self.store.request_status(self.guest,req['request_id'])
        self.assertEqual(late['charged_micro'],0)
        self.assertEqual(Decimal(late['user_cost_cny']),Decimal('.00252'))
        self.assertEqual(Decimal(late['supplier_cost_cny']),Decimal('.00252'))
        self.assertEqual(Decimal(late['platform_cost_cny']),Decimal('.00252'))
        self.assertEqual(self.store.wallet(self.guest)['available'],'100.000000')
        self.assertAudit()

    def test_reconcile_unknown_before_deadline_settles_once(self):
        self.store.grant_guest(self.guest)
        req,_=self.request()
        self.call(req,usage={})
        self.store.settle(req['request_id'])
        self.store.record_call(req['request_id'],'call-0001',{'prompt_tokens':900,'completion_tokens':90})
        result=self.store.request_status(self.guest,req['request_id'])
        self.assertEqual(result['state'],'settled')
        self.assertEqual(result['charged_points'],'2.520000')
        self.store.settle(req['request_id'])
        self.assertEqual(self.store.wallet(self.guest)['available'],'97.480000')
        self.assertAudit()

    def test_persistent_incomplete_usage_disables_model(self):
        self.store.grant_guest(self.guest)
        req,_=self.request(max_calls=3)
        for i in range(3):
            self.call(req,call_id='call-000'+str(i),usage={})
        self.assertEqual(self.store.prices()[0]['status'],'disabled')
        self.store.settle(req['request_id'],failure=True,delivered=False)
        with self.assertRaises(PointsError) as caught:
            self.request(key='request-02')
        self.assertEqual(caught.exception.code,'price_unavailable')

    def test_known_usage_replay_does_not_charge_twice_or_allow_overwrite(self):
        self.store.grant_guest(self.guest)
        req,_=self.request()
        self.call(req)
        result=self.store.record_call(req['request_id'],'call-0001',{'prompt_tokens':1000,'completion_tokens':100})
        self.assertTrue(result['duplicate'])
        with self.assertRaises(PointsError):
            self.store.record_call(req['request_id'],'call-0001',{'prompt_tokens':999,'completion_tokens':100})
        self.store.settle(req['request_id']); self.store.settle(req['request_id'])
        self.assertEqual(len(self.rows("SELECT * FROM points_ledger WHERE event='settle'")),1)
        self.assertAudit()

    def test_orphan_worker_recovery_releases_unused_hold_but_preserves_unknown_cash(self):
        self.store.grant_guest(self.guest)
        req,_=self.request()
        self.now+=901
        self.assertEqual(self.store.wallet(self.guest)['available'],'100.000000')
        self.assertEqual(self.store.request_status(self.guest,req['request_id'])['state'],'platform_absorbed')
        req,_=self.request(key='request-02')
        self.store.before_call(req['request_id'],'call-0001',1000,100)
        self.now+=901
        recovered=PointsStore(self.db,clock=lambda:self.now).recover_stale_requests()
        self.assertEqual(recovered['recovered_requests'],1)
        self.assertEqual(self.store.request_status(self.guest,req['request_id'])['state'],'usage_pending')
        self.store.record_call(req['request_id'],'call-0001',{'prompt_tokens':900,'completion_tokens':90})
        self.assertEqual(self.store.request_status(self.guest,req['request_id'])['charged_micro'],0)
        self.assertEqual(self.store.wallet(self.guest)['available'],'100.000000')
        self.assertAudit()

    def test_worker_heartbeat_prevents_stale_recovery(self):
        self.store.grant_guest(self.guest)
        req,_=self.request()
        self.now+=800;self.store.heartbeat(req['request_id']);self.now+=800
        self.assertEqual(self.store.recover_stale_requests()['recovered_requests'],0)
        self.assertEqual(self.store.request_status(self.guest,req['request_id'])['state'],'reserved')

    def test_provider_request_id_cannot_be_reused_on_other_call(self):
        self.store.grant_guest(self.guest)
        req,_=self.request()
        self.store.before_call(req['request_id'],'call-0001',1000,100)
        self.store.record_call(req['request_id'],'call-0001',{'prompt_tokens':1000,'completion_tokens':100},provider_request_id='vendor-001')
        self.store.before_call(req['request_id'],'call-0002',1000,100)
        with self.assertRaises(PointsError) as caught:
            self.store.record_call(req['request_id'],'call-0002',{'prompt_tokens':1000,'completion_tokens':100},provider_request_id='vendor-001')
        self.assertEqual(caught.exception.code,'provider_call_duplicate')

    def test_midnight_old_hold_releases_to_expired_not_new_day(self):
        self.now=datetime(2026,10,27,15,59,tzinfo=timezone.utc).timestamp()
        user=self.store.user_actor(1)
        self.store.wallet(user)
        req,_=self.request(user,input_tokens=20000,output_tokens=0,max_calls=1)
        self.store.before_call(req['request_id'],'call-0001',20000,0)
        self.now+=120
        self.assertEqual(self.store.wallet(user)['available'],'100.000000')
        self.store.record_call(req['request_id'],'call-0001',{'prompt_tokens':15000,'completion_tokens':0})
        self.store.settle(req['request_id'])
        self.assertEqual(self.store.wallet(user)['available'],'100.000000')
        old=self.store.wallet(user)['lots'][0]
        self.assertEqual(old['spent'],'30.000000')
        self.assertEqual(old['expired'],'70.000000')
        self.assertAudit()

    def test_claim_migrates_inflight_holds_without_regrant_or_rebilling(self):
        self.store.grant_guest(self.guest)
        req,_=self.request(input_tokens=30000,output_tokens=0,max_calls=1)
        self.call(req,input_tokens=30000,output_tokens=0)
        self.store.settle(req['request_id'])
        req,_=self.request(input_tokens=10000,output_tokens=0,max_calls=1,key='request-02')
        self.store.before_call(req['request_id'],'call-0001',10000,0)
        user=self.store.user_actor(1)
        self.store.wallet(user)
        claim=self.store.claim_both(self.guest,user,'claim-0001')
        self.assertTrue(claim['wallet_claimed'])
        self.assertEqual(self.store.wallet(user)['available'],'120.000000')
        self.assertEqual(self.store.wallet(user)['held'],'20.000000')
        self.assertIsNone(self.store.resolve_guest(self.bootstrap['guest_token']))
        self.assertIsNone(self.store.resolve_wallet(self.bootstrap['wallet_token']))
        self.store.record_call(req['request_id'],'call-0001',{'prompt_tokens':6250,'completion_tokens':0})
        self.store.settle(req['request_id'])
        self.assertEqual(self.store.wallet(user)['available'],'127.500000')
        self.assertEqual(self.store.claim_both(self.guest,user,'claim-0001'),claim)
        self.assertEqual(self.store.content_owner(self.guest['storage_id']),1)
        self.assertAudit()

    def test_account_claim_once_but_content_can_move_without_confiscating_wallet(self):
        self.store.grant_guest(self.guest)
        user=self.store.user_actor(1)
        self.store.claim_both(self.guest,user,'claim-0001')
        second=self.store.bootstrap(); self.store.grant_guest(second['actor'])
        result=self.store.claim_both(second['actor'],user,'claim-0002')
        self.assertFalse(result['wallet_claimed'])
        self.assertTrue(result['content_claimed'])
        wallet_actor=self.store.resolve_wallet(second['wallet_token'])
        self.assertIsNotNone(wallet_actor)
        self.assertEqual(self.store.wallet(wallet_actor)['available'],'100.000000')
        with self.assertRaises(PointsError):
            self.request(wallet_actor)
        empty_space=self.store.bootstrap(browser_token=second['browser_token'],wallet_token=second['wallet_token'])
        self.assertNotEqual(empty_space['actor']['storage_id'],second['actor']['storage_id'])
        self.assertEqual(self.store.grant_guest(empty_space['actor'])['available'],'100.000000')
        self.assertEqual(self.store.wallet(user)['available'],'200.000000')
        self.assertAudit()

    def test_different_accounts_cannot_claim_same_content_or_wallet(self):
        self.store.grant_guest(self.guest)
        first=self.store.user_actor(1); second=self.store.user_actor(2)
        self.store.claim_both(self.guest,first,'claim-0001')
        with self.assertRaises(PointsError):
            self.store.claim_both(self.guest,second,'claim-0002')
        self.assertEqual(self.store.content_owner(self.guest['storage_id']),1)
        self.assertEqual(self.store.wallet(second)['available'],'100.000000')

    def test_status_and_ledger_are_wallet_authorized_and_paginated(self):
        self.store.grant_guest(self.guest)
        req,_=self.request()
        other=self.store.bootstrap()['actor']
        with self.assertRaises(PointsError) as caught:
            self.store.request_status(other,req['request_id'])
        self.assertEqual(caught.exception.status,404)
        first=self.store.ledger(self.guest,limit=1)
        self.assertIsNotNone(first['next_cursor'])
        second=self.store.ledger(self.guest,limit=1,cursor=first['next_cursor'])
        self.assertNotEqual(first['items'][0]['id'],second['items'][0]['id'])
        for bad in [0,-1,201,True,'2']:
            with self.assertRaises(PointsError):
                self.store.ledger(self.guest,limit=bad)

    def test_price_snapshots_survive_normal_publication_but_not_emergency_disable(self):
        self.store.grant_guest(self.guest)
        digest='a'*64
        q=self.store.quote(self.guest,digest,model='test-model',gateway='https://provider.test/v1',input_tokens=1000,output_tokens=100,max_calls=1)
        self.now+=1
        self.publish(rates={'input_uncached':'5','cache_read':'1','cache_write':'5','billable_output':'20'})
        req=self.store.reserve(self.guest,q['quote_id'],'request-01',digest)
        self.call(req)
        self.assertEqual(self.store.settle(req['request_id'])['charged_points'],'2.800000')
        q=self.store.quote(self.guest,digest,model='test-model',gateway='https://provider.test/v1',input_tokens=1000,output_tokens=100,max_calls=1)
        self.store.disable_price(q['price']['id'])
        with self.assertRaises(PointsError):
            self.store.reserve(self.guest,q['quote_id'],'request-02',digest)

    def test_stale_quote_or_missing_price_fails_closed(self):
        self.store.grant_guest(self.guest)
        digest='a'*64
        q=self.store.quote(self.guest,digest,model='test-model',gateway='https://provider.test/v1',input_tokens=1000,output_tokens=100,max_calls=1)
        self.now+=301
        with self.assertRaises(PointsError):
            self.store.reserve(self.guest,q['quote_id'],'request-01',digest)
        with self.assertRaises(PointsError):
            self.store.quote(self.guest,digest,model='missing',gateway='https://provider.test/v1',input_tokens=1,output_tokens=1)

    def test_unused_quote_per_principal_cap_and_ttl_do_not_grow_without_bound(self):
        def quote():
            return self.store.quote(self.guest,'a'*64,model='test-model',gateway='https://provider.test/v1',input_tokens=1000,output_tokens=100,max_calls=1)
        offers=[quote() for _ in range(10)]
        with self.assertRaises(PointsError) as caught:quote()
        self.assertEqual(caught.exception.status,429)
        self.assertEqual(caught.exception.code,'quote_limit')
        self.assertEqual(len(self.rows('SELECT * FROM points_quotes')),10)
        self.assertEqual(self.store.wallet(self.guest)['available'],'0.000000')
        self.now+=300
        fresh=quote()
        rows=self.rows('SELECT * FROM points_quotes')
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['id'],fresh['quote_id'])
        self.assertNotIn(fresh['quote_id'],[q['quote_id'] for q in offers])

    def test_global_quote_capacity_rejects_creation_but_existing_quote_can_start(self):
        self.store.grant_guest(self.guest)
        quote=self.store.quote(self.guest,'a'*64,model='test-model',gateway='https://provider.test/v1',input_tokens=1000,output_tokens=100,max_calls=1)
        template=self.rows('SELECT * FROM points_quotes WHERE id=?',(quote['quote_id'],))[0]
        columns=list(template)
        # Seed exactly the configured global number of independent pending
        # offers without thousands of unrelated HTTP/account operations.
        with sqlite3.connect(self.db) as conn:
            conn.executemany('INSERT INTO points_quotes('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',
                [tuple(('capacity-'+str(i)) if k=='id' else ('u:'+str(i+2)) if k=='principal' else template[k] for k in columns) for i in range(9999)])
        with self.assertRaises(PointsError) as caught:
            self.store.quote(self.guest,'b'*64,model='test-model',gateway='https://provider.test/v1',input_tokens=1000,output_tokens=100,max_calls=1)
        self.assertEqual(caught.exception.status,503)
        self.assertEqual(caught.exception.code,'quote_capacity')
        self.assertEqual(self.rows('SELECT COUNT(*) AS n FROM points_quotes')[0]['n'],10000)
        req=self.store.reserve(self.guest,quote['quote_id'],'capacity-request','a'*64)
        self.assertEqual(req['state'],'reserved')
        self.assertEqual(self.rows('SELECT price_id FROM points_quotes WHERE id=?',(quote['quote_id'],))[0]['price_id'],quote['price']['id'])

    def test_cleanup_deletes_expired_unused_quotes_but_preserves_reserved_price_snapshot(self):
        self.store.grant_guest(self.guest)
        unused=self.store.quote(self.guest,'a'*64,model='test-model',gateway='https://provider.test/v1',input_tokens=1000,output_tokens=100,max_calls=1)
        req,used=self.request(max_calls=1)
        self.now+=301
        result=self.store.cleanup()
        self.assertEqual(result['expired_quotes_deleted'],1)
        self.assertFalse(self.rows('SELECT id FROM points_quotes WHERE id=?',(unused['quote_id'],)))
        self.assertTrue(self.rows('SELECT id FROM points_quotes WHERE id=?',(used['quote_id'],)))
        self.call(req)
        self.assertEqual(self.store.settle(req['request_id'])['charged_points'],'2.800000')

    def test_rebuild_repairs_only_projections_from_grants_refunds_expiry_and_transfer(self):
        self.store.grant_guest(self.guest)
        req,_=self.request(max_calls=1)
        self.call(req)
        self.store.settle(req['request_id'])
        self.store.refund(req['request_id'],'refund-rebuild-01','核实后退款',admin_id=1)
        self.store.adjust(self.guest,'-1','adjust-rebuild-01','追回重复积分',admin_id=1)
        self.store.adjust(self.guest,'4','adjust-rebuild-02','短期补偿积分',admin_id=1,expires_at=self.now+1)
        self.now+=2
        self.store.wallet(self.guest)
        user=self.store.user_actor(1)
        self.store.claim_guest(self.guest,user,'claim-rebuild-01')
        expected=self.rows('SELECT * FROM points_lots ORDER BY id')
        ledger=self.rows('SELECT * FROM points_ledger ORDER BY id')
        with self.store._tx() as conn:
            conn.execute('UPDATE points_lots SET available=available-1000000,held=held+1000000 WHERE id=?',(expected[0]['id'],))
        result=self.store.rebuild_projection(admin_id=1)
        self.assertEqual(result['repaired_lots'],1)
        self.assertEqual(result['checked_lots'],2)
        self.assertEqual(result['requests_checked'],1)
        self.assertEqual(self.rows('SELECT * FROM points_lots ORDER BY id'),expected)
        self.assertEqual(self.rows('SELECT * FROM points_ledger ORDER BY id'),ledger)
        self.assertEqual(self.store.rebuild_projection(admin_id=1)['repaired_lots'],0)
        self.assertAudit()

    def test_rebuild_refuses_every_active_state_without_recovery_or_expiry_side_effects(self):
        self.store.grant_guest(self.guest)
        req,_=self.request(max_calls=1)
        for state in ('reserved','running','usage_pending'):
            self.assertEqual(self.rows('SELECT state FROM points_requests')[0]['state'],state)
            lots=self.rows('SELECT * FROM points_lots')
            ledger=self.rows('SELECT * FROM points_ledger')
            with self.assertRaises(PointsError) as caught:self.store.rebuild_projection(admin_id=1)
            self.assertEqual(caught.exception.code,'projection_busy')
            self.assertEqual(self.rows('SELECT * FROM points_lots'),lots)
            self.assertEqual(self.rows('SELECT * FROM points_ledger'),ledger)
            if state=='reserved':self.store.before_call(req['request_id'],'call-0001',1000,100)
            elif state=='running':
                self.store.record_call(req['request_id'],'call-0001',{})
                self.store.settle(req['request_id'])
        self.now+=86401
        with self.assertRaises(PointsError) as caught:self.store.rebuild_projection(admin_id=1)
        self.assertEqual(caught.exception.code,'projection_busy')
        self.store.wallet(self.guest)
        cash=self.rows('SELECT * FROM points_cash_commitments')
        self.assertEqual(self.store.rebuild_projection(admin_id=1)['repaired_lots'],0)
        self.assertEqual(self.rows('SELECT * FROM points_cash_commitments'),cash)

    def test_rebuild_rejects_bad_ledger_and_rolls_back_all_projection_repairs(self):
        self.store.grant_guest(self.guest)
        self.store.adjust(self.guest,'10','adjust-rebuild-01','人工补偿积分',admin_id=1,expires_at=self.now+3600)
        lots=self.rows('SELECT * FROM points_lots ORDER BY id')
        with self.store._tx() as conn:
            conn.execute('UPDATE points_lots SET available=available-1000000,spent=spent+1000000 WHERE id=?',(lots[0]['id'],))
            self.store._entry(conn,'corrupt-ledger-01',lots[1]['wallet_id'],lots[1]['id'],'expire',20*MICRO,available=-20*MICRO,expired=20*MICRO)
        corrupted=self.rows('SELECT * FROM points_lots ORDER BY id')
        ledger=self.rows('SELECT * FROM points_ledger ORDER BY id')
        with self.assertRaises(PointsError) as caught:self.store.rebuild_projection(admin_id=1)
        self.assertEqual(caught.exception.code,'projection_source_invalid')
        self.assertEqual(self.rows('SELECT * FROM points_lots ORDER BY id'),corrupted)
        self.assertEqual(self.rows('SELECT * FROM points_ledger ORDER BY id'),ledger)

    def test_rebuild_refuses_allocation_and_final_request_inconsistency(self):
        self.store.grant_guest(self.guest)
        req,_=self.request(max_calls=1)
        self.call(req)
        self.store.settle(req['request_id'])
        lots=self.rows('SELECT * FROM points_lots')
        with self.store._tx() as conn:
            conn.execute('UPDATE points_allocations SET charged=charged-1')
        with self.assertRaises(PointsError) as caught:self.store.rebuild_projection(admin_id=1)
        self.assertEqual(caught.exception.code,'projection_source_invalid')
        self.assertEqual(self.rows('SELECT * FROM points_lots'),lots)
        with self.store._tx() as conn:
            conn.execute('UPDATE points_allocations SET charged=charged+1')
            conn.execute("UPDATE points_requests SET state='usage_released'")
        with self.assertRaises(PointsError) as caught:self.store.rebuild_projection(admin_id=1)
        self.assertEqual(caught.exception.code,'projection_source_invalid')
        self.assertEqual(self.rows('SELECT * FROM points_lots'),lots)
        with self.store._tx() as conn:
            conn.execute("UPDATE points_requests SET state='settled'")
            conn.execute("UPDATE points_calls SET state='unknown'")
        with self.assertRaises(PointsError) as caught:self.store.rebuild_projection(admin_id=1)
        self.assertEqual(caught.exception.code,'projection_source_invalid')
        self.assertEqual(self.rows('SELECT * FROM points_lots'),lots)

    def test_cash_budget_race_is_atomic_across_wallets(self):
        self.store.configure({'daily_cash_limit_cny':'0.003'})
        guests=[self.guest,self.store.bootstrap()['actor']]
        for g in guests:self.store.grant_guest(g)
        digest='a'*64
        quotes=[self.store.quote(g,digest,model='test-model',gateway='https://provider.test/v1',input_tokens=1000,output_tokens=100,max_calls=1) for g in guests]
        def reserve(i):
            try:
                self.store.reserve(guests[i],quotes[i]['quote_id'],'request-0'+str(i),digest)
                return True
            except PointsError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(reserve,range(2))),1)
        self.assertEqual(Decimal(self.store.summary()['cash_committed_cny']),Decimal('.0028'))

    def test_cash_parent_child_promises_are_not_double_counted(self):
        self.store.grant_guest(self.guest)
        req,_=self.request()
        before=Decimal(self.store.summary()['cash_committed_cny'])
        self.store.before_call(req['request_id'],'call-0001',1000,100)
        self.assertEqual(Decimal(self.store.summary()['cash_committed_cny']),before)
        self.store.record_call(req['request_id'],'call-0001',{'prompt_tokens':900,'completion_tokens':90})
        self.assertEqual(Decimal(self.store.summary()['cash_committed_cny']),before)
        self.store.settle(req['request_id'])
        self.assertEqual(Decimal(self.store.summary()['cash_committed_cny']),Decimal('.00252'))

    def test_actual_model_overrun_blocks_other_reserved_wallet_until_next_day(self):
        self.store.configure({'daily_cash_limit_cny':'0.006'})
        other=self.store.bootstrap()['actor']
        for actor in (self.guest,other):self.store.grant_guest(actor)
        first,quote=self.request(max_calls=1,key='request-first')
        second,_=self.request(other,max_calls=1,key='request-second')
        self.call(first,usage={'prompt_tokens':1000,'completion_tokens':1000})
        summary=self.store.summary()
        self.assertEqual(Decimal(summary['cash_committed_cny']),Decimal('.0128'))
        self.assertTrue(summary['budget_exhausted'])
        promise=self.rows('SELECT * FROM points_cash_commitments WHERE request_id=?',(second['request_id'],))[0]
        with self.assertRaises(PointsError) as caught:
            self.store.before_call(second['request_id'],'call-second',1000,100)
        self.assertEqual((caught.exception.status,caught.exception.code),(503,'cash_budget_exhausted'))
        self.assertFalse(self.rows('SELECT * FROM points_calls WHERE request_id=?',(second['request_id'],)))
        self.assertEqual(self.rows('SELECT * FROM points_cash_commitments WHERE request_id=?',(second['request_id'],))[0],promise)
        result=self.store.settle(first['request_id'])
        self.assertEqual(result['charged_points'],'2.800000')
        self.assertEqual(Decimal(result['supplier_cost_cny']),Decimal('.010'))
        self.assertEqual(Decimal(result['platform_cost_cny']),Decimal('.0072'))
        self.assertEqual(self.rows('SELECT output_cap,max_micro FROM points_quotes WHERE id=?',(quote['quote_id'],))[0],{'output_cap':100,'max_micro':2800000})
        self.assertEqual(self.store.prices()[0]['status'],'published')
        self.now+=86400
        self.store.before_call(second['request_id'],'call-second',1000,100)
        self.assertEqual(Decimal(self.store.summary()['cash_committed_cny']),Decimal('.0028'))
        self.assertFalse(self.store.summary()['budget_exhausted'])
        self.store.record_call(second['request_id'],'call-second',{'prompt_tokens':1000,'completion_tokens':100})
        self.store.settle(second['request_id'])
        self.assertAudit()

    def test_actual_search_overrun_blocks_other_reserved_model_and_search_dispatch(self):
        self.store.configure({'daily_cash_limit_cny':'0.05'})
        other=self.store.bootstrap()['actor']
        for actor in (self.guest,other):self.store.grant_guest(actor)
        first,_=self.request(max_calls=1,extra='.01',key='request-first')
        second,_=self.request(other,max_calls=1,extra='.01',key='request-second')
        self.call(first)
        self.store.before_service_call(first['request_id'],'search-first','.01')
        self.store.record_service_call(first['request_id'],'search-first','.05')
        self.assertEqual(Decimal(self.store.summary()['cash_committed_cny']),Decimal('.0656'))
        for dispatch in (lambda:self.store.before_call(second['request_id'],'call-second',1000,100),
                         lambda:self.store.before_service_call(second['request_id'],'search-second','.01')):
            with self.assertRaises(PointsError) as caught:dispatch()
            self.assertEqual(caught.exception.code,'cash_budget_exhausted')
        self.assertFalse(self.rows('SELECT * FROM points_calls WHERE request_id=?',(second['request_id'],)))
        result=self.store.settle(first['request_id'])
        self.assertEqual(result['charged_points'],'2.800000')
        self.assertEqual(Decimal(self.store.summary()['supplier_cost_cny']),Decimal('.0528'))
        self.assertAudit()

    def test_exact_daily_limit_allows_existing_model_and_search_promises(self):
        self.store.configure({'daily_cash_limit_cny':'0.0156'})
        other=self.store.bootstrap()['actor']
        for actor in (self.guest,other):self.store.grant_guest(actor)
        first,_=self.request(max_calls=1,extra='.01',key='request-first')
        second,_=self.request(other,max_calls=1,key='request-second')
        self.assertEqual(Decimal(self.store.summary()['cash_committed_cny']),Decimal('.0156'))
        self.store.before_service_call(first['request_id'],'search-first','.01')
        self.call(first)
        self.call(second,call_id='call-second')
        self.assertEqual(Decimal(self.store.summary()['cash_committed_cny']),Decimal('.0156'))
        self.store.record_service_call(first['request_id'],'search-first','.01')
        self.store.settle(first['request_id'])
        self.store.settle(second['request_id'])
        self.assertAudit()

    def test_cross_day_reserved_transfer_rejects_destination_actual_overrun_atomically(self):
        self.now=datetime(2026,10,27,15,59,tzinfo=timezone.utc).timestamp()
        self.store.configure({'daily_cash_limit_cny':'0.006'})
        other=self.store.bootstrap()['actor']
        for actor in (self.guest,other):self.store.grant_guest(actor)
        pending,_=self.request(max_calls=1,key='request-pending')
        promise=self.rows('SELECT * FROM points_cash_commitments WHERE request_id=?',(pending['request_id'],))[0]
        self.now+=120
        current,_=self.request(other,max_calls=1,key='request-current')
        self.call(current,usage={'prompt_tokens':1000,'completion_tokens':1000})
        with self.assertRaises(PointsError) as caught:
            self.store.before_call(pending['request_id'],'call-pending',1000,100)
        self.assertEqual(caught.exception.code,'cash_budget_exhausted')
        self.assertEqual(self.rows('SELECT * FROM points_cash_commitments WHERE request_id=?',(pending['request_id'],))[0],promise)
        self.assertFalse(self.rows('SELECT * FROM points_calls WHERE request_id=?',(pending['request_id'],)))
        self.assertEqual(Decimal(self.store.summary()['cash_committed_cny']),Decimal('.010'))
        self.assertAudit()

    def test_cross_midnight_budget_transfer_checks_destination_first(self):
        self.now=datetime(2026,10,27,15,59,tzinfo=timezone.utc).timestamp()
        self.store.grant_guest(self.guest)
        req,_=self.request()
        commitment=self.rows('SELECT * FROM points_cash_commitments')[0]
        self.now+=120
        self.store.configure({'daily_cash_limit_cny':'0.001'})
        with self.assertRaises(PointsError):
            self.store.before_call(req['request_id'],'call-0001',1000,100)
        self.assertEqual(self.rows('SELECT * FROM points_cash_commitments')[0],commitment)
        self.assertFalse(self.rows('SELECT * FROM points_calls'))

    def test_external_search_is_separate_platform_cost(self):
        self.store.grant_guest(self.guest)
        req,_=self.request(extra='0.01')
        self.store.before_service_call(req['request_id'],'search-01','0.005')
        self.store.record_service_call(req['request_id'],'search-01','0.004')
        self.call(req)
        result=self.store.settle(req['request_id'])
        self.assertEqual(result['charged_points'],'2.800000')
        self.assertEqual(Decimal(self.store.summary()['supplier_cost_cny']),Decimal('.0068'))
        with self.assertRaises(PointsError):
            self.store.before_service_call(req['request_id'],'search-02','0')

    def test_search_quote_total_scales_with_call_cap_without_bypassing_cash_budget(self):
        self.store.grant_guest(self.guest)
        kwargs={'model':'test-model','gateway':'https://provider.test/v1',
                'input_tokens':0,'output_tokens':0,'max_calls':3}
        quote=self.store.quote(self.guest,'a'*64,cash_extra_cny=str(500*3),**kwargs)
        snapshot=self.rows('SELECT extra_cash,cash_max FROM points_quotes WHERE id=?',(quote['quote_id'],))[0]
        self.assertEqual(Decimal(snapshot['extra_cash']),Decimal('1500'))
        self.assertEqual(Decimal(snapshot['cash_max']),Decimal('1500'))
        with self.assertRaises(PointsError):
            self.store.quote(self.guest,'b'*64,cash_extra_cny='3000.000000000001',**kwargs)
        self.assertEqual(len(self.rows('SELECT id FROM points_quotes')),1)
        with self.assertRaises(PointsError) as caught:
            self.store.reserve(self.guest,quote['quote_id'],'request-search-large','a'*64)
        self.assertEqual(caught.exception.code,'cash_budget_exhausted')
        self.assertFalse(self.rows('SELECT * FROM points_requests'))
        self.assertEqual(self.store.wallet(self.guest)['held_micro'],0)
        self.store.configure({'daily_cash_limit_cny':'1500'})
        request=self.store.reserve(self.guest,quote['quote_id'],'request-search-large','a'*64)
        self.assertEqual(Decimal(self.store.summary()['cash_committed_cny']),Decimal('1500'))
        self.store.before_service_call(request['request_id'],'search-large-01','500')
        with self.assertRaises(PointsError) as caught:
            self.store.before_service_call(request['request_id'],'search-large-02','1000.000000000001')
        self.assertEqual(caught.exception.status,400)
        self.assertEqual(len(self.rows('SELECT * FROM points_calls')),1)
        self.assertEqual(Decimal(self.store.summary()['cash_committed_cny']),Decimal('1500'))
        self.assertAudit()

    def test_refund_is_idempotent_and_retains_original_expiry(self):
        user=self.store.user_actor(1)
        self.store.wallet(user)
        req,_=self.request(user)
        self.call(req)
        self.store.settle(req['request_id'])
        self.now=self.store.wallet(user)['lots'][0]['expires_at']+1
        original=self.store.refund(req['request_id'],'refund-01','service correction',admin_id=1)
        self.assertEqual(self.store.refund(req['request_id'],'refund-01','service correction',admin_id=1),original)
        with self.assertRaises(PointsError):
            self.store.refund(req['request_id'],'refund-02','duplicate refund',admin_id=1)
        self.assertEqual(self.store.wallet(user)['available'],'100.000000')
        old=self.store.wallet(user)['lots'][0]
        self.assertEqual(old['expired'],'100.000000')
        self.assertEqual(old['spent'],'0.000000')
        self.assertAudit()

    def test_ledger_cannot_be_updated_and_projection_can_be_audited(self):
        self.store.grant_guest(self.guest)
        with sqlite3.connect(self.db) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE points_ledger SET amount=999")
        self.assertAudit()

    def test_policy_and_price_validation_rejects_float_unknown_and_negative(self):
        for bad in ['-1','NaN','Infinity','0',1.2,True]:
            with self.assertRaises(PointsError):self.store.configure({'daily_cash_limit_cny':bad})
        for extra in [{'currency':'USD'},{'rates':{'input_uncached':'2'}},{'adapter':'unknown'},{'gateway':'https://secret:password@example.test/v1'},{'rates':{'input_uncached':'-1','cache_read':'0','cache_write':'0','billable_output':'0'}}]:
            with self.assertRaises(PointsError):self.publish(**extra)
        with self.assertRaises(PointsError):self.store.user_actor(0)

    def test_missing_budget_fails_closed(self):
        blank=PointsStore(Path(self.tmp.name)/'blank.db',clock=lambda:self.now)
        actor=blank.bootstrap()['actor']; blank.grant_guest(actor)
        blank.publish_price({'gateway':'https://provider.test/v1','model':'test-model','rates':{k:'1' for k in ['input_uncached','cache_read','cache_write','billable_output']}})
        quote=blank.quote(actor,'a'*64,model='test-model',gateway='https://provider.test/v1',input_tokens=1,output_tokens=1)
        with self.assertRaises(PointsError) as caught:blank.reserve(actor,quote['quote_id'],'request-01','a'*64)
        self.assertEqual(caught.exception.code,'budget_unavailable')

    def test_expensive_cache_write_is_included_in_quote_bound(self):
        self.publish(rates={'input_uncached':'2','cache_read':'0.5','cache_write':'10','billable_output':'8'})
        self.store.grant_guest(self.guest)
        req,quote=self.request(input_tokens=1000,output_tokens=0,max_calls=1)
        self.assertEqual(quote['max_points'],'10.000000')
        self.store.settle(req['request_id'],failure=True)

    def test_disabling_latest_price_does_not_fall_back_to_superseded_price(self):
        latest=self.publish()
        self.store.disable_price(latest['id'])
        with self.assertRaises(PointsError) as caught:
            self.store.quote(self.guest,'a'*64,model='test-model',gateway='https://provider.test/v1',input_tokens=1,output_tokens=1)
        self.assertEqual(caught.exception.code,'price_unavailable')

    def test_admin_adjustment_needs_reason_review_and_cannot_spend_holds(self):
        self.store.grant_guest(self.guest)
        with self.assertRaises(PointsError):
            self.store.adjust(self.guest,'101','adjust-01','corrective credit',admin_id=1,expires_at=self.now+86400)
        self.store.adjust(self.guest,'101','adjust-01','corrective credit',admin_id=1,reviewed_by=2,expires_at=self.now+86400)
        self.assertEqual(self.store.wallet(self.guest)['available'],'201.000000')
        result=self.store.adjust(self.guest,'-100','adjust-02','corrective debit',admin_id=1)
        self.assertEqual(self.store.adjust(self.guest,'-100','adjust-02','corrective debit',admin_id=1),result)
        self.assertEqual(self.store.wallet(self.guest)['available'],'101.000000')
        req,_=self.request(input_tokens=50000,output_tokens=0,max_calls=1)
        with self.assertRaises(PointsError):
            self.store.adjust(self.guest,'-2','adjust-03','invalid hold debit',admin_id=1)
        self.assertEqual(self.store.wallet(self.guest)['held'],'100.000000')
        self.assertAudit()

    def test_financial_retention_preserves_lifetime_claim_flag_and_unknown_liability(self):
        self.store.grant_guest(self.guest)
        user=self.store.user_actor(1)
        self.store.claim_both(self.guest,user,'claim-0001')
        self.store.wallet(user)
        req,_=self.request(user)
        self.call(req)
        self.store.settle(req['request_id'])
        self.now+=361*86400
        result=self.store.cleanup()
        self.assertEqual(result['financial_requests_pruned'],1)
        self.assertEqual(self.rows('SELECT claim_used FROM points_principals WHERE principal=?',('u:1',))[0]['claim_used'],1)
        self.assertAudit()
        guest=self.store.bootstrap()['actor'];self.store.grant_guest(guest)
        req,_=self.request(guest,key='request-02')
        self.call(req,usage={});self.store.settle(req['request_id'])
        self.now+=361*86400
        self.store.cleanup()
        self.assertEqual(self.store.request_status(guest,req['request_id'])['state'],'usage_released')
        self.assertTrue(self.rows("SELECT * FROM points_calls WHERE state='unknown'"))
        self.assertAudit()


class UsageNormalizationTests(unittest.TestCase):
    def test_cache_aliases_are_consistent_not_added_twice(self):
        raw={'prompt_tokens':20000,'completion_tokens':2000,'prompt_cache_hit_tokens':8000,'prompt_cache_miss_tokens':12000,'prompt_tokens_details':{'cached_tokens':8000}}
        self.assertEqual(normalize_usage(raw,'deepseek',cache_read_supported=True),{'input_uncached':12000,'cache_read':8000,'cache_write':0,'billable_output':2000})
        raw['prompt_tokens_details']['cached_tokens']=9000
        self.assertIsNone(normalize_usage(raw,'deepseek',cache_read_supported=True))

    def test_anonymized_gateway_usage_keeps_prompt_counts_over_zero_aliases(self):
        # Count-only sample from a bounded live protocol check. No supplier
        # request ID, content, credential, or assumed monetary price is kept.
        raw={'prompt_tokens':1619,'completion_tokens':4,'total_tokens':1623,
             'prompt_cache_hit_tokens':1408,'input_tokens':0,'output_tokens':0,
             'claude_cache_creation_5_m_tokens':0,'claude_cache_creation_1_h_tokens':0,
             'prompt_tokens_details':{'cached_tokens':1408,'text_tokens':0,'audio_tokens':0,'image_tokens':0},
             'completion_tokens_details':{'text_tokens':0,'audio_tokens':0,'image_tokens':0,'reasoning_tokens':0}}
        self.assertEqual(normalize_usage(raw,'deepseek',cache_read_supported=True,cache_write_supported=False),
                         {'input_uncached':211,'cache_read':1408,'cache_write':0,'billable_output':4})
        self.assertIsNone(normalize_usage(raw,'anthropic',cache_read_supported=False,cache_write_supported=False))

    def test_unknown_not_zero_and_invalid_counts_rejected(self):
        for value in [None,{}, {'prompt_tokens':1}, {'prompt_tokens':-1,'completion_tokens':0}, {'prompt_tokens':True,'completion_tokens':0}, {'prompt_tokens':2,'completion_tokens':0,'total_tokens':3}]:
            self.assertIsNone(normalize_usage(value))
        self.assertEqual(normalize_usage({'prompt_tokens':0,'completion_tokens':0})['input_uncached'],0)
        self.assertIsNone(normalize_usage({'prompt_tokens':2,'completion_tokens':0},cache_read_supported=True))
        self.assertIsNone(normalize_usage({'prompt_tokens':2,'completion_tokens':0,'prompt_tokens_details':{'cached_tokens':1}}))

    def test_anthropic_buckets_are_disjoint(self):
        raw={'input_tokens':100,'output_tokens':20,'cache_read_input_tokens':50,'cache_creation_input_tokens':25}
        self.assertEqual(normalize_usage(raw,'anthropic',cache_read_supported=True,cache_write_supported=True),{'input_uncached':100,'cache_read':50,'cache_write':25,'billable_output':20})

    def test_anthropic_dual_format_aliases_must_match_all_disjoint_buckets(self):
        raw={'input_tokens':100,'output_tokens':20,'cache_read_input_tokens':50,'cache_creation_input_tokens':25,
             'prompt_tokens':175,'completion_tokens':20,'total_tokens':195}
        options={'cache_read_supported':True,'cache_write_supported':True}
        self.assertEqual(normalize_usage(raw,'anthropic',**options),
                         {'input_uncached':100,'cache_read':50,'cache_write':25,'billable_output':20})
        for alias in ('prompt_tokens','completion_tokens','total_tokens'):
            with self.subTest(alias=alias):
                self.assertIsNone(normalize_usage({**raw,alias:raw[alias]+1},'anthropic',**options))
                self.assertIsNone(normalize_usage({**raw,alias:True},'anthropic',**options))


if __name__=='__main__':unittest.main()
