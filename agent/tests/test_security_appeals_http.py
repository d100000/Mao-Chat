"""Real HTTP appeal and manual-unlock authorization, with isolated real MFA."""
import json
import sqlite3
import time
import unittest
import uuid

from agent import auth_server
from agent.auth import totp_code
from agent.tests import test_billing_admin_http as fixture
from agent.tests import test_credits_http as credit_fixture


class SecurityAppealsHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.BillingAdminHTTPTests(methodName='runTest')
        self.addCleanup(self.fx.doCleanups)
        self.fx.setUp()
        self.server, self.db, self.admin = self.fx.server, self.fx.db, self.fx.admin
        self.uid, self.actor, self.user = self.fx.target_user('appeal_http_user')

    def prove(self, values, client=None):
        return self.fx.fx.prove('/api/security/appeals', values, client or self.user)[0]

    def submit(self, values=None, client=None):
        client = client or self.user
        request = self.prove(values or {'code': 'benefit_pending'}, client)
        status, result, _ = client.request('/api/security/appeals', 'POST', request)
        self.assertEqual(status, 200, result)
        return result, request

    def post_admin(self, path, values, *, client=None, operation=None):
        return self.fx.post(path, values, client=client or self.admin, operation=operation)

    def review_path(self, appeal):
        return '/api/admin/security/appeals/' + appeal['appeal_id'] + '/review'

    def own_risk(self, client=None):
        status, rejected, _ = (client or self.user).request('/api/points/ledger?limit=0')
        self.assertEqual(status, 400, rejected)
        return rejected['risk_event_id']

    def test_appeal_requires_identity_and_strong_bound_proof(self):
        anonymous = credit_fixture.CookieClient(self.server)
        self.assertEqual(anonymous.request('/api/security/appeals')[0], 401)
        status, denied, _ = anonymous.request('/api/security/appeals', 'POST',
            {'operation_id': uuid.uuid4().hex, 'code': 'benefit_pending'})
        self.assertEqual(status, 401, denied)
        request = {'operation_id': uuid.uuid4().hex, 'code': 'benefit_pending'}
        status, challenge, _ = self.user.request('/api/security/appeals', 'POST', request)
        self.assertEqual(status, 403, challenge)
        self.assertEqual(challenge['code'], 'challenge_required')
        self.assertEqual(self.fx.rows('SELECT COUNT(*) AS count FROM security_appeals')[0]['count'], 0)
        status, denied, _ = self.user.request('/api/security/appeals', 'POST',
            {**request, 'security_proof': 'made-up-proof'})
        self.assertEqual(status, 403, denied)
        proof = self.prove({'code': 'benefit_pending', 'note': '原申诉说明'})
        status, rebound, _ = self.user.request('/api/security/appeals', 'POST',
            {**proof, 'note': '被更换的申诉说明'})
        self.assertEqual(status, 403, rebound)
        self.assertEqual(self.fx.rows('SELECT COUNT(*) AS count FROM security_appeals')[0]['count'], 0)

    def test_risk_binding_own_lists_and_client_identity_override_are_enforced(self):
        risk = self.own_risk()
        appeal, request = self.submit({'risk_event_id': risk, 'note': '共享网络导致的误伤说明'})
        self.assertEqual(appeal['risk_event_id'], risk)
        status, repeated, _ = self.user.request('/api/security/appeals', 'POST', request)
        self.assertEqual(status, 200, repeated)
        self.assertEqual(repeated['appeal_id'], appeal['appeal_id'])
        _, _, other = self.fx.target_user('appeal_http_other')
        foreign = self.prove({'risk_event_id': risk}, other)
        status, denied, _ = other.request('/api/security/appeals', 'POST', foreign)
        self.assertEqual(status, 404, denied)
        self.assertEqual(denied['code'], 'risk_event_missing')
        status, own, _ = self.user.request('/api/security/appeals')
        self.assertEqual(status, 200, own)
        self.assertEqual([item['appeal_id'] for item in own['items']], [appeal['appeal_id']])
        self.assertNotIn('principal_hash', own['items'][0])
        self.assertEqual(other.request('/api/security/appeals')[1]['items'], [])
        status, denied, _ = other.request('/api/security/appeals', 'POST',
            {'operation_id': uuid.uuid4().hex, 'code': 'benefit_pending', 'principal': self.actor['principal']})
        self.assertEqual(status, 400, denied)
        self.assertEqual(other.request('/api/admin/security/appeals')[0], 401)
        status, admin, _ = self.admin.request('/api/admin/security/appeals?status=pending&limit=1')
        self.assertEqual(status, 200, admin)
        self.assertEqual(admin['items'][0]['appeal_id'], appeal['appeal_id'])
        self.assertNotIn('127.0.0.1', json.dumps(admin))

    def test_review_requires_recent_mfa_and_session_identity_without_grant_side_effects(self):
        self.server.require_admin_mfa = True
        self.server.points.set_benefit_pending(self.actor)
        before = self.server.points.wallet(self.actor)
        policy = self.server.points.policy()
        ledger = self.fx.rows('SELECT * FROM points_ledger')
        appeal, _ = self.submit()
        path = self.review_path(appeal)
        values = {'decision': 'approved', 'reason': '人工复核申诉情况属实'}
        status, denied, _ = self.post_admin(path, values, client=self.user)
        self.assertEqual(status, 401, denied)
        for field in ('admin_id', 'reviewed_by', 'principal'):
            status, forged, _ = self.post_admin(path, {**values, field: self.admin.admin_id})
            self.assertEqual(status, 400, forged)
        with sqlite3.connect(self.db) as conn:
            conn.execute('UPDATE admin_sessions SET recent_authenticated_at=?,mfa_verified_at=? WHERE session_id=?',
                         (time.time() - 600, time.time() - 600, self.admin.session_id))
        status, stale, _ = self.post_admin(path, values)
        self.assertEqual(status, 403, stale)
        self.assertEqual(stale['code'], 'reauth_required')
        operation = uuid.uuid4().hex
        status, approved, _ = self.post_admin(path,
            {**values, 'otp_code': totp_code(self.admin.totp_secret, self.admin.login_tick)}, operation=operation)
        self.assertEqual(status, 200, approved)
        self.assertEqual(approved['status'], 'approved')
        status, retried, _ = self.post_admin(path, values, operation=operation)
        self.assertEqual(status, 200, retried)
        self.assertEqual(retried, approved)
        status, changed, _ = self.post_admin(path, {**values, 'decision': 'denied'}, operation=operation)
        self.assertEqual(status, 409, changed)
        self.assertEqual(self.server.points.wallet(self.actor), before)
        self.assertEqual(self.server.points.policy(), policy)
        self.assertEqual(self.fx.rows('SELECT * FROM points_ledger'), ledger)
        self.assertEqual(len(self.fx.rows("SELECT * FROM security_appeal_audit WHERE action='review'")), 1)
        status, own, _ = self.user.request('/api/security/appeals')
        self.assertEqual(status, 200, own)
        self.assertEqual(own['items'][0]['status'], 'approved')
        self.assertEqual(own['items'][0]['review_reason'], values['reason'])

    def test_password_only_or_revoked_admin_cannot_review(self):
        appeal, _ = self.submit()
        path = self.review_path(appeal)
        values = {'decision': 'denied', 'reason': '有效身份及动态验证测试'}
        password_only = self.fx.admin_client('appeal_no_mfa', mfa=False)
        self.server.require_admin_mfa = True
        self.assertEqual(self.post_admin(path, values, client=password_only)[0], 401)
        self.server.auth_service.admin_logout(self.admin.cookies[auth_server.ADMIN_SESSION_COOKIE])
        status, denied, _ = self.post_admin(path, values)
        self.assertEqual(status, 401, denied)
        self.assertEqual(self.user.request('/api/security/appeals')[1]['items'][0]['status'], 'pending')

    def test_unlock_requires_reason_operation_and_recent_admin_then_audits_once(self):
        lock_path, unlock_path = ('/api/admin/users/' + str(self.uid) + '/' + verb for verb in ('lock', 'unlock'))
        status, locked, _ = self.post_admin(lock_path, {'reason': '人工确认账户应暂停使用'})
        self.assertEqual(status, 200, locked)
        self.assertEqual(self.user.request('/api/points/wallet')[0], 401)
        for values in ({'operation_id': uuid.uuid4().hex}, {'reason': '没有操作标识'}):
            status, denied, _ = self.admin.request(unlock_path, 'POST', values)
            self.assertEqual(status, 400, denied)
        status, forged, _ = self.post_admin(unlock_path,
            {'reason': '伪造管理员字段测试', 'admin_id': 123456})
        self.assertEqual(status, 400, forged)
        with sqlite3.connect(self.db) as conn:
            conn.execute('UPDATE admin_sessions SET recent_authenticated_at=?,mfa_verified_at=? WHERE session_id=?',
                         (time.time() - 600, time.time() - 600, self.admin.session_id))
        status, stale, _ = self.post_admin(unlock_path, {'reason': '人工核验后解除账户限制'})
        self.assertEqual(status, 403, stale)
        operation = uuid.uuid4().hex
        values = {'reason': '人工核验后解除账户限制'}
        status, unlocked, _ = self.post_admin(unlock_path,
            {**values, 'otp_code': totp_code(self.admin.totp_secret, self.admin.login_tick)}, operation=operation)
        self.assertEqual(status, 200, unlocked)
        self.assertEqual(self.post_admin(unlock_path, values, operation=operation)[0], 200)
        status, changed, _ = self.post_admin(lock_path, values, operation=operation)
        self.assertEqual(status, 409, changed)
        rows = self.fx.rows('SELECT * FROM auth_admin_operations ORDER BY created_at')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]['admin_id'], self.admin.admin_id)
        self.assertEqual(rows[-1]['action'], 'admin_user_unlock')
        self.assertEqual(rows[-1]['reason'], values['reason'])
        self.assertEqual(self.fx.rows('SELECT disabled FROM users WHERE user_id=' + str(self.uid))[0]['disabled'], 0)
        # Administrative unlock does not reactivate the revoked cookie.
        self.assertEqual(self.user.request('/api/points/wallet')[0], 401)

    def test_bounded_note_pagination_and_bad_reasons_leave_appeal_pending(self):
        appeal, _ = self.submit({'code': 'benefit_pending', 'note': '一次正常的申诉说明'})
        path = self.review_path(appeal)
        for values in ({'decision': 'approved'}, {'decision': 'approved', 'reason': ''},
                       {'decision': 'approved', 'reason': 'token=private-test-placeholder'},
                       {'decision': 'global_unlock', 'reason': '尝试超出申诉权限'}):
            status, denied, _ = self.post_admin(path, values)
            self.assertEqual(status, 400, denied)
        request = self.prove({'code': 'rate_limited', 'note': 'x' * 501})
        status, too_long, _ = self.user.request('/api/security/appeals', 'POST', request)
        self.assertEqual(status, 400, too_long)
        for route in ('/api/security/appeals?limit=101', '/api/security/appeals?offset=-1'):
            self.assertEqual(self.user.request(route)[0], 400)
        self.assertEqual(self.admin.request('/api/admin/security/appeals?limit=101')[0], 400)
        self.assertEqual(self.user.request('/api/security/appeals')[1]['items'][0]['status'], 'pending')
        self.assertEqual(self.fx.rows('SELECT COUNT(*) AS count FROM security_appeals')[0]['count'], 1)


if __name__ == '__main__':
    unittest.main()
