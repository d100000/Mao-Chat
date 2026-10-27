"""Projection maintenance through real HTTP with isolated money and MFA.

Compose the existing administrative fixture so its unrelated test methods are
not inherited or rediscovered. Provider I/O remains fake and no keys are read.
"""
import json
import sqlite3
import threading
import time
import unittest
import uuid

from agent.auth import totp_code
from agent.points import MICRO
from agent.tests import test_billing_admin_http as fixture
from agent.tests.test_credits_http import CookieClient


class ProjectionHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.BillingAdminHTTPTests(methodName='runTest')
        self.addCleanup(self.fx.doCleanups)
        self.fx.setUp()
        self.server, self.db, self.admin = self.fx.server, self.fx.db, self.fx.admin

    def rows(self, query, args=()):
        return self.fx.rows(query, args)

    def rebuild(self, *, client=None, operation=None, reason='核对原账后重建余额投影', **extra):
        return self.fx.post('/api/admin/billing/rebuild-projection', {'reason': reason, **extra},
                            client=client, operation=operation)

    def corrupt_projection(self):
        lot = self.rows('SELECT * FROM points_lots ORDER BY id LIMIT 1')[0]
        with sqlite3.connect(self.db) as conn:
            conn.execute('UPDATE points_lots SET available=available-?,spent=spent+? WHERE id=?', (MICRO, MICRO, lot['id']))
        return lot

    def test_http_audit_and_rebuild_repair_real_projection_without_postings_or_grants(self):
        charge = self.fx.generated_request()
        lots = self.rows('SELECT * FROM points_lots ORDER BY id')
        ledger = self.rows('SELECT * FROM points_ledger ORDER BY id')
        requests = self.rows('SELECT * FROM points_requests ORDER BY id')
        cash = self.rows('SELECT * FROM points_cash_commitments ORDER BY request_id')
        corrupt = self.corrupt_projection()
        status, inspected, _ = self.admin.request('/api/admin/billing/projection-audit')
        self.assertEqual(status, 200, inspected)
        self.assertEqual(inspected['audit'], {'ok': False, 'mismatched_lots': [corrupt['id']]})
        operation = uuid.uuid4().hex
        status, repaired, _ = self.rebuild(operation=operation)
        self.assertEqual(status, 200, repaired)
        self.assertEqual(repaired['status'], 'rebuilt')
        self.assertEqual(repaired['repaired_lots'], 1)
        self.assertEqual(repaired['requests_checked'], 1)
        self.assertEqual(self.rows('SELECT * FROM points_lots ORDER BY id'), lots)
        self.assertEqual(self.rows('SELECT * FROM points_ledger ORDER BY id'), ledger)
        self.assertEqual(self.rows('SELECT * FROM points_requests ORDER BY id'), requests)
        self.assertEqual(self.rows('SELECT * FROM points_cash_commitments ORDER BY request_id'), cash)
        self.assertEqual(self.admin.request('/api/admin/billing/projection-audit')[1]['audit'], {'ok': True, 'mismatched_lots': []})
        status, retry, _ = self.rebuild(operation=operation)
        self.assertEqual(status, 200, retry)
        self.assertEqual(retry['result'], repaired['result'])
        status, conflict, _ = self.rebuild(operation=operation, reason='同一操作标识改为另一原因')
        self.assertEqual(status, 409, conflict)
        self.assertEqual(conflict['code'], 'idempotency_conflict')
        audit = self.rows('SELECT * FROM billing_admin_audit')
        self.assertEqual(len(audit), 1)
        self.assertEqual((audit[0]['action'], audit[0]['admin_id']), ('projection.rebuild', self.admin.admin_id))
        self.assertEqual(audit[0]['operation_id'], operation)
        self.assertEqual(json.loads(audit[0]['parameters'])['reason'], '核对原账后重建余额投影')
        self.assertEqual(self.rows('SELECT charged_micro FROM points_requests WHERE id=?', (charge['request_id'],))[0]['charged_micro'], requests[0]['charged_micro'])

    def test_non_admin_and_expired_admin_cannot_read_or_rebuild(self):
        self.fx.fx.bootstrap()
        _, _, user = self.fx.target_user('projection_regular_user')
        expired = self.fx.admin_client('projection_expired_admin')
        with sqlite3.connect(self.db) as conn:
            conn.execute('UPDATE admin_sessions SET expires_at=? WHERE session_id=?', (time.time()-1, expired.session_id))
        before = self.rows('SELECT * FROM points_lots ORDER BY id')
        for client in (CookieClient(self.server), self.fx.fx.client, user, expired):
            status, result, _ = client.request('/api/admin/billing/projection-audit')
            self.assertEqual(status, 401, result)
            self.assertNotIn('audit', result)
            status, result, _ = self.rebuild(client=client)
            self.assertEqual(status, 401, result)
        self.assertEqual(self.rows('SELECT * FROM points_lots ORDER BY id'), before)
        self.assertEqual(self.rows('SELECT * FROM billing_admin_audit'), [])

    def test_rebuild_requires_recent_mfa_and_accepts_fresh_code_once(self):
        self.fx.target_user('projection_mfa_user')
        expected = self.rows('SELECT * FROM points_lots ORDER BY id')
        self.corrupt_projection()
        corrupted = self.rows('SELECT * FROM points_lots ORDER BY id')
        self.server.require_admin_mfa = True
        old = time.time()-600
        with sqlite3.connect(self.db) as conn:
            conn.execute('UPDATE admin_sessions SET recent_authenticated_at=?,mfa_verified_at=? WHERE session_id=?', (old, old, self.admin.session_id))
        self.assertEqual(self.admin.request('/api/admin/billing/projection-audit')[0], 200)
        for payload in ({}, {'otp_code': 'invalid'}):
            status, rejected, _ = self.rebuild(**payload)
            self.assertEqual(status, 403, rejected)
            self.assertEqual(rejected['code'], 'reauth_required')
            self.assertEqual(self.rows('SELECT * FROM points_lots ORDER BY id'), corrupted)
        fresh = totp_code(self.admin.totp_secret, self.admin.login_tick)
        status, accepted, _ = self.rebuild(otp_code=fresh)
        self.assertEqual(status, 200, accepted)
        self.assertEqual(self.rows('SELECT * FROM points_lots ORDER BY id'), expected)
        status, replayed, _ = self.rebuild(otp_code=fresh)
        self.assertEqual(status, 403, replayed)
        self.assertEqual(len(self.rows('SELECT * FROM billing_admin_audit')), 1)
        password_only = self.fx.admin_client('projection_password_only', mfa=False)
        self.assertEqual(self.rebuild(client=password_only)[0], 401)

    def test_running_and_usage_pending_http_requests_block_maintenance(self):
        fx = self.fx.fx
        fx.bootstrap()
        payload, quote = fx.quoted(thread_id='projection-running')
        self.server.agent.pause = True
        result = []
        worker = threading.Thread(target=lambda: result.append(fx.post('/api/agent/stream', {**payload, 'quote_id': quote['quote_id']})), daemon=True)
        worker.start()
        try:
            self.assertTrue(self.server.agent.started.wait(3))
            ledger = self.rows('SELECT * FROM points_ledger ORDER BY id')
            lots = self.rows('SELECT * FROM points_lots ORDER BY id')
            status, denied, _ = self.rebuild()
            self.assertEqual(status, 409, denied)
            self.assertEqual(denied['code'], 'projection_busy')
            self.assertEqual(self.rows('SELECT * FROM points_ledger ORDER BY id'), ledger)
            self.assertEqual(self.rows('SELECT * FROM points_lots ORDER BY id'), lots)
        finally:
            self.server.agent.finish_gate.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0][0], 200)
        self.server.agent.pause = False
        pending = self.fx.generated_request(unknown=True)
        self.assertEqual(pending['state'], 'usage_pending')
        status, denied, _ = self.rebuild()
        self.assertEqual(status, 409, denied)
        self.assertEqual(denied['code'], 'projection_busy')
        self.assertEqual(self.rows('SELECT * FROM billing_admin_audit'), [])

    def test_bad_source_ledger_refuses_whole_rebuild_without_audit_success(self):
        self.fx.target_user('projection_bad_source')
        lot = self.corrupt_projection()
        with self.server.points._tx() as conn:
            self.server.points._entry(conn, 'test-corrupt-posting', lot['wallet_id'], lot['id'], 'expire',
                                      200*MICRO, available=-200*MICRO, expired=200*MICRO)
        lots = self.rows('SELECT * FROM points_lots ORDER BY id')
        ledger = self.rows('SELECT * FROM points_ledger ORDER BY id')
        status, denied, _ = self.rebuild()
        self.assertEqual(status, 409, denied)
        self.assertEqual(denied['code'], 'projection_source_invalid')
        self.assertEqual(self.rows('SELECT * FROM points_lots ORDER BY id'), lots)
        self.assertEqual(self.rows('SELECT * FROM points_ledger ORDER BY id'), ledger)
        self.assertEqual(self.rows('SELECT * FROM billing_admin_audit'), [])

    def test_audit_failure_rolls_back_http_repair_and_untrusted_fields_are_rejected(self):
        self.fx.target_user('projection_journal_failure')
        self.corrupt_projection()
        lots = self.rows('SELECT * FROM points_lots ORDER BY id')
        for payload in ({'reviewed_by': 2}, {'admin_id': self.admin.admin_id}, {'available': '100'}, {'reason': ''}):
            status, result, _ = self.fx.post('/api/admin/billing/rebuild-projection', {'reason': '核对原账后重建余额投影', **payload})
            self.assertEqual(status, 400, result)
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TRIGGER test_projection_audit_failure BEFORE INSERT ON billing_admin_audit BEGIN SELECT RAISE(ABORT,'isolated journal failure'); END")
        status, result, _ = self.rebuild()
        self.assertEqual(status, 500, result)
        self.assertEqual(self.rows('SELECT * FROM points_lots ORDER BY id'), lots)
        self.assertEqual(self.rows('SELECT * FROM billing_admin_audit'), [])
        self.assertNotIn('isolated journal failure', json.dumps(result))

    def test_read_only_audit_does_not_issue_or_expire_grants(self):
        fx = self.fx.fx
        fx.bootstrap()
        self.assertEqual(self.rows('SELECT * FROM points_lots'), [])
        self.assertEqual(self.rows('SELECT * FROM points_ledger'), [])
        for _ in range(2):
            status, result, _ = self.admin.request('/api/admin/billing/projection-audit')
            self.assertEqual(status, 200, result)
            self.assertEqual(result['audit'], {'ok': True, 'mismatched_lots': []})
        self.assertEqual(self.rows('SELECT * FROM points_lots'), [])
        self.assertEqual(self.rows('SELECT * FROM points_ledger'), [])
        self.fx.target_user('projection_expiry_read_only')
        lots = self.rows('SELECT * FROM points_lots ORDER BY id')
        ledger = self.rows('SELECT * FROM points_ledger ORDER BY id')
        later = self.server.points._now()+2*86400
        self.server.points.clock = lambda: later
        status, result, _ = self.admin.request('/api/admin/billing/projection-audit')
        self.assertEqual(status, 200, result)
        self.assertEqual(self.rows('SELECT * FROM points_lots ORDER BY id'), lots)
        self.assertEqual(self.rows('SELECT * FROM points_ledger ORDER BY id'), ledger)


if __name__ == '__main__':
    unittest.main()
