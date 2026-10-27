import base64
import sqlite3
import json
import tempfile
import unittest
import re
import threading
from pathlib import Path

from agent.auth import AuthService


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
        self.assertFalse(limited.verify_math(math["challenge_id"], "0")["ok"])
        self.assertFalse(limited.verify_math(math["challenge_id"], "0")["ok"])
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


if __name__ == "__main__":
    unittest.main()
