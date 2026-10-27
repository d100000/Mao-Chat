import base64
import sqlite3
import json
import tempfile
import unittest
import re
import threading
from pathlib import Path
from unittest.mock import patch

from agent.auth import AuthService, AuthError, totp_code


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mao-auth-")
        self.db = Path(self.tmp.name) / "auth.db"
        self.auth = AuthService(self.db, captcha_ttl=60, session_ttl=60, max_captcha_attempts=3)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _answer(prompt):
        left, op, right = prompt.split()[:3]
        a, b = int(left), int(right)
        return a + b if op == "+" else a - b if op == "-" else a * b

    @staticmethod
    def _expression(challenge):
        encoded = challenge["image"].split(",", 1)[1]
        svg = base64.b64decode(encoded).decode("utf-8")
        return re.search(r">(\d+ [+\-×] \d+ = \?)</text>", svg).group(1)

    def _row(self, table, key):
        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute("SELECT * FROM %s WHERE challenge_id=?" % table, (key,)).fetchone()

    def captcha(self, service=None):
        service = service or self.auth
        math = service.create_math_challenge()
        expression = self._expression(math)
        return {"math_challenge_id": math["challenge_id"], "math_answer": self._answer(expression)}

    def test_register_login_session_logout(self):
        result = self.auth.register("alice_1", "correct horse battery staple", **self.captcha())
        self.assertTrue(result["ok"])
        login = self.auth.login("ALICE_1", "correct horse battery staple", **self.captcha())
        self.assertTrue(login["ok"])
        token = login["session_token"]
        self.assertTrue(self.auth.validate_session(token)["ok"])
        self.assertTrue(self.auth.logout(token)["ok"])
        self.assertFalse(self.auth.validate_session(token)["ok"])

    def test_duplicate_and_wrong_password(self):
        self.assertTrue(self.auth.register("alice", "password-123", **self.captcha())["ok"])
        duplicate = self.auth.register("ALICE", "password-123", **self.captcha())
        self.assertFalse(duplicate["ok"])
        wrong = self.auth.login("alice", "wrong-password", **self.captcha())
        self.assertFalse(wrong["ok"])

    def test_captcha_is_one_use_and_wrong_attempts_fail(self):
        c = self.captcha()
        self.assertFalse(self.auth.verify_math(c["math_challenge_id"], "999999")["ok"])
        self.assertTrue(self.auth.verify_math(c["math_challenge_id"], c["math_answer"])["ok"])
        self.assertFalse(self.auth.verify_math(c["math_challenge_id"], c["math_answer"])["ok"])

        limited = AuthService(self.db, max_captcha_attempts=2)
        math = limited.create_math_challenge()
        answer = self._answer(self._expression(math))
        wrong = str(int(answer) + 1)  # Zero can be a valid random subtraction answer.
        self.assertFalse(limited.verify_math(math["challenge_id"], wrong)["ok"])
        self.assertFalse(limited.verify_math(math["challenge_id"], wrong)["ok"])
        self.assertFalse(limited.verify_math(math["challenge_id"], answer)["ok"])

    def test_challenge_consumption_is_atomic_under_concurrency(self):
        math = self.auth.create_math_challenge()
        answer = self._answer(self._expression(math))
        barrier = threading.Barrier(2)
        results, errors = [], []

        def worker():
            try:
                barrier.wait(timeout=3)
                results.append(self.auth.verify_math(math["challenge_id"], answer)["ok"])
            except Exception as exc:  # pragma: no cover - diagnostic guard
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(errors)
        self.assertEqual(sum(bool(value) for value in results), 1)

    def test_expired_challenge_and_safe_storage(self):
        auth = AuthService(self.db, captcha_ttl=0)
        math = auth.create_math_challenge()
        expression = self._expression(math)
        self.assertFalse(auth.verify_math(math["challenge_id"], self._answer(expression))["ok"])
        self.assertTrue(self.auth.register("bob", "password-123", **self.captcha())["ok"])
        login = self.auth.login("bob", "password-123", **self.captcha())
        self.assertTrue(login["ok"])
        with sqlite3.connect(self.db) as conn:
            password_hashes = conn.execute("SELECT password_hash FROM users").fetchall()
            salts = conn.execute("SELECT password_salt FROM users").fetchall()
            answer_hashes = conn.execute("SELECT answer_hash FROM captcha_challenges WHERE answer_hash IS NOT NULL").fetchall()
            token_hashes = conn.execute("SELECT token_hash FROM auth_sessions").fetchall()
            self.assertTrue(all(len(str(row[0])) >= 32 and row[0] != "password-123" for row in password_hashes))
            self.assertTrue(all(len(str(row[0])) >= 16 and row[0] != "password-123" for row in salts))
            self.assertTrue(all(len(str(row[0])) == 64 for row in answer_hashes))
            self.assertTrue(all(len(str(row[0])) == 64 for row in token_hashes))
            text = " ".join(str(row[0]) for row in conn.execute("SELECT metadata_json FROM auth_audit"))
            self.assertNotIn("password-123", text)

    def test_login_lock_after_repeated_bad_passwords(self):
        auth = AuthService(self.db, max_login_attempts=2, login_lock_seconds=60)
        self.assertTrue(auth.register("locked", "password-123", **self.captcha(auth))["ok"])
        self.assertFalse(auth.login("locked", "wrong-pass", **self.captcha(auth))["ok"])
        self.assertFalse(auth.login("locked", "wrong-pass", **self.captcha(auth))["ok"])
        # A valid password is still rejected during the temporary lock.
        self.assertFalse(auth.login("locked", "password-123", **self.captcha(auth))["ok"])
        with sqlite3.connect(self.db) as conn:
            failed, locked_until = conn.execute("SELECT failed_logins, locked_until FROM users WHERE username_norm='locked'").fetchone()
            self.assertGreaterEqual(failed, 2)
            self.assertIsNotNone(locked_until)

    def test_stable_secret_keeps_session_after_service_restart(self):
        self.assertTrue(self.auth.register("restart", "password-123", **self.captcha())["ok"])
        login = self.auth.login("restart", "password-123", **self.captcha())
        self.assertTrue(login["ok"])
        restarted = AuthService(self.db, session_ttl=60)
        self.assertTrue(restarted.validate_session(login["session_token"])["ok"])

    def test_session_secret_survives_service_restart(self):
        result = self.auth.register("restart_user", "password-123", **self.captcha())
        self.assertTrue(result["ok"])
        login = self.auth.login("restart_user", "password-123", **self.captcha())
        self.assertTrue(login["ok"])
        restarted = AuthService(self.db, captcha_ttl=60, session_ttl=60)
        self.assertTrue(restarted.validate_session(login["session_token"])["ok"])
        self.assertEqual(self.db.stat().st_mode & 0o777, 0o600)

    def test_username_and_path_safe_shape(self):
        with self.assertRaises(ValueError):
            self.auth.register("../escape", "password-123", **self.captcha())
        self.assertTrue(self.auth.cleanup() is not None)

    def test_cleanup_removes_expired_state_and_old_audit_rows(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO auth_audit(occurred_at,action,success) VALUES(0,'old',0)")
            conn.execute("INSERT INTO auth_rate_limits(rate_key_hash,window_start,attempts) VALUES('old',0,1)")
            conn.commit()
        result = self.auth.cleanup()
        self.assertEqual(result["audits"], 1)
        self.assertEqual(result["rates"], 1)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM auth_audit WHERE action='old'").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM auth_rate_limits WHERE rate_key_hash='old'").fetchone()[0], 0)

    def test_malformed_password_record_fails_closed(self):
        self.assertTrue(self.auth.register("alice", "correct horse battery staple", **self.captcha())["ok"])
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE users SET password_salt=? WHERE username_norm=?", ("not-base64", "alice"))
            conn.commit()
        result = self.auth.login("alice", "correct horse battery staple", **self.captcha())
        self.assertFalse(result["ok"])

    def test_failed_login_cooldown_does_not_revoke_an_existing_session(self):
        auth=AuthService(self.db,max_login_attempts=2,login_lock_seconds=60)
        registered=auth.register('active_user','password-123',**self.captcha(auth))
        login=auth.login('active_user','password-123',**self.captcha(auth))
        for _ in range(2):
            self.assertFalse(auth.login('active_user','wrong-password',**self.captcha(auth))['ok'])
        self.assertTrue(auth.validate_session(login['session_token'])['ok'])
        listing=auth.admin_list_users()['users'][0]
        self.assertFalse(listing['locked']); self.assertTrue(listing['login_cooling_down'])
        # Explicit administrator suspension is a separate, revoking action.
        auth.admin_set_user_lock(registered['user_id'],True)
        self.assertFalse(auth.validate_session(login['session_token'])['ok'])
        auth.admin_set_user_lock(registered['user_id'],False)
        self.assertFalse(auth.validate_session(login['session_token'])['ok'])

    def test_invalid_captcha_attempts_still_consume_persistent_source_budget(self):
        for _ in range(30):
            result=self.auth.login('any_user','password-123',rate_key='192.0.2.5')
            self.assertFalse(result['ok'])
        restarted=AuthService(self.db)
        result=restarted.login('another_user','password-123',rate_key='192.0.2.5')
        self.assertEqual(result.get('code'),'rate_limited')

    def test_invalid_challenges_cannot_consume_a_victims_account_cooldown(self):
        self.auth.register('protected_user','password-123',**self.captcha())
        for _ in range(25):
            self.assertFalse(self.auth.login('protected_user','wrong-password')['ok'])
        self.assertTrue(self.auth.login('protected_user','password-123',**self.captcha())['ok'])

    def test_registration_and_admin_source_limits_cover_bad_challenges(self):
        for i in range(20):
            self.assertFalse(self.auth.register('new_user'+str(i),'password-123',rate_key='192.0.2.6')['ok'])
        self.assertEqual(self.auth.register('next_user','password-123',rate_key='192.0.2.6').get('code'),'rate_limited')
        for _ in range(30):
            self.assertFalse(self.auth.admin_login('admin_user','password-123',rate_key='192.0.2.7')['ok'])
        self.assertEqual(self.auth.admin_login('different_admin','password-123',rate_key='192.0.2.7').get('code'),'rate_limited')

    def test_active_cooldown_not_extended_by_repeated_attempts(self):
        auth=AuthService(self.db,max_login_attempts=2,login_lock_seconds=60)
        with patch('agent.auth._now',return_value=1000):
            auth.register('cold_user','password-123',require_captcha=False)
            for _ in range(2): auth.login('cold_user','wrong-password',require_captcha=False)
        with patch('agent.auth._now',return_value=1020):
            auth.login('cold_user','wrong-password',require_captcha=False)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT locked_until FROM users WHERE username_norm='cold_user'").fetchone()[0],1060)
        with patch('agent.auth._now',return_value=1061):
            auth.login('cold_user','wrong-password',require_captcha=False)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT locked_until FROM users WHERE username_norm='cold_user'").fetchone()[0],1181)

    def test_second_factor_uses_rfc6238_and_cannot_be_replayed(self):
        # RFC 6238 Appendix B SHA-1 vector at t=59.
        secret='GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'
        self.assertEqual(totp_code(secret,59,digits=8),'94287082')
        admin=self.auth.bootstrap_admin('root_test','admin-password-123')
        before=self.auth.admin_login('root_test','admin-password-123',**self.captcha())
        enrollment=self.auth.configure_admin_totp(admin['admin_id'],secret)
        self.assertIn('otpauth://totp/',enrollment['uri'])
        self.assertEqual(self.auth.totp_secret_path.stat().st_mode&0o777,0o600)
        self.assertFalse(self.auth.validate_admin_session(before['session_token'])['ok'])
        with patch('agent.auth._now',return_value=1000):
            self.assertFalse(self.auth.admin_login('root_test','admin-password-123',require_captcha=False)['ok'])
            code=totp_code(secret,1000)
            first=self.auth.admin_login('root_test','admin-password-123',otp_code=code,require_captcha=False,require_mfa=True)
            self.assertTrue(first['ok'])
            self.assertEqual(first['admin']['mfa_verified_at'],1000)
            self.assertTrue(self.auth.validate_admin_session(first['session_token'])['ok'])
            replay=self.auth.admin_login('root_test','admin-password-123',otp_code=code,require_captcha=False,require_mfa=True)
            self.assertFalse(replay['ok'])
        with patch('agent.auth._now',return_value=1030):
            fresh=self.auth.admin_login('root_test','admin-password-123',otp_code=totp_code(secret,1030),require_captcha=False,require_mfa=True)
            self.assertTrue(fresh['ok'])
        with sqlite3.connect(self.db) as conn:
            self.assertNotIn(secret,str(conn.execute('SELECT metadata_json FROM auth_audit').fetchall()))

    def test_public_admin_mfa_requirement_fails_closed_without_enrollment(self):
        self.auth.bootstrap_admin('root_test','admin-password-123')
        result=self.auth.admin_login('root_test','admin-password-123',require_mfa=True,require_captcha=False)
        self.assertFalse(result['ok']); self.assertEqual(result.get('code'),'mfa_setup_required')

    def test_totp_adjacent_steps_are_bounded_with_a_frozen_verification_clock(self):
        secret = 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'
        with patch('agent.auth._now', return_value=1000):
            for timestamp, step in ((970, 32), (1000, 33), (1030, 34)):
                code = totp_code(secret, timestamp)
                self.assertEqual(self.auth._match_totp(secret, code, None), step)
                self.assertIsNone(self.auth._match_totp(secret, code, step))
            for timestamp in (940, 1060):
                self.assertIsNone(self.auth._match_totp(secret, totp_code(secret, timestamp), None))

    def test_recent_admin_verification_refreshes_only_current_session_without_extending_expiry(self):
        admin=self.auth.bootstrap_admin('root_recent','admin-password-123')
        secret='GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'
        self.auth.configure_admin_totp(admin['admin_id'],secret)
        with patch('agent.auth._now',return_value=1000):
            login=self.auth.admin_login('root_recent','admin-password-123',require_captcha=False,require_mfa=True,otp_code=totp_code(secret,1000))
        with self.auth._conn() as conn:
            before=dict(conn.execute('SELECT * FROM admin_sessions WHERE session_id=?',(login['session_id'],)).fetchone())
        with patch('agent.auth._now',return_value=1030):
            result=self.auth.reauthenticate_admin(login['session_token'],totp_code(secret,1030))
            self.assertTrue(result['ok']); self.assertEqual(result['admin']['recent_authenticated_at'],1030)
            self.assertFalse(self.auth.reauthenticate_admin(login['session_token'],totp_code(secret,1030))['ok'])
        with self.auth._conn() as conn:
            after=dict(conn.execute('SELECT * FROM admin_sessions WHERE session_id=?',(login['session_id'],)).fetchone())
        self.assertEqual(before['expires_at'],after['expires_at'])
        self.assertEqual(before['created_at'],after['created_at'])
        self.assertEqual(after['recent_authenticated_at'],1030)
        self.assertFalse(self.auth.reauthenticate_admin('invalid-token','123456')['ok'])

    def test_password_only_session_cannot_refresh_recent_auth_with_an_empty_code(self):
        self.auth.bootstrap_admin('root_recent','admin-password-123')
        login=self.auth.admin_login('root_recent','admin-password-123',require_captcha=False)
        result=self.auth.reauthenticate_admin(login['session_token'],'')
        self.assertFalse(result['ok']); self.assertEqual(result['code'],'mfa_setup_required')

    def test_malformed_or_symlink_second_factor_store_fails_closed(self):
        self.auth.bootstrap_admin('root_test','admin-password-123')
        self.auth.totp_secret_path.write_text('{broken')
        with self.assertRaises(AuthError):
            self.auth.admin_login('root_test','admin-password-123',require_captcha=False)
        self.auth.totp_secret_path.unlink()
        other=self.db.parent/'other-secret'; other.write_text('{}')
        self.auth.totp_secret_path.symlink_to(other)
        with self.assertRaises(AuthError):
            self.auth.admin_login('root_test','admin-password-123',require_captcha=False)

    def test_strongly_verified_caller_can_skip_legacy_math_but_not_rates(self):
        self.assertFalse(self.auth.register('proof_user','password-123')['ok'])
        self.assertTrue(self.auth.register('proof_user','password-123',require_captcha=False)['ok'])
        self.assertTrue(self.auth.login('proof_user','password-123',require_captcha=False)['ok'])

    def test_registration_receipt_recovers_after_outer_http_receipt_was_lost(self):
        first=self.auth.register('receipt_user','private-password-123',operation_key='browser:operation-001',require_captcha=False)
        restarted=AuthService(self.db)
        # The business operation is already committed, so a used/absent legacy
        # captcha does not prevent retrieving its exact original result.
        replay=restarted.register('RECEIPT_USER','private-password-123',operation_key='browser:operation-001')
        self.assertEqual(replay,first)
        changed=restarted.register('receipt_user','changed-password-123',operation_key='browser:operation-001',require_captcha=False)
        self.assertFalse(changed['ok']); self.assertEqual(changed['code'],'idempotency_conflict')
        with self.auth._conn() as conn:
            receipts=conn.execute('SELECT * FROM auth_registration_receipts').fetchall()
            self.assertEqual(len(receipts),1)
            self.assertNotIn('private-password-123',str([dict(row) for row in receipts]))
            self.assertNotIn('browser:operation-001',str([dict(row) for row in receipts]))
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM users').fetchone()[0],1)

    def test_concurrent_registration_operation_creates_one_user_and_one_receipt(self):
        barrier=threading.Barrier(4); results=[]; errors=[]
        def work():
            try:
                service=AuthService(self.db)
                barrier.wait(timeout=5)
                results.append(service.register('concurrent_receipt','password-123',operation_key='browser:same-operation',require_captcha=False))
            except Exception as exc: errors.append(exc)
        threads=[threading.Thread(target=work) for _ in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=10)
        self.assertFalse(errors); self.assertEqual(len(results),4)
        self.assertTrue(all(result==results[0] and result['ok'] for result in results))
        with self.auth._conn() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM users').fetchone()[0],1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM auth_registration_receipts').fetchone()[0],1)

    def test_receipt_storage_failure_rolls_back_account_creation(self):
        with self.auth._conn() as conn:
            conn.execute("CREATE TRIGGER receipt_failure BEFORE INSERT ON auth_registration_receipts BEGIN SELECT RAISE(ABORT,'simulated receipt failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.auth.register('rollback_receipt','password-123',operation_key='browser:rollback-operation',require_captcha=False)
        with self.auth._conn() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM users').fetchone()[0],0)
            conn.execute('DROP TRIGGER receipt_failure')
        self.assertTrue(self.auth.register('rollback_receipt','password-123',operation_key='browser:rollback-operation',require_captcha=False)['ok'])

    def test_account_and_pending_eligibility_commit_together_and_callback_is_not_replayed(self):
        calls=[]
        with self.auth._conn() as conn:
            conn.execute('CREATE TABLE test_benefit_pending(user_id INTEGER PRIMARY KEY,pending INTEGER NOT NULL)')
        def pending(conn,user_id):
            self.assertTrue(conn.in_transaction)
            calls.append(user_id)
            conn.execute('INSERT INTO test_benefit_pending VALUES(?,1)',(user_id,))
        first=self.auth.register('pending_receipt','password-123',operation_key='browser:pending-operation',on_created=pending,require_captcha=False)
        replay=self.auth.register('pending_receipt','password-123',operation_key='browser:pending-operation',on_created=pending,require_captcha=False)
        self.assertEqual(first,replay); self.assertEqual(calls,[first['user_id']])
        with self.auth._conn() as conn:
            self.assertEqual(conn.execute('SELECT pending FROM test_benefit_pending WHERE user_id=?',(first['user_id'],)).fetchone()[0],1)

    def test_pending_eligibility_callback_failure_does_not_leave_a_login_capable_account(self):
        def fail(conn,user_id): raise RuntimeError('simulated qualification storage failure')
        with self.assertRaises(RuntimeError):
            self.auth.register('failed_pending','password-123',operation_key='browser:failed-pending',on_created=fail,require_captcha=False)
        with self.auth._conn() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM users').fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM auth_registration_receipts').fetchone()[0],0)


    def test_audited_admin_lock_and_unlock_replay_once_without_reviving_sessions(self):
        uid = self.auth.register('audit_lock_user', 'password-123', require_captcha=False)['user_id']
        token = self.auth.login('audit_lock_user', 'password-123', require_captcha=False)['session_token']
        admin = self.auth.bootstrap_admin('audit_admin', 'password-123')['admin_id']
        first = self.auth.admin_set_user_lock(uid, True, admin_id=admin,
            operation_id='operation-lock-001', reason='确认账户异常操作')
        self.assertTrue(first['ok'])
        self.assertEqual(first, self.auth.admin_set_user_lock(uid, True, admin_id=admin,
            operation_id='operation-lock-001', reason='确认账户异常操作'))
        self.assertFalse(self.auth.validate_session(token)['ok'])
        self.assertTrue(self.auth.admin_set_user_lock(uid, False, admin_id=admin,
            operation_id='operation-unlock-001', reason='人工核实后解除限制')['ok'])
        self.assertFalse(self.auth.validate_session(token)['ok'])
        self.assertTrue(self.auth.login('audit_lock_user', 'password-123', require_captcha=False)['ok'])
        with self.auth._conn() as conn:
            receipts = [dict(row) for row in conn.execute('SELECT * FROM auth_admin_operations')]
            self.assertEqual(len(receipts), 2)
            self.assertNotIn('operation-lock-001', str(receipts))
            audit = conn.execute("SELECT action,metadata_json FROM auth_audit WHERE action LIKE 'admin_user_%' ORDER BY audit_id").fetchall()
            self.assertEqual([row['action'] for row in audit], ['admin_user_lock', 'admin_user_unlock'])
            self.assertEqual(json.loads(audit[-1]['metadata_json'])['reason'], '人工核实后解除限制')

    def test_audited_lock_rejects_changed_parameters_or_disabled_admin(self):
        from agent.security import SecurityError
        uid = self.auth.register('audit_conflict_user', 'password-123', require_captcha=False)['user_id']
        admin = self.auth.bootstrap_admin('conflict_admin', 'password-123')['admin_id']
        args = {'admin_id': admin, 'operation_id': 'operation-conflict-001', 'reason': '确认账户异常操作'}
        self.auth.admin_set_user_lock(uid, True, **args)
        with self.assertRaises(SecurityError) as conflict:
            self.auth.admin_set_user_lock(uid, False, **args)
        self.assertEqual(conflict.exception.status, 409)
        for invalid in ({'admin_id': admin}, {**args, 'reason': ''},
                        {**args, 'reason': 'api_key=sk-private-test-key-123456789'}):
            with self.assertRaises(AuthError): self.auth.admin_set_user_lock(uid, False, **invalid)
        with self.auth._conn() as conn:
            conn.execute('UPDATE admins SET disabled=1 WHERE admin_id=?', (admin,))
        with self.assertRaises(SecurityError) as denied:
            self.auth.admin_set_user_lock(uid, False, admin_id=admin,
                operation_id='operation-disabled-admin', reason='尝试解除账户限制')
        self.assertEqual(denied.exception.status, 403)
        with self.auth._conn() as conn:
            self.assertEqual(conn.execute('SELECT disabled FROM users WHERE user_id=?', (uid,)).fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM auth_admin_operations').fetchone()[0], 1)

    def test_admin_lock_audit_failure_rolls_back_ban_and_session_revocation(self):
        uid = self.auth.register('audit_failure_user', 'password-123', require_captcha=False)['user_id']
        token = self.auth.login('audit_failure_user', 'password-123', require_captcha=False)['session_token']
        admin = self.auth.bootstrap_admin('failure_admin', 'password-123')['admin_id']
        with self.auth._conn() as conn:
            conn.execute("CREATE TRIGGER lock_audit_failure BEFORE INSERT ON auth_admin_operations BEGIN SELECT RAISE(ABORT,'simulated audit storage failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.auth.admin_set_user_lock(uid, True, admin_id=admin,
                operation_id='operation-rollback-lock', reason='模拟事务回滚测试')
        self.assertTrue(self.auth.validate_session(token)['ok'])
        with self.auth._conn() as conn:
            self.assertEqual(conn.execute('SELECT disabled FROM users WHERE user_id=?', (uid,)).fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM auth_audit WHERE action='admin_user_lock'").fetchone()[0], 0)

    def test_concurrent_admin_lock_receipts_do_not_duplicate_audits(self):
        uid = self.auth.register('audit_parallel_user', 'password-123', require_captcha=False)['user_id']
        admin = self.auth.bootstrap_admin('parallel_admin', 'password-123')['admin_id']
        barrier, results, errors = threading.Barrier(5), [], []
        def work():
            try:
                service = AuthService(self.db)
                barrier.wait(timeout=5)
                results.append(service.admin_set_user_lock(uid, True, admin_id=admin,
                    operation_id='operation-concurrent-lock', reason='并发审计一致性测试'))
            except Exception as exc: errors.append(exc)
        threads = [threading.Thread(target=work) for _ in range(5)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=10)
        self.assertFalse(errors)
        self.assertEqual(results, [{'ok': True}] * 5)
        with self.auth._conn() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM auth_admin_operations').fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM auth_audit WHERE action='admin_user_lock'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
