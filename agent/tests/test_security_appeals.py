import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from agent.auth import AuthService
from agent.points import PointsStore
from agent.risk_audit import RiskAuditStore
from agent.security import SecurityError
from agent.security_appeals import SecurityAppealsStore


class SecurityAppealsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='jiaoyuan-appeals-')
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'appeals.db'
        self.now = 200000.0
        self.secret = b'isolated-appeals-test-secret-32-bytes'
        self.auth = AuthService(self.db)
        self.admin = self.auth.bootstrap_admin('appeal_admin', 'isolated-password-123')['admin_id']
        self.risks = RiskAuditStore(self.db, self.secret, clock=lambda: self.now)
        self.store = SecurityAppealsStore(self.db, self.secret, clock=lambda: self.now)

    def event(self, principal='u:1'):
        return self.risks.record('register', 'benefit_pending', principal=principal, source='192.0.2.1')

    def submit(self, principal='u:1', operation='appeal-operation-001', **values):
        return self.store.submit(principal, operation, code='benefit_pending', **values)

    def rows(self, table):
        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute('SELECT * FROM ' + table)]

    def test_submit_and_read_are_bound_to_current_principal(self):
        event = self.event()
        result = self.store.submit('u:1', 'appeal-operation-001', risk_event_id=event, note='使用共享网络时被误判')
        self.assertEqual(result['status'], 'pending')
        own = self.store.get_own('u:1', result['appeal_id'])
        self.assertEqual(own['note'], '使用共享网络时被误判')
        self.assertNotIn('principal_hash', own)
        self.assertNotIn('reviewed_by', own)
        with self.assertRaises(SecurityError) as denied: self.store.get_own('u:2', result['appeal_id'])
        self.assertEqual(denied.exception.status, 404)
        with self.assertRaises(SecurityError) as denied:
            self.store.submit('u:2', 'appeal-operation-002', risk_event_id=event)
        self.assertEqual(denied.exception.status, 404)
        self.assertEqual(self.store.list_own('u:2')['total'], 0)
        self.assertTrue(self.store.matches_principal(result['appeal_id'], 'u:1'))
        self.assertFalse(self.store.matches_principal(result['appeal_id'], 'u:2'))

    def test_receipt_replays_original_submit_after_review_without_note_in_audit(self):
        first = self.submit(note='这是可选的误伤申诉说明')
        self.store.review(first['appeal_id'], 'denied', self.admin, 'review-operation-001', '已经核实原限制仍然适用')
        restarted = SecurityAppealsStore(self.db, self.secret, clock=lambda: self.now)
        self.assertEqual(first, restarted.submit('u:1', 'appeal-operation-001', code='benefit_pending', note='这是可选的误伤申诉说明'))
        self.assertEqual(self.store.get_own('u:1', first['appeal_id'])['status'], 'denied')
        with self.assertRaises(SecurityError) as conflict:
            self.submit(note='修改后的申诉内容')
        self.assertEqual(conflict.exception.status, 409)
        for table in ('security_appeal_operations', 'security_appeal_audit'):
            data = json.dumps(self.rows(table), ensure_ascii=False)
            self.assertNotIn('这是可选的误伤申诉说明', data)
            self.assertNotIn('已经核实原限制仍然适用', data)
            self.assertNotIn('appeal-operation-001', data)
            self.assertNotIn('192.0.2.1', data)
        self.assertNotIn('u:1', json.dumps(self.rows('security_appeals')))

    def test_review_is_authenticated_closed_and_idempotent(self):
        appeal = self.submit()
        with self.assertRaises(SecurityError) as denied:
            self.store.review(appeal['appeal_id'], 'approved', 999, 'review-operation-001', '核实后批准申诉')
        self.assertEqual(denied.exception.status, 403)
        result = self.store.review(appeal['appeal_id'], 'approved', self.admin, 'review-operation-001', '核实后批准申诉')
        self.assertEqual(result, self.store.review(appeal['appeal_id'], 'approved', self.admin, 'review-operation-001', '核实后批准申诉'))
        with self.assertRaises(SecurityError) as closed:
            self.store.review(appeal['appeal_id'], 'denied', self.admin, 'review-operation-002', '尝试覆盖已经完成的结论')
        self.assertEqual(closed.exception.code, 'appeal_closed')
        with self.assertRaises(SecurityError) as conflict:
            self.store.review(appeal['appeal_id'], 'denied', self.admin, 'review-operation-001', '尝试覆盖已经完成的结论')
        self.assertEqual(conflict.exception.status, 409)
        own = self.store.get_own('u:1', appeal['appeal_id'])
        self.assertEqual(own['review_reason'], '核实后批准申诉')
        self.assertEqual(len(self.rows('security_appeal_audit')), 2)
        self.assertEqual(self.store.list_admin(status='approved')['total'], 1)

    def test_approval_cannot_unlock_account_grant_points_or_bypass_budget(self):
        user = self.auth.register('appeal_user', 'isolated-password-123', require_captcha=False)
        points = PointsStore(self.db, clock=lambda: self.now)
        actor = points.user_actor(user['user_id'])
        points.configure({'daily_cash_limit_cny': '0.01'})
        points.set_benefit_pending(actor)
        self.auth.admin_set_user_lock(user['user_id'], True)
        before = points.wallet(actor)
        ledger_before = self.rows('points_ledger')
        appeal = self.submit(principal=actor['principal'])
        self.store.review(appeal['appeal_id'], 'approved', self.admin, 'review-no-side-effects', '人工核实申诉情况属实')
        self.assertEqual(points.wallet(actor), before)
        self.assertEqual(self.rows('points_ledger'), ledger_before)
        self.assertEqual(points.policy()['daily_cash_limit_cny'], '0.01')
        self.assertEqual(next(row for row in self.rows('users') if row['user_id'] == user['user_id'])['disabled'], 1)

    def test_concurrent_submit_and_review_record_each_decision_once(self):
        results, errors = [], []
        barrier = threading.Barrier(6)
        def submit():
            try:
                store = SecurityAppealsStore(self.db, self.secret, clock=lambda: self.now)
                barrier.wait(timeout=5)
                results.append(store.submit('u:1', 'concurrent-operation', code='rate_limited'))
            except Exception as exc: errors.append(exc)
        threads = [threading.Thread(target=submit) for _ in range(6)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=10)
        self.assertFalse(errors)
        self.assertEqual(len(results), 6)
        self.assertTrue(all(result == results[0] for result in results))
        identity = results[0]['appeal_id']
        barrier = threading.Barrier(6); results.clear()
        def review():
            try:
                store = SecurityAppealsStore(self.db, self.secret, clock=lambda: self.now)
                barrier.wait(timeout=5)
                results.append(store.review(identity, 'approved', self.admin, 'concurrent-review', '核验通过可以结束申诉'))
            except Exception as exc: errors.append(exc)
        threads = [threading.Thread(target=review) for _ in range(6)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=10)
        self.assertFalse(errors)
        self.assertEqual(len(results), 6)
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(len(self.rows('security_appeals')), 1)
        self.assertEqual(len(self.rows('security_appeal_operations')), 2)
        self.assertEqual(len(self.rows('security_appeal_audit')), 2)

    def test_audit_insert_failure_rolls_back_review(self):
        appeal = self.submit()
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TRIGGER appeal_audit_failure BEFORE INSERT ON security_appeal_audit WHEN NEW.action='review' BEGIN SELECT RAISE(ABORT,'simulated audit failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.review(appeal['appeal_id'], 'approved', self.admin, 'failed-audit-review', '模拟审核事务回滚')
        self.assertEqual(self.store.get_own('u:1', appeal['appeal_id'])['status'], 'pending')
        self.assertEqual(len(self.rows('security_appeal_operations')), 1)

    def test_pending_and_global_capacity_limits_do_not_evict_or_overwrite(self):
        for index in range(3): self.submit(operation='pending-operation-' + str(index))
        with self.assertRaises(SecurityError) as limit: self.submit(operation='pending-operation-extra')
        self.assertEqual(limit.exception.status, 429)
        self.assertEqual(self.store.list_own('u:1')['total'], 3)
        cap = SecurityAppealsStore(self.db, self.secret, max_appeals=3, clock=lambda: self.now)
        with self.assertRaises(SecurityError) as full: cap.submit('u:2', 'capacity-operation', code='rate_limited')
        self.assertEqual(full.exception.status, 503)
        self.assertEqual(self.store.list_admin()['total'], 3)

    def test_pagination_and_expiry_delete_private_notes_receipts_and_audit(self):
        for index in range(3): self.submit(operation='pagination-operation-' + str(index))
        first = self.store.list_own('u:1', limit=1)
        second = self.store.list_own('u:1', limit=1, offset=first['next_offset'])
        self.assertNotEqual(first['items'][0]['appeal_id'], second['items'][0]['appeal_id'])
        for kwargs in ({'limit': 101}, {'limit': True}, {'offset': -1}, {'offset': 10001}, {'status': 'all'}):
            with self.assertRaises(SecurityError): self.store.list_admin(**kwargs)
        self.now += 180 * 86400 + 1
        self.assertEqual(self.store.list_admin()['total'], 0)
        self.assertEqual(self.store.list_own('u:1')['total'], 0)
        self.assertEqual(self.store.cleanup(), 3)
        for table in ('security_appeals', 'security_appeal_operations', 'security_appeal_audit'):
            self.assertEqual(self.rows(table), [])

    def test_invalid_note_credentials_codes_and_risk_binding_leave_no_records(self):
        for kwargs in ({'note': 'x' * 501}, {'note': 'password=do-not-store-this'},
                       {'note': 'sk-' + ('a' * 32)},
                       {'code': 'clear_all_limits'}, {'risk_event_id': 'risk_fake'},
                       {'risk_event_id': 'risk_' + 'a' * 32}, {'note': '\x00not-allowed'}):
            with self.assertRaises(SecurityError):
                self.store.submit('u:1', 'invalid-operation', **{'code': 'benefit_pending', **kwargs})
        self.assertEqual(self.rows('security_appeals'), [])
        event = self.event()
        with self.assertRaises(SecurityError):
            self.store.submit('u:1', 'invalid-code-event', risk_event_id=event, code='rate_limited')
        self.now += 180 * 86400 + 1
        with self.assertRaises(SecurityError) as expired:
            self.store.submit('u:1', 'expired-event-operation', risk_event_id=event)
        self.assertEqual(expired.exception.status, 404)

    def test_disabled_admin_cannot_review_and_day_count_is_not_reset_by_closing(self):
        for index in range(10):
            appeal = self.submit(operation='daily-appeal-' + str(index))
            self.store.review(appeal['appeal_id'], 'denied', self.admin, 'daily-review-' + str(index), '已经复核申诉内容')
        with self.assertRaises(SecurityError) as daily: self.submit(operation='daily-overflow-operation')
        self.assertEqual(daily.exception.status, 429)
        self.now += 86401
        appeal = self.submit(operation='next-day-operation')
        with sqlite3.connect(self.db) as conn: conn.execute('UPDATE admins SET disabled=1 WHERE admin_id=?', (self.admin,))
        with self.assertRaises(SecurityError) as disabled:
            self.store.review(appeal['appeal_id'], 'approved', self.admin, 'disabled-review-operation', '管理员已经停用测试')
        self.assertEqual(disabled.exception.status, 403)
        self.assertEqual(self.store.get_own('u:1', appeal['appeal_id'])['status'], 'pending')


if __name__ == '__main__':
    unittest.main()
