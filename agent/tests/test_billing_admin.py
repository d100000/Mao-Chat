from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from agent.billing_admin import BillingAdminStore
from agent.points import PointsError, PointsStore, SCHEMA


class BillingAdminTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='jiaoyuan-admin-billing-')
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / 'billing.db'
        self.now = datetime(2026, 10, 27, 8, tzinfo=timezone.utc).timestamp()
        self.points = PointsStore(self.db, clock=lambda: self.now)
        self.admin = BillingAdminStore(self.points)
        self.points.configure({'daily_cash_limit_cny': '10'})
        self.actor = self.points.bootstrap()['actor']
        self.points.grant_guest(self.actor)
        self.payload = {'gateway': 'https://supplier.test/v1', 'model': 'fictional-test-model',
                        'rates': {'input_uncached': '2', 'cache_read': '.5', 'cache_write': '2', 'billable_output': '8'}}
        self.price = self.points.publish_price(self.payload)

    def rows(self, sql, args=()):
        with self.points._connect() as conn:
            return [dict(r) for r in conn.execute(sql, args)]

    def request(self, *, known=True, service=False, key='request-0001'):
        digest = hashlib.sha256(key.encode()).hexdigest()
        quote = self.points.quote(self.actor, digest, model=self.payload['model'], gateway=self.payload['gateway'],
                                  input_tokens=1000, output_tokens=100, max_calls=1, cash_extra_cny='.1' if service else '0')
        request = self.points.reserve(self.actor, quote['quote_id'], key, digest)
        rid = request['request_id']
        self.points.before_call(rid, 'model-call-0001', 1000, 100)
        self.points.record_call(rid, 'model-call-0001', {'prompt_tokens': 1000, 'completion_tokens': 100} if known else None,
                                provider_request_id='supplier-' + key)
        if service:
            self.points.before_service_call(rid, 'search-call-0001', '.1')
            self.points.record_service_call(rid, 'search-call-0001')
        return self.points.settle(rid)

    def assertCode(self, code, action):
        with self.assertRaises(PointsError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def fail_audit(self):
        with self.points._connect() as conn:
            conn.execute("CREATE TRIGGER injected_audit_failure BEFORE INSERT ON billing_admin_audit BEGIN SELECT RAISE(ABORT,'test journal outage'); END")

    def test_price_draft_preview_never_activates_and_publishing_is_idempotent(self):
        draft = self.admin.draft_price(self.payload, 1, 'draft-operation-01')
        self.assertEqual(draft['preview']['sample']['supplier_cost_cny'], '0.010')
        self.assertEqual(draft['preview']['sample']['user_points'], '10.000000')
        self.assertEqual(len(self.points.prices()), 1)
        self.assertEqual(self.admin.draft_price(self.payload, 1, 'draft-operation-01'), draft)
        self.now += 30
        published = self.admin.publish_draft(draft['draft_id'], 2, 'publish-operation-01')
        self.assertEqual(published['price']['effective_at'], self.now)
        self.assertEqual(len(self.points.prices()), 2)
        self.assertEqual(self.admin.publish_draft(draft['draft_id'], 2, 'publish-operation-01'), published)
        self.assertCode('draft_published', lambda: self.admin.publish_draft(draft['draft_id'], 2, 'publish-operation-02'))
        self.assertEqual(len(self.admin.finance_audit()['items']), 2)

    def test_price_payload_and_operation_reuse_fail_closed(self):
        self.admin.draft_price(self.payload, 1, 'draft-operation-01')
        self.assertCode('idempotency_conflict', lambda: self.admin.draft_price({**self.payload, 'model': 'changed'}, 1, 'draft-operation-01'))
        self.assertCode('idempotency_conflict', lambda: self.admin.draft_price(self.payload, 2, 'draft-operation-01'))
        for change in ({'api_key': 'not-stored'}, {'rates': {'input_uncached': '0'}}, {'currency': 'USD'}):
            with self.assertRaises(PointsError):
                self.admin.draft_price({**self.payload, **change}, 1, 'invalid-operation-01')
        self.assertEqual(len(self.points.prices()), 1)
        self.assertEqual(len(self.admin.drafts()['items']), 1)

    def test_disable_requires_reason_and_records_price_id(self):
        result = self.admin.disable_price(self.price['id'], 2, 'disable-operation-01', '采购价格待核实')
        self.assertEqual(result['status'], 'disabled')
        self.assertEqual(self.points.prices()[0]['status'], 'disabled')
        self.assertEqual(self.admin.disable_price(self.price['id'], 2, 'disable-operation-01', '采购价格待核实'), result)
        self.assertEqual(self.admin.finance_audit()['items'][0]['parameters']['price_id'], self.price['id'])
        with self.assertRaises(PointsError):
            self.admin.disable_price(self.price['id'], 2, 'disable-operation-02', '')

    def test_refund_is_a_reversal_once_and_preserves_original_posting(self):
        request = self.request()
        before = self.rows("SELECT * FROM points_ledger WHERE event='settle'")
        result = self.admin.refund(request['request_id'], 1, 'refund-operation-01', '供应商回答交付失败')
        self.assertEqual(result['refunded_points'], '2.800000')
        self.assertEqual(self.points.wallet(self.actor)['available'], '100.000000')
        self.assertEqual(self.rows("SELECT * FROM points_ledger WHERE event='settle'"), before)
        reversal = self.rows("SELECT * FROM points_ledger WHERE event='reversal'")[0]
        self.assertEqual(reversal['original_id'], before[0]['id'])
        self.assertEqual(self.admin.refund(request['request_id'], 1, 'refund-operation-01', '供应商回答交付失败'), result)
        self.assertCode('refund_duplicate', lambda: self.admin.refund(request['request_id'], 1, 'refund-operation-02', '供应商回答交付失败'))
        self.assertTrue(self.points.audit()['ok'])

    def test_small_adjustment_is_immediate_and_has_no_client_reviewer_argument(self):
        result = self.admin.adjust(self.actor, '100', 1, 'adjust-operation-01', '客服人工补偿处理', self.now + 3600)
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(self.points.wallet(self.actor)['available'], '200.000000')
        self.assertEqual(self.admin.adjust(self.actor, '100.000000', 1, 'adjust-operation-01', '客服人工补偿处理', self.now + 3600), result)
        with self.assertRaises(TypeError):
            self.admin.adjust(self.actor, '1000', 1, 'forged-review-0001', '客服人工补偿处理', reviewed_by=2)
        self.assertEqual(self.admin.adjustments()['items'], [])
        self.assertTrue(self.points.audit()['ok'])

    def test_large_adjustment_requires_distinct_reviewer_and_applies_once(self):
        proposal = self.admin.adjust(self.actor, '101', 1, 'adjust-operation-01', '核实后进行批量补偿', self.now + 3600)
        self.assertEqual(proposal['status'], 'pending_approval')
        self.assertEqual(self.points.wallet(self.actor)['available'], '100.000000')
        self.assertCode('distinct_reviewer_required', lambda: self.admin.approve_adjustment(proposal['adjustment_id'], 1, 'approve-operation-01'))
        result = self.admin.approve_adjustment(proposal['adjustment_id'], 2, 'approve-operation-02')
        self.assertEqual(result['status'], 'approved')
        self.assertEqual(self.points.wallet(self.actor)['available'], '201.000000')
        self.assertEqual(self.admin.approve_adjustment(proposal['adjustment_id'], 2, 'approve-operation-02'), result)
        self.assertCode('adjustment_approved', lambda: self.admin.approve_adjustment(proposal['adjustment_id'], 3, 'approve-operation-03'))
        self.assertEqual(self.admin.adjustments(status='approved')['items'][0]['reviewed_by'], 2)
        self.assertTrue(self.points.audit()['ok'])

    def test_parallel_review_cannot_double_apply(self):
        proposal = self.admin.adjust(self.actor, '101', 1, 'adjust-operation-01', '核实后进行批量补偿', self.now + 3600)
        def approve(identity):
            try:
                other = BillingAdminStore(PointsStore(self.db, clock=lambda: self.now))
                return other.approve_adjustment(proposal['adjustment_id'], identity, 'approve-operation-' + str(identity))['status']
            except PointsError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(approve, (2, 3))), ['approved', 'adjustment_approved'])
        self.assertEqual(self.points.wallet(self.actor)['available'], '201.000000')

    def test_approval_rechecks_expiry_and_debit_never_touches_held_or_negative(self):
        proposal = self.admin.adjust(self.actor, '101', 1, 'adjust-operation-01', '核实后进行批量补偿', self.now + 10)
        self.now += 11
        with self.assertRaises(PointsError):
            self.admin.approve_adjustment(proposal['adjustment_id'], 2, 'approve-operation-01')
        self.assertEqual(self.admin.adjustments()['items'][0]['status'], 'pending_approval')
        proposal = self.admin.adjust(self.actor, '-101', 1, 'adjust-operation-02', '追回重复发放的积分')
        self.assertCode('insufficient_points', lambda: self.admin.approve_adjustment(proposal['adjustment_id'], 2, 'approve-operation-02'))
        self.assertEqual(self.points.wallet(self.actor)['available'], '100.000000')

    def test_reconcile_pending_usage_settles_verified_amount_and_keeps_reference_admin_only(self):
        request = self.request(known=False)
        self.assertEqual(request['state'], 'usage_pending')
        pending = self.admin.pending()['items'][0]
        self.assertEqual(pending['provider_request_id'], 'supplier-request-0001')
        self.assertEqual(pending['call_kind'], 'model')
        self.assertIsNone(self.admin.report()['items'][0]['supplier_cost_cny'])
        self.assertIsNone(self.admin.report()['items'][0]['user_priced_cost_cny'])
        self.assertFalse(self.admin.report()['items'][0]['user_priced_cost_complete'])
        result = self.admin.reconcile(request['request_id'], 'model-call-0001', {'prompt_tokens': 1000, 'completion_tokens': 100},
                                      'supplier-request-0001', 2, 'reconcile-operation-01', '供应商账单核实用量')
        self.assertTrue(result['complete'])
        self.assertEqual(self.points.request_status(self.actor, request['request_id'])['charged_points'], '2.800000')
        self.assertNotIn('supplier-request-0001', json.dumps(self.points.request_status(self.actor, request['request_id'])))
        self.assertEqual(self.admin.pending()['items'], [])
        report = self.admin.report()['items'][0]
        self.assertEqual(report['calls'][0]['provider_request_id'], 'supplier-request-0001')
        self.assertEqual(report['supplier_cost_cny'], '0.0028')

    def test_late_reconciliation_after_release_is_platform_cost_without_clawback(self):
        self.points.publish_price({**self.payload, 'user_rates': {'input_uncached':'4', 'cache_read':'1', 'cache_write':'4', 'billable_output':'16'}})
        request = self.request(known=False)
        self.now += 86401
        status = self.points.request_status(self.actor, request['request_id'])
        self.assertEqual(status['state'], 'usage_released')
        self.assertEqual(self.points.wallet(self.actor)['available'], '100.000000')
        self.admin.reconcile(request['request_id'], 'model-call-0001', {'prompt_tokens': 1000, 'completion_tokens': 100},
                             'supplier-request-0001', 2, 'reconcile-operation-01', '供应商迟到费用核实')
        status = self.points.request_status(self.actor, request['request_id'])
        self.assertEqual(status['charged_points'], '0.000000')
        self.assertEqual(status['platform_cost_cny'], '0.0028')
        self.assertEqual(status['user_cost_cny'], '0.0056')
        self.assertEqual(self.points.summary()['user_priced_cost_cny'], '0.0056')
        report=self.admin.report()['items'][0]
        self.assertEqual(report['user_priced_cost_cny'], '0.0056')
        self.assertEqual(report['supplier_cost_cny'], '0.0028')
        self.assertTrue(report['user_priced_cost_complete'])
        self.assertEqual(report['charged_points'], '0.000000')
        self.assertEqual(self.points.wallet(self.actor)['available'], '100.000000')

    def test_reconciliation_rejects_incomplete_body_and_changed_existing_usage(self):
        request = self.request(known=False)
        for usage in ({'prompt_tokens': 1000}, {'prompt_tokens': 1000, 'completion_tokens': 100, 'prompt': 'must not be stored'}):
            with self.assertRaises(PointsError):
                self.admin.reconcile(request['request_id'], 'model-call-0001', usage, 'supplier-request-0001', 1, 'reconcile-operation-01', '供应商账单核实用量')
        self.assertEqual(self.admin.finance_audit()['items'], [])
        self.admin.reconcile(request['request_id'], 'model-call-0001', {'prompt_tokens': 1000, 'completion_tokens': 100},
                             'supplier-request-0001', 1, 'reconcile-operation-01', '供应商账单核实用量')
        self.assertCode('usage_conflict', lambda: self.admin.reconcile(request['request_id'], 'model-call-0001', {'prompt_tokens': 900, 'completion_tokens': 100},
                                                                     'supplier-request-0001', 1, 'reconcile-operation-02', '供应商账单核实用量'))
        with self.assertRaises(PointsError):
            self.admin.reconcile(request['request_id'], 'model-call-0001', {'prompt_tokens': 1000, 'completion_tokens': 100},
                                 'bad\nreference', 1, 'reconcile-operation-03', '供应商账单核实用量')

    def test_service_reconciliation_is_platform_only_and_shown_even_when_request_settled(self):
        request = self.request(service=True)
        self.assertEqual(request['state'], 'settled')
        self.assertEqual(self.admin.pending()['items'][0]['call_id'], 'search-call-0001')
        self.assertEqual(self.admin.pending()['items'][0]['call_kind'], 'firecrawl')
        self.assertEqual(self.admin.report()['items'][0]['user_priced_cost_cny'], '0.0028')
        self.assertTrue(self.admin.report()['items'][0]['user_priced_cost_complete'])
        result = self.admin.reconcile_service(request['request_id'], 'search-call-0001', '.07', 2, 'service-operation-01',
                                             '搜索供应商账单核实', provider_request_id='firecrawl-reference-01')
        self.assertEqual(result['cost_cny'], '0.07')
        self.assertEqual(self.points.request_status(self.actor, request['request_id'])['charged_points'], '2.800000')
        self.assertEqual(self.admin.report()['items'][0]['supplier_cost_cny'], '0.0728')
        self.assertEqual(self.admin.pending()['items'], [])
        self.assertEqual(self.admin.reconcile_service(request['request_id'], 'search-call-0001', '.07', 2, 'service-operation-01',
                                                     '搜索供应商账单核实', provider_request_id='firecrawl-reference-01'), result)
        with self.assertRaises(PointsError):
            self.admin.reconcile_service(request['request_id'], 'search-call-0001', '.08', 2, 'service-operation-02', '搜索供应商账单核实')

    def test_journal_failure_rolls_back_core_price_and_adjustment_changes(self):
        draft = self.admin.draft_price(self.payload, 1, 'draft-operation-01')
        self.fail_audit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.admin.publish_draft(draft['draft_id'], 1, 'publish-operation-01')
        self.assertEqual(len(self.points.prices()), 1)
        self.assertEqual(self.admin.drafts()['items'][0]['status'], 'draft')
        with self.assertRaises(sqlite3.IntegrityError):
            self.admin.adjust(self.actor, '10', 1, 'adjust-operation-01', '客服人工补偿处理', self.now + 3600)
        self.assertEqual(self.points.wallet(self.actor)['available'], '100.000000')
        self.assertEqual(self.rows("SELECT * FROM points_operations WHERE key LIKE 'adjust:admin:%'"), [])
        self.assertTrue(self.points.audit()['ok'])

    def test_audit_is_immutable_and_operations_cannot_cross_actions(self):
        self.admin.draft_price(self.payload, 1, 'shared-operation-01')
        self.assertCode('idempotency_conflict', lambda: self.admin.disable_price(self.price['id'], 1, 'shared-operation-01', '采购价格待核实'))
        for sql in ('UPDATE billing_admin_audit SET admin_id=2', 'DELETE FROM billing_admin_audit'):
            with self.assertRaises(sqlite3.IntegrityError):
                with self.points._connect() as conn:
                    conn.execute(sql)

    def test_bounded_pages_export_and_no_content_or_credentials(self):
        for number in range(3):
            self.admin.draft_price(self.payload, 1, 'draft-operation-' + str(number))
            self.admin.adjust(self.actor, '101', 1, 'adjust-operation-' + str(number), '核实后进行批量补偿', self.now + 3600)
        for method in (self.admin.finance_audit, self.admin.drafts, self.admin.adjustments, self.admin.pending):
            first = method(limit=1)
            self.assertEqual(len(first['items']), 1)
            second = method(limit=1, cursor=first['next_cursor'])
            self.assertNotEqual(first['items'], second['items'])
            for kwargs in ({'limit': 201}, {'limit': True}, {'cursor': -1}, {'cursor': '0'}):
                with self.assertRaises(PointsError):
                    method(**kwargs)
        self.request(key='request-0001')
        self.request(key='request-0002')
        first = self.admin.report(limit=1)
        second = self.admin.report(limit=1, cursor=first['next_cursor'])
        self.assertNotEqual(first['items'][0]['request_id'], second['items'][0]['request_id'])
        for kwargs in ({'start_at': self.now - 32 * 86400}, {'end_at': 'invalid'}, {'end_at': float('nan')}, {'limit': 201}):
            with self.assertRaises(PointsError):
                self.admin.report(**kwargs)
        self.assertNotIn('prompt', json.dumps(self.admin.report()))
        for reason in ('bad\nreason', 'sk-abcdefghijklmnopqrstuvwxyz', 'Bearer privatecredential', 'api_key=privatecredential'):
            with self.assertRaises(PointsError):
                self.admin.refund(first['items'][0]['request_id'], 1, 'invalid-reason-01', reason)

    def test_server_admin_identity_is_strict(self):
        for admin in (True, 0, -1, '1', None):
            self.assertCode('admin_required', lambda: self.admin.draft_price(self.payload, admin, 'draft-operation-01'))
        for amount in ('NaN', '0', '1.0000001', '100001', 1.5, True):
            with self.assertRaises(PointsError):
                self.admin.adjust(self.actor, amount, 1, 'invalid-adjustment-01', '客服人工补偿处理', self.now + 3600)

    def test_legacy_database_adds_supplier_reference_without_rewriting_usage(self):
        legacy = self.db.parent / 'legacy.db'
        with sqlite3.connect(legacy) as conn:
            conn.executescript(SCHEMA.replace('provider_request_id TEXT, ', ''))
            self.assertNotIn('provider_request_id', {row[1] for row in conn.execute('PRAGMA table_info(points_calls)')})
        upgraded = PointsStore(legacy, clock=lambda: self.now)
        with upgraded._connect() as conn:
            self.assertIn('provider_request_id', {row[1] for row in conn.execute('PRAGMA table_info(points_calls)')})
        self.assertTrue(BillingAdminStore(upgraded).report()['items'] == [])

    def test_policy_configuration_receipt_is_current_idempotent_and_atomic(self):
        result = self.admin.configure_policy({'daily_cash_limit_cny': '2'}, 1, 'policy-operation-01')
        self.assertEqual(result['daily_cash_limit_cny'], '2')
        self.assertEqual(self.points.policy()['daily_cash_limit_cny'], '2')
        self.assertEqual(self.admin.finance_audit()['items'][0]['action'], 'policy.configure')
        self.assertEqual(self.admin.configure_policy({'daily_cash_limit_cny': '2'}, 1, 'policy-operation-01'), result)
        self.assertCode('idempotency_conflict', lambda:self.admin.configure_policy({'daily_cash_limit_cny': '3'}, 1, 'policy-operation-01'))
        self.fail_audit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.admin.configure_policy({'daily_cash_limit_cny': '4'}, 1, 'policy-operation-02')
        self.assertEqual(self.points.policy()['daily_cash_limit_cny'], '2')
        self.assertEqual(self.rows('SELECT limit_cny FROM points_cash_days')[0]['limit_cny'], '2')
        self.assertEqual(len(self.admin.finance_audit()['items']), 1)

    def test_draft_reload_restores_authoritative_preview_without_activating_price(self):
        payload = {**self.payload, 'user_rates': {'input_uncached': '4', 'cache_read': '1', 'cache_write': '4', 'billable_output': '16'}}
        draft = self.admin.draft_price(payload, 1, 'draft-operation-01')
        self.now += 3600
        reopened = BillingAdminStore(PointsStore(self.db, clock=lambda:self.now))
        saved = reopened.drafts()['items'][0]
        self.assertEqual(saved['draft_id'], draft['draft_id'])
        self.assertEqual(saved['preview'], draft['preview'])
        self.assertEqual(saved['preview']['sample']['user_points'], '20.000000')
        self.assertEqual(saved['preview']['sample']['supplier_cost_cny'], '0.010')
        self.assertEqual(saved['status'], 'draft')
        self.assertEqual(len(self.points.prices()), 1)
        self.assertEqual(len(reopened.finance_audit()['items']), 1)

    def test_projection_rebuild_is_audited_idempotent_and_changes_no_postings(self):
        ledger=self.rows('SELECT * FROM points_ledger ORDER BY id')
        with self.points._tx() as conn:
            conn.execute('UPDATE points_lots SET available=available-1000000,spent=spent+1000000')
        result=self.admin.rebuild_projection(1,'rebuild-operation-01','账本检查后重建余额投影')
        self.assertEqual(result['repaired_lots'],1)
        self.assertEqual(self.points.wallet(self.actor)['available'],'100.000000')
        self.assertEqual(self.rows('SELECT * FROM points_ledger ORDER BY id'),ledger)
        self.assertEqual(self.admin.rebuild_projection(1,'rebuild-operation-01','账本检查后重建余额投影'),result)
        self.assertEqual(len(self.admin.finance_audit()['items']),1)
        event=self.admin.finance_audit()['items'][0]
        self.assertEqual((event['action'],event['admin_id']),('projection.rebuild',1))
        self.assertEqual(event['parameters']['reason'],'账本检查后重建余额投影')
        self.assertCode('idempotency_conflict',lambda:self.admin.rebuild_projection(1,'rebuild-operation-01','另一项余额重建操作'))
        with self.assertRaises(PointsError):self.admin.rebuild_projection(1,'rebuild-operation-02','')
        self.assertTrue(self.points.audit()['ok'])

    def test_projection_rebuild_audit_failure_rolls_back_repaired_balances(self):
        with self.points._tx() as conn:
            conn.execute('UPDATE points_lots SET available=available-1000000,held=held+1000000')
        corrupted=self.rows('SELECT * FROM points_lots')
        self.fail_audit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.admin.rebuild_projection(1,'rebuild-operation-01','账本检查后重建余额投影')
        self.assertEqual(self.rows('SELECT * FROM points_lots'),corrupted)
        self.assertEqual(self.admin.finance_audit()['items'],[])


if __name__ == '__main__':
    unittest.main()
