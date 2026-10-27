"""Real HTTP billing administration against isolated ledgers and real MFA.

The existing credit fixture fakes provider I/O only. Account provisioning is a
trusted local setup helper; every tested administrative action crosses HTTP.
Composition avoids re-running the fixture class's unrelated test methods.
"""
from decimal import Decimal
import json
import sqlite3
import time
import unittest
import uuid
from urllib.parse import urlencode
from unittest.mock import patch

from agent import auth_server
from agent.auth import totp_code
from agent.tests import test_credits_http as fixture


class BillingAdminHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.CreditsHTTPTests(methodName='runTest')
        self.addCleanup(self.fx.doCleanups)
        self.fx.setUp()
        self.server, self.db = self.fx.server, self.fx.db
        self.auth = self.server.auth_service
        self.admin = self.admin_client('billing_admin_one')

    def admin_client(self, username, *, mfa=True):
        password = 'isolated-admin-password-123'
        created = self.auth.bootstrap_admin(username, password)
        self.assertTrue(created['ok'], created)
        secret = 'JBSWY3DPEHPK3PXP'
        tick = int(time.time() // 30) * 30
        if mfa:
            self.auth.configure_admin_totp(created['admin_id'], secret)
        # Setup KDF/file work can cross a wall-clock boundary. Pin only this
        # trusted login so its previous-window code stays valid, reserving the
        # current-window code for the real HTTP reauthentication assertions.
        with patch('agent.auth._now', return_value=tick + 1):
            logged = self.auth.admin_login(username, password, require_captcha=False,
                require_mfa=mfa, otp_code=totp_code(secret, tick - 30) if mfa else None)
        self.assertTrue(logged['ok'], logged)
        client = fixture.CookieClient(self.server)
        client.cookies[auth_server.ADMIN_SESSION_COOKIE] = logged['session_token']
        client.admin_id, client.session_id = created['admin_id'], logged['session_id']
        client.totp_secret, client.login_tick = secret, tick
        return client

    def target_user(self, name='billing_target'):
        password = 'isolated-user-password-123'
        registered = self.auth.register(name, password, require_captcha=False)
        self.assertTrue(registered['ok'], registered)
        logged = self.auth.login(name, password, require_captcha=False)
        self.assertTrue(logged['ok'], logged)
        client = fixture.CookieClient(self.server)
        client.cookies[auth_server.SESSION_COOKIE] = logged['session_token']
        uid = registered['user_id']
        actor = self.server.points.user_actor(uid)
        self.server.points.wallet(actor)
        return uid, actor, client

    def post(self, path, values=None, *, client=None, operation=None):
        payload = {'operation_id': operation or uuid.uuid4().hex, **(values or {})}
        return (client or self.admin).request(path, 'POST', payload)

    def rows(self, query, args=()):
        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(query, args)]

    def price_payload(self, **changes):
        config = self.server.agent.config.llm
        return {'provider': config.provider, 'gateway': config.base_url,
            'model': config.model, 'adapter': 'openai',
            'rates': {'input_uncached': '1', 'cache_read': '0', 'cache_write': '1', 'billable_output': '2'},
            **changes}

    def generated_request(self, *, unknown=False):
        self.fx.bootstrap()
        self.server.agent.usage_complete = not unknown
        payload, quote = self.fx.quoted(thread_id='billing-' + uuid.uuid4().hex)
        status, events, _ = self.fx.post('/api/agent/stream', {**payload, 'quote_id': quote['quote_id']})
        self.assertEqual(status, 200, events)
        charge = next(event['charge'] for event in events if event['type'] in {'settled', 'usage_pending'})
        self.assertEqual(charge['state'], 'usage_pending' if unknown else 'settled')
        return charge

    def test_fixture_admin_login_survives_setup_crossing_totp_boundary(self):
        tick = int(time.time() // 30) * 30
        # Simulate reading the fixture tick immediately before the boundary,
        # followed by KDF/enrollment work finishing in the next time step.
        with patch('agent.auth._now', return_value=tick + 31), \
             patch('agent.tests.test_billing_admin_http.time.time', return_value=tick + 29.99):
            client = self.admin_client('boundary_admin_fixture')
        session = self.rows('SELECT mfa_verified_at FROM admin_sessions WHERE session_id=?',
                            (client.session_id,))[0]
        self.assertEqual(session['mfa_verified_at'], tick + 1)
        self.assertTrue(self.auth.validate_admin_session(client.cookies[auth_server.ADMIN_SESSION_COOKIE])['ok'])

    def test_admin_reads_and_writes_reject_guests_and_regular_user_tokens(self):
        _, _, user = self.target_user()
        guests = [fixture.CookieClient(self.server), self.fx.client, user]
        self.fx.bootstrap()
        for client in guests:
            for path in ('/api/admin/billing', '/api/admin/billing/report',
                         '/api/admin/billing/finance-audit', '/api/admin/billing/pending-usage',
                         '/api/admin/billing/drafts', '/api/admin/billing/adjustments',
                         '/api/admin/security/events', '/api/admin/security/summary'):
                status, result, _ = client.request(path)
                self.assertEqual(status, 401, (path, result))
                self.assertRegex(result['risk_event_id'], r'^risk_[a-f0-9]{32}$')
                self.assertNotIn('items', result)
                self.assertNotIn('principal_hash', json.dumps(result))
            status, result, _ = self.post('/api/admin/billing/policy',
                {'daily_cash_limit_cny': '99'}, client=client)
            self.assertEqual(status, 401, result)
        self.assertEqual(self.server.points.policy()['daily_cash_limit_cny'], '1')
        self.assertEqual(self.rows('SELECT COUNT(*) AS count FROM billing_admin_audit')[0]['count'], 0)

    def test_recent_auth_requires_fresh_real_totp_and_rejects_replay(self):
        self.server.require_admin_mfa = True
        old = time.time() - 600
        with sqlite3.connect(self.db) as conn:
            conn.execute('UPDATE admin_sessions SET recent_authenticated_at=?,mfa_verified_at=? WHERE session_id=?',
                         (old, old, self.admin.session_id))
        values = {'daily_cash_limit_cny': '2'}
        status, denied, _ = self.post('/api/admin/billing/policy', values)
        self.assertEqual(status, 403, denied)
        self.assertEqual(denied['code'], 'reauth_required')
        self.assertEqual(self.server.points.policy()['daily_cash_limit_cny'], '1')
        status, denied, _ = self.post('/api/admin/billing/policy', {**values, 'otp_code': 'abcdef'})
        self.assertEqual(status, 403, denied)
        fresh = totp_code(self.admin.totp_secret, self.admin.login_tick)
        operation = uuid.uuid4().hex
        status, accepted, _ = self.post('/api/admin/billing/policy', {**values, 'otp_code': fresh}, operation=operation)
        self.assertEqual(status, 200, accepted)
        self.assertEqual(self.server.points.policy()['daily_cash_limit_cny'], '2')
        session = self.rows('SELECT recent_authenticated_at,mfa_verified_at FROM admin_sessions WHERE session_id=?',
                            (self.admin.session_id,))[0]
        self.assertGreater(session['recent_authenticated_at'], old)
        self.assertGreater(session['mfa_verified_at'], old)
        status, replayed, _ = self.post('/api/admin/billing/policy',
            {'daily_cash_limit_cny': '3', 'otp_code': fresh})
        self.assertEqual(status, 403, replayed)
        self.assertEqual(self.server.points.policy()['daily_cash_limit_cny'], '2')
        self.assertEqual(len(self.rows('SELECT * FROM billing_admin_audit')), 1)

    def test_public_mfa_policy_rejects_valid_password_only_admin_session(self):
        password_only = self.admin_client('billing_no_mfa', mfa=False)
        self.server.require_admin_mfa = True
        self.assertEqual(password_only.request('/api/admin/billing')[0], 401)
        status, result, _ = self.post('/api/admin/billing/policy',
            {'daily_cash_limit_cny': '2', 'otp_code': '123456'}, client=password_only)
        self.assertEqual(status, 401, result)
        self.assertEqual(self.server.points.policy()['daily_cash_limit_cny'], '1')
        self.assertEqual(self.admin.request('/api/admin/billing')[0], 200)

    def test_price_draft_preview_publish_and_disable_are_audited_and_idempotent(self):
        before = len(self.server.points.prices())
        operation = uuid.uuid4().hex
        payload = {'price': self.price_payload()}
        status, draft, _ = self.post('/api/admin/billing/drafts', payload, operation=operation)
        self.assertEqual(status, 200, draft)
        self.assertEqual(draft['preview']['status'], 'draft')
        self.assertIn('sample', draft['preview'])
        self.assertEqual(len(self.server.points.prices()), before)
        status, retry, _ = self.post('/api/admin/billing/drafts', payload, operation=operation)
        self.assertEqual(status, 200, retry)
        self.assertEqual(retry['draft_id'], draft['draft_id'])
        status, published, _ = self.post('/api/admin/billing/drafts/' + draft['draft_id'] + '/publish')
        self.assertEqual(status, 200, published)
        price = published['price']
        self.assertEqual(price['status'], 'published')
        self.assertEqual(len(self.server.points.prices()), before + 1)
        latest = self.server.points.quote(self.server.points.user_actor(17), 'a'*64,
            model=price['model'], gateway=price['gateway'], input_tokens=100, output_tokens=10, max_calls=1)
        self.assertEqual(latest['price']['id'], price['id'])
        status, disabled, _ = self.post('/api/admin/billing/prices/' + price['id'] + '/disable',
                                      {'reason': '隔离测试暂停价格'})
        self.assertEqual(status, 200, disabled)
        self.assertEqual(next(p for p in self.server.points.prices() if p['id'] == price['id'])['status'], 'disabled')
        actions = [row['action'] for row in self.rows('SELECT action FROM billing_admin_audit ORDER BY id')]
        self.assertEqual(actions, ['draft_price', 'publish_draft', 'disable_price'])

    def test_policy_operation_id_cannot_be_reused_for_new_values_or_admin(self):
        operation = uuid.uuid4().hex
        values = {'daily_cash_limit_cny': '2'}
        self.assertEqual(self.post('/api/admin/billing/policy', values, operation=operation)[0], 200)
        self.assertEqual(self.post('/api/admin/billing/policy', values, operation=operation)[0], 200)
        status, result, _ = self.post('/api/admin/billing/policy', {'daily_cash_limit_cny': '3'}, operation=operation)
        self.assertEqual(status, 409, result)
        second = self.admin_client('billing_admin_two')
        status, result, _ = self.post('/api/admin/billing/policy', values, operation=operation, client=second)
        self.assertEqual(status, 409, result)
        self.assertEqual(self.server.points.policy()['daily_cash_limit_cny'], '2')
        audit = self.rows('SELECT * FROM billing_admin_audit')
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]['admin_id'], self.admin.admin_id)
        self.assertEqual(audit[0]['operation_id'], operation)

    def test_compatibility_publish_accepts_maximum_operation_id_without_duplicate_price(self):
        before = len(self.server.points.prices())
        operation = 'P' * 128
        payload = self.price_payload()
        status, first, _ = self.post('/api/admin/billing/prices', payload, operation=operation)
        self.assertEqual(status, 200, first)
        status, second, _ = self.post('/api/admin/billing/prices', payload, operation=operation)
        self.assertEqual(status, 200, second)
        self.assertEqual(first['price']['id'], second['price']['id'])
        self.assertEqual(len(self.server.points.prices()), before + 1)
        self.assertEqual(len(self.rows('SELECT * FROM billing_admin_audit')), 2)

    def test_public_request_uses_user_prices_and_hides_supplier_cost_and_reference(self):
        payload = self.price_payload(
            rates={'input_uncached': '0.001', 'cache_read': '0.001', 'cache_write': '0.001', 'billable_output': '0.002'},
            user_rates={'input_uncached': '0.005', 'cache_read': '0.005', 'cache_write': '0.005', 'billable_output': '0.007'})
        status, published, _ = self.post('/api/admin/billing/prices', payload)
        self.assertEqual(status, 200, published)
        charge = self.generated_request()
        status, public, _ = self.fx.client.request('/api/points/requests/' + charge['request_id'])
        self.assertEqual(status, 200, public)
        self.assertEqual(public['price']['rates'], payload['user_rates'])
        self.assertEqual(Decimal(public['user_cost_cny']), Decimal('0.00000093'))
        for hidden in ('supplier_cost_cny', 'platform_cost_cny', 'provider_request_id', 'isolated-http-1'):
            self.assertNotIn(hidden, json.dumps(public))
        status, finance, _ = self.admin.request('/api/admin/billing/report')
        self.assertEqual(status, 200, finance)
        item = next(item for item in finance['items'] if item['request_id'] == charge['request_id'])
        self.assertEqual(Decimal(item['supplier_cost_cny']), Decimal('0.000000213'))
        self.assertEqual(item['calls'][0]['provider_request_id'], 'isolated-http-1')

    def test_refund_same_operation_restores_once_and_keeps_original_charge(self):
        charge = self.generated_request()
        self.assertGreater(Decimal(charge['charged_points']), 0)
        before = self.fx.client.request('/api/points/wallet')[1]['wallet']['available_micro']
        operation = uuid.uuid4().hex
        values = {'request_id': charge['request_id'], 'reason': '隔离测试退款确认'}
        status, first, _ = self.post('/api/admin/billing/refunds', values, operation=operation)
        self.assertEqual(status, 200, first)
        status, retried, _ = self.post('/api/admin/billing/refunds', values, operation=operation)
        self.assertEqual(status, 200, retried)
        self.assertEqual(first, retried)
        after = self.fx.client.request('/api/points/wallet')[1]['wallet']['available_micro']
        self.assertEqual(after - before, charge['charged_micro'])
        status, duplicate, _ = self.post('/api/admin/billing/refunds', values)
        self.assertEqual(status, 409, duplicate)
        self.assertEqual(duplicate['code'], 'refund_duplicate')
        stored = self.rows('SELECT charged_micro FROM points_requests WHERE id=?', (charge['request_id'],))[0]
        self.assertEqual(stored['charged_micro'], charge['charged_micro'])
        self.assertEqual(len(self.rows("SELECT * FROM billing_admin_audit WHERE action='refund'")), 1)
        self.assertTrue(self.server.points.audit()['ok'])

    def test_small_adjustment_applies_and_large_adjustment_needs_independent_admin(self):
        uid, actor, user = self.target_user()
        expiry = time.time() + 3600
        values = {'user_id': uid, 'points': '100', 'reason': '隔离测试赠送调整', 'expires_at': expiry}
        status, small, _ = self.post('/api/admin/billing/adjustments', values)
        self.assertEqual(status, 200, small)
        self.assertEqual(small['status'], 'applied')
        self.assertEqual(self.server.points.wallet(actor)['available'], '200.000000')
        status, proposal, _ = self.post('/api/admin/billing/adjustments', {**values, 'points': '101'})
        self.assertEqual(status, 200, proposal)
        self.assertEqual(proposal['status'], 'pending_approval')
        self.assertEqual(self.server.points.wallet(actor)['available'], '200.000000')
        path = '/api/admin/billing/adjustments/' + proposal['adjustment_id'] + '/approve'
        status, denied, _ = self.post(path)
        self.assertEqual(status, 403, denied)
        self.assertEqual(denied['code'], 'distinct_reviewer_required')
        status, denied, _ = self.post(path, client=user)
        self.assertEqual(status, 401, denied)
        second = self.admin_client('billing_reviewer')
        status, forged, _ = self.post(path, {'reviewed_by': second.admin_id})
        self.assertEqual(status, 400, forged)
        with sqlite3.connect(self.db) as conn:
            conn.execute('UPDATE admin_sessions SET recent_authenticated_at=?,mfa_verified_at=? WHERE session_id=?',
                         (time.time() - 600, time.time() - 600, second.session_id))
        status, stale, _ = self.post(path, client=second)
        self.assertEqual(status, 403, stale)
        self.assertEqual(stale['code'], 'reauth_required')
        self.assertEqual(self.server.points.wallet(actor)['available'], '200.000000')
        operation = uuid.uuid4().hex
        status, approved, _ = self.post(path,
            {'otp_code': totp_code(second.totp_secret, second.login_tick)}, client=second, operation=operation)
        self.assertEqual(status, 200, approved)
        self.assertEqual(approved['status'], 'approved')
        self.assertEqual(self.server.points.wallet(actor)['available'], '301.000000')
        self.assertEqual(self.post(path, client=second, operation=operation)[0], 200)
        self.assertEqual(self.server.points.wallet(actor)['available'], '301.000000')
        stored = self.rows('SELECT admin_id,reviewed_by,status FROM billing_admin_adjustments WHERE id=?',
                           (proposal['adjustment_id'],))[0]
        self.assertEqual(stored, {'admin_id': self.admin.admin_id, 'reviewed_by': second.admin_id, 'status': 'approved'})
        status, subtraction, _ = self.post('/api/admin/billing/adjustments',
            {'user_id': uid, 'points': '-100', 'reason': '隔离测试边界扣减'})
        self.assertEqual(status, 200, subtraction)
        self.assertEqual(subtraction['status'], 'applied')
        self.assertEqual(self.server.points.wallet(actor)['available'], '201.000000')
        self.assertTrue(self.server.points.audit()['ok'])

    def test_negative_large_adjustment_also_requires_review_and_rejects_forged_reviewer(self):
        uid, actor, _ = self.target_user()
        before = self.server.points.wallet(actor)['available_micro']
        values = {'user_id': uid, 'points': '-101', 'reason': '隔离测试扣减调整'}
        status, denied, _ = self.post('/api/admin/billing/adjustments', {**values, 'reviewed_by': 99})
        self.assertEqual(status, 400, denied)
        self.assertEqual(self.server.points.wallet(actor)['available_micro'], before)
        status, proposal, _ = self.post('/api/admin/billing/adjustments', values)
        self.assertEqual(status, 200, proposal)
        self.assertEqual(proposal['status'], 'pending_approval')
        second = self.admin_client('billing_negative_reviewer')
        status, denied, _ = self.post('/api/admin/billing/adjustments/' + proposal['adjustment_id'] + '/approve', client=second)
        self.assertEqual(status, 402, denied)
        self.assertEqual(self.server.points.wallet(actor)['available_micro'], before)
        self.assertEqual(self.rows('SELECT status FROM billing_admin_adjustments WHERE id=?', (proposal['adjustment_id'],))[0]['status'], 'pending_approval')

    def test_unknown_usage_manual_reconciliation_settles_and_late_usage_never_recharges(self):
        for late in (False, True):
            if late:
                # A separate wallet avoids the first request's in-flight gate.
                self.fx.client = fixture.CookieClient(self.server)
            charge = self.generated_request(unknown=True)
            rid = charge['request_id']
            call = self.rows('SELECT id,provider_request_id FROM points_calls WHERE request_id=?', (rid,))[0]
            status, pending, _ = self.admin.request('/api/admin/billing/pending-usage')
            self.assertEqual(status, 200, pending)
            self.assertIn(rid, [item['request_id'] for item in pending['items'] if item['kind'] == 'usage'])
            if late:
                future = time.time() + self.server.points.pending_seconds + 1
                self.server.points.clock = lambda: future
                self.fx.client.request('/api/points/wallet')
                self.assertEqual(self.rows('SELECT state FROM points_requests WHERE id=?', (rid,))[0]['state'], 'usage_released')
            operation = uuid.uuid4().hex
            values = {'request_id': rid, 'call_id': call['id'],
                'usage': {'prompt_tokens': 123, 'completion_tokens': 45},
                'provider_request_id': call['provider_request_id'], 'reason': '隔离供应商账单核验'}
            status, reconciled, _ = self.post('/api/admin/billing/reconcile', values, operation=operation)
            self.assertEqual(status, 200, reconciled)
            self.assertTrue(reconciled['complete'])
            self.assertEqual(self.post('/api/admin/billing/reconcile', values, operation=operation)[0], 200)
            stored = self.rows('SELECT state,charged_micro,supplier_cost FROM points_requests WHERE id=?', (rid,))[0]
            self.assertEqual(stored['state'], 'usage_released' if late else 'settled')
            self.assertEqual(stored['charged_micro'] == 0, late)
            self.assertGreater(Decimal(stored['supplier_cost']), 0)
            status, public, _ = self.fx.client.request('/api/points/requests/' + rid)
            self.assertEqual(status, 200, public)
            for key in ('supplier_cost_cny', 'platform_cost_cny'):
                self.assertNotIn(key, public)
            self.assertTrue(all('cost_cny' not in item for item in public['calls']))
            self.assertEqual(public['calls'][0]['usage'],
                {'input_uncached': 123, 'cache_read': 0, 'cache_write': 0, 'billable_output': 45})
            self.assertEqual(Decimal(public['calls'][0]['user_cost_cny']), Decimal('0.000000213'))
            self.assertTrue(self.server.points.audit()['ok'])

    def test_search_service_reconciliation_records_reference_and_retries_once(self):
        _, actor, user = self.target_user()
        config = self.server.agent.config.llm
        quote = self.server.points.quote(actor, 'a'*64, model=config.model,
            gateway=config.base_url, input_tokens=100, output_tokens=100,
            max_calls=1, cash_extra_cny='0.005')
        reserved = self.server.points.reserve(actor, quote['quote_id'], uuid.uuid4().hex, 'a'*64)
        rid, call_id = reserved['request_id'], uuid.uuid4().hex
        self.server.points.before_service_call(rid, call_id, '0.005')
        self.server.points.record_service_call(rid, call_id, None)
        self.server.points.settle(rid)
        before = self.server.points.wallet(actor)['available_micro']
        operation = uuid.uuid4().hex
        payload = {'request_id': rid, 'call_id': call_id, 'actual_cost_cny': '0.003',
            'provider_request_id': 'isolated-search-invoice-1', 'reason': '隔离搜索费用账单核验'}
        status, reconciled, _ = self.post('/api/admin/billing/reconcile-service', payload, operation=operation)
        self.assertEqual(status, 200, reconciled)
        self.assertEqual(Decimal(reconciled['cost_cny']), Decimal('0.003'))
        status, retry, _ = self.post('/api/admin/billing/reconcile-service', payload, operation=operation)
        self.assertEqual(status, 200, retry)
        self.assertEqual(reconciled, retry)
        call = self.rows('SELECT state,provider_request_id,cost,user_cost FROM points_calls WHERE request_id=? AND id=?',
                         (rid, call_id))[0]
        self.assertEqual(call['provider_request_id'], payload['provider_request_id'])
        self.assertEqual(call['state'], 'known')
        self.assertEqual(Decimal(call['cost']), Decimal('0.003'))
        self.assertEqual(Decimal(call['user_cost']), 0)
        self.assertEqual(self.server.points.wallet(actor)['available_micro'], before)
        status, conflict, _ = self.post('/api/admin/billing/reconcile-service',
            {**payload, 'provider_request_id': 'different-invoice'})
        self.assertEqual(status, 409, conflict)
        self.assertEqual(len(self.rows("SELECT * FROM billing_admin_audit WHERE action='reconcile_service'")), 1)
        status, report, _ = self.admin.request('/api/admin/billing/report')
        self.assertEqual(status, 200, report)
        item = next(item for item in report['items'] if item['request_id'] == rid)
        self.assertTrue(item['cost_complete'])
        self.assertEqual(item['calls'][0]['provider_request_id'], payload['provider_request_id'])
        status, public, _ = user.request('/api/points/requests/' + rid)
        self.assertEqual(status, 200, public)
        self.assertNotIn(payload['provider_request_id'], json.dumps(public))
        self.assertTrue(all('provider_request_id' not in row and 'cost_cny' not in row for row in public['calls']))
        self.assertTrue(self.server.points.audit()['ok'])

    def test_risk_event_ids_admin_pagination_and_summary_hide_raw_identifiers(self):
        status, rejected, _ = self.fx.client.request('/api/points/wallet')
        self.assertEqual(status, 401, rejected)
        event_id = rejected['risk_event_id']
        self.assertNotIn('score', rejected)
        self.assertNotIn('threshold', rejected)
        for _ in range(2): self.fx.client.request('/api/admin/billing')
        status, first, _ = self.admin.request('/api/admin/security/events?limit=1')
        self.assertEqual(status, 200, first)
        self.assertEqual(len(first['items']), 1)
        self.assertTrue(first['has_more'])
        status, second, _ = self.admin.request('/api/admin/security/events?limit=1&offset=' + str(first['next_offset']))
        self.assertEqual(status, 200, second)
        self.assertNotEqual(first['items'][0]['risk_event_id'], second['items'][0]['risk_event_id'])
        status, all_events, _ = self.admin.request('/api/admin/security/events?limit=200')
        self.assertEqual(status, 200, all_events)
        self.assertIn(event_id, [row['risk_event_id'] for row in all_events['items']])
        self.assertNotIn('127.0.0.1', json.dumps(all_events))
        status, report, _ = self.admin.request('/api/admin/security/summary')
        self.assertEqual(status, 200, report)
        self.assertEqual(report['total'], all_events['total'])
        self.assertIn('rules_version', report)
        self.assertNotIn('source_hash', report)

    def test_financial_report_and_audit_are_bounded_and_invalid_requests_do_not_mutate(self):
        self.generated_request()
        now = time.time()
        exact = urlencode({'start_at': now - 31*86400, 'end_at': now, 'limit': 1})
        status, report, _ = self.admin.request('/api/admin/billing/report?' + exact)
        self.assertEqual(status, 200, report)
        self.assertEqual(len(report['items']), 1)
        self.assertIn('supplier_cost_cny', report['items'][0])
        self.assertIn('provider_request_id', report['items'][0]['calls'][0])
        audit_before = len(self.rows('SELECT * FROM billing_admin_audit'))
        policy_before = self.server.points.policy()
        for path in ('/api/admin/billing/report?limit=201', '/api/admin/billing/report?cursor=-1',
                     '/api/admin/billing/report?' + urlencode({'start_at': now - 31*86400 - 1, 'end_at': now}),
                     '/api/admin/billing/report?start_at=nan', '/api/admin/billing/finance-audit?limit=0',
                     '/api/admin/security/events?limit=100000', '/api/admin/security/events?offset=-1',
                     '/api/admin/security/summary?since=nan'):
            status, result, _ = self.admin.request(path)
            self.assertEqual(status, 400, (path, result))
        for payload in ({'daily_cash_limit_cny': '-1'}, {'daily_cash_limit_cny': 0.1},
                        {'daily_cash_limit_cny': '2', 'reviewed_by': self.admin.admin_id}):
            status, result, _ = self.post('/api/admin/billing/policy', payload)
            self.assertEqual(status, 400, result)
        status, result, _ = self.post('/api/admin/billing/drafts', {'price': self.price_payload(rates={'input_uncached': '-1'})})
        self.assertEqual(status, 422, result)
        self.assertEqual(self.server.points.policy(), policy_before)
        self.assertEqual(len(self.rows('SELECT * FROM billing_admin_audit')), audit_before)
        self.assertEqual(self.rows('SELECT COUNT(*) AS count FROM billing_admin_drafts')[0]['count'], 0)
        self.assertTrue(self.server.points.audit()['ok'])


if __name__ == '__main__':
    unittest.main()
