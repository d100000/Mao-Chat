#!/usr/bin/env python3
"""Local authentication primitives for the evidence-first Agent.

This module intentionally has no web framework dependency.  A local HTTP/UI
layer can call AuthService and expose only its JSON-safe return values.
Passwords, captcha answers and session token fingerprints are never stored
in plaintext.  Captcha challenges are deliberately short-lived and one-use.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .db import connect, default_db_path, ensure_schema


AUTH_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS users (
  user_id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL,
  username_norm TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  password_salt TEXT NOT NULL,
  password_iterations INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  failed_logins INTEGER NOT NULL DEFAULT 0,
  locked_until REAL
);
CREATE TABLE IF NOT EXISTS captcha_challenges (
  challenge_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('math','slider')),
  answer_hash TEXT,
  token_hash TEXT,
  prompt TEXT NOT NULL,
  visual_payload TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  used_at REAL
);
CREATE TABLE IF NOT EXISTS auth_sessions (
  session_id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL UNIQUE,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  revoked_at REAL
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id);
CREATE TABLE IF NOT EXISTS auth_audit (
  audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
  occurred_at REAL NOT NULL,
  action TEXT NOT NULL,
  user_id INTEGER,
  username_norm TEXT,
  success INTEGER NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_auth_audit_user ON auth_audit(username_norm, occurred_at);
CREATE TABLE IF NOT EXISTS auth_rate_limits (
  rate_key_hash TEXT PRIMARY KEY,
  window_start REAL NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS admins (
  admin_id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL,
  username_norm TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  password_salt TEXT NOT NULL,
  password_iterations INTEGER NOT NULL,
  role TEXT NOT NULL DEFAULT 'admin',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  failed_logins INTEGER NOT NULL DEFAULT 0,
  locked_until REAL,
  disabled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS admin_sessions (
  session_id TEXT PRIMARY KEY,
  admin_id INTEGER NOT NULL REFERENCES admins(admin_id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL UNIQUE,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  revoked_at REAL
);
CREATE INDEX IF NOT EXISTS idx_admin_sessions_admin ON admin_sessions(admin_id);
CREATE TABLE IF NOT EXISTS app_settings (
  setting_key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  is_secret INTEGER NOT NULL DEFAULT 0,
  updated_by INTEGER,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


USERNAME_RE = re.compile(r"^[\w\-]{3,32}$", re.UNICODE)
PASSWORD_ITERATIONS = 310_000


def _now() -> float:
    return time.time()


def _utc(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else _now(), timezone.utc).isoformat()


def _hash_bytes(value: str, pepper: bytes = b"") -> str:
    # HMAC avoids treating the application secret as a raw hash prefix and is
    # suitable for challenge, rate-limit and session token fingerprints.
    return hmac.new(pepper, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _pbkdf2(password: str, salt: bytes, iterations: int) -> str:
    value = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return base64.urlsafe_b64encode(value).decode("ascii")


def _password_record(password: str, iterations: int = PASSWORD_ITERATIONS) -> Dict[str, Any]:
    salt = secrets.token_bytes(16)
    return {
        "password_hash": _pbkdf2(password, salt, iterations),
        "password_salt": base64.urlsafe_b64encode(salt).decode("ascii"),
        "password_iterations": iterations,
    }


def _password_matches(password: str, row: Optional[sqlite3.Row]) -> bool:
    # Run the same expensive KDF for an unknown username so login timing does
    # not reveal whether an account exists.
    if row is None:
        _pbkdf2(password, b"\x00" * 16, PASSWORD_ITERATIONS)
        return False
    try:
        salt = base64.urlsafe_b64decode(row["password_salt"].encode("ascii"))
        actual = _pbkdf2(password, salt, int(row["password_iterations"]))
        return hmac.compare_digest(actual, row["password_hash"])
    except (ValueError, KeyError, TypeError, binascii.Error):
        return False


def _svg_text(text: str) -> str:
    """Return a dependency-free image payload suitable for <img src=...>."""
    escaped = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))
    # Base64 keeps punctuation, Unicode operators and spaces safe in a data
    # URI while avoiding a dependency on an image or URL-encoding package.
    svg = ("<svg xmlns='http://www.w3.org/2000/svg' width='260' height='80'>"
           "<rect width='100%' height='100%' fill='#f4f4f5'/>"
           "<text x='18' y='50' font-size='28' font-family='sans-serif' "
           "fill='#111827'>" + escaped + "</text></svg>")
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return "data:image/svg+xml;base64," + encoded


class AuthError(ValueError):
    pass


class AuthService:
    def __init__(self, db_path: Optional[Path] = None, *, captcha_ttl: int = 300,
                 session_ttl: int = 86_400,
                 max_captcha_attempts: int = 5, max_login_attempts: int = 8,
                 login_lock_seconds: int = 300, rate_window: int = 300,
                 audit_retention_seconds: int = 30 * 86_400):
        self.db_path = Path(db_path) if db_path else default_db_path()
        self.captcha_ttl = int(captcha_ttl)
        self.session_ttl = int(session_ttl)
        self.max_captcha_attempts = int(max_captcha_attempts)
        self.max_login_attempts = int(max_login_attempts)
        self.login_lock_seconds = int(login_lock_seconds)
        self.rate_window = int(rate_window)
        self.audit_retention_seconds = max(86_400, int(audit_retention_seconds))
        # A stable local secret keeps sessions valid across a normal process
        # restart.  Operators may provide one through the environment; for a
        # local database we otherwise create a 0600 sidecar secret file.
        configured = os.environ.get("MAO_AUTH_SECRET") or os.environ.get("MAO_AUTH_CAPTCHA_PEPPER")
        if configured:
            raw_secret = configured.encode("utf-8")
        elif self.db_path:
            secret_path = self.db_path.with_name(self.db_path.name + ".auth-secret")
            try:
                raw_secret = secret_path.read_bytes()
            except FileNotFoundError:
                raw_secret = secrets.token_bytes(32)
                secret_path.parent.mkdir(parents=True, exist_ok=True)
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                try:
                    fd = os.open(secret_path, flags, 0o600)
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(raw_secret)
                except FileExistsError:
                    raw_secret = secret_path.read_bytes()
            try:
                os.chmod(secret_path, 0o600)
            except OSError:
                pass
        else:
            raw_secret = secrets.token_bytes(32)
        self._captcha_pepper = raw_secret if len(raw_secret) >= 32 else hashlib.sha256(raw_secret).digest()
        # The database contains password and session verifiers.  Keep it
        # private to the local account when the platform permits chmod.
        for protected in (self.db_path, Path(str(self.db_path) + "-wal"), Path(str(self.db_path) + "-shm")):
            try:
                if protected.exists():
                    os.chmod(protected, 0o600)
            except OSError:
                pass
        conn = self._conn()
        ensure_schema(conn)
        conn.executescript(AUTH_SCHEMA)
        conn.commit()
        conn.close()
        for protected in (self.db_path, Path(str(self.db_path) + "-wal"), Path(str(self.db_path) + "-shm")):
            try:
                if protected.exists():
                    os.chmod(protected, 0o600)
            except OSError:
                pass
        bootstrap_user = os.environ.get("MAO_ADMIN_USERNAME")
        bootstrap_password = os.environ.get("MAO_ADMIN_PASSWORD")
        if bootstrap_user and bootstrap_password:
            try:
                self.bootstrap_admin(bootstrap_user, bootstrap_password)
            except AuthError:
                pass

    def _conn(self) -> sqlite3.Connection:
        return connect(self.db_path)

    def bootstrap_admin(self, username: str, password: str) -> Dict[str, Any]:
        display, norm = self._username(username)
        password = self._password(password)
        conn = self._conn()
        try:
            with conn:
                if conn.execute("SELECT 1 FROM admins WHERE username_norm=?", (norm,)).fetchone():
                    return {"ok": False, "error": "管理员已存在"}
                record = _password_record(password)
                cur = conn.execute("INSERT INTO admins(username,username_norm,password_hash,password_salt,password_iterations) VALUES(?,?,?,?,?)", (display, norm, record["password_hash"], record["password_salt"], record["password_iterations"]))
                self._audit(conn, "admin_bootstrap", norm, True, int(cur.lastrowid))
                return {"ok": True, "admin_id": int(cur.lastrowid), "username": display}
        finally:
            conn.close()

    def admin_login(self, username: str, password: str, *, lock_attempts: int = 8, **captcha) -> Dict[str, Any]:
        display, norm = self._username(username)
        password = self._password(password)
        conn = self._conn()
        try:
            with conn:
                if not self._consume_captcha(conn, captcha):
                    self._audit(conn, "admin_login", norm, False, reason="captcha")
                    return {"ok": False, "error": "管理员账号、密码或验证码错误"}
                if not self._rate_ok(conn, "admin:" + norm, lock_attempts):
                    self._audit(conn, "admin_login", norm, False, reason="rate")
                    return {"ok": False, "error": "管理员账号或密码错误"}
                row = conn.execute("SELECT * FROM admins WHERE username_norm=?", (norm,)).fetchone()
                password_ok = _password_matches(password, row)
                locked = bool(row and (row["disabled"] or (row["locked_until"] and float(row["locked_until"]) > _now())))
                if not row or locked or not password_ok:
                    if row and not locked:
                        failed = int(row["failed_logins"] or 0) + 1
                        lock_until = _now() + 300 if failed >= lock_attempts else None
                        conn.execute("UPDATE admins SET failed_logins=?,locked_until=?,updated_at=CURRENT_TIMESTAMP WHERE admin_id=?", (failed, lock_until, row["admin_id"]))
                    self._audit(conn, "admin_login", norm, False, reason="credentials")
                    return {"ok": False, "error": "管理员账号或密码错误"}
                conn.execute("UPDATE admins SET failed_logins=0,locked_until=NULL,updated_at=CURRENT_TIMESTAMP WHERE admin_id=?", (row["admin_id"],))
                token = secrets.token_urlsafe(32); sid = secrets.token_urlsafe(16); now = _now(); expires = now + self.session_ttl
                conn.execute("INSERT INTO admin_sessions(session_id,admin_id,token_hash,created_at,expires_at) VALUES(?,?,?,?,?)", (sid, row["admin_id"], _hash_bytes(token, self._captcha_pepper), now, expires))
                self._audit(conn, "admin_login", norm, True, row["admin_id"])
                return {"ok": True, "session_id": sid, "session_token": token, "expires_at": _utc(expires), "admin": {"admin_id": row["admin_id"], "username": row["username"], "role": row["role"]}}
        finally:
            conn.close()

    def validate_admin_session(self, token: str) -> Dict[str, Any]:
        if not isinstance(token, str) or not token or len(token) > 256:
            return {"ok": False, "error": "管理员会话无效"}
        conn = self._conn()
        try:
            row = conn.execute("SELECT s.*,a.username,a.role FROM admin_sessions s JOIN admins a ON a.admin_id=s.admin_id WHERE s.token_hash=? AND s.revoked_at IS NULL AND s.expires_at>? AND a.disabled=0", (_hash_bytes(token, self._captcha_pepper), _now())).fetchone()
            return {"ok": bool(row), "admin": {"admin_id": row["admin_id"], "username": row["username"], "role": row["role"]} if row else None, "error": None if row else "管理员会话无效"}
        finally:
            conn.close()

    def admin_logout(self, token: str) -> Dict[str, Any]:
        if not isinstance(token, str) or not token or len(token) > 256:
            return {"ok": False}
        conn = self._conn()
        try:
            with conn:
                changed = conn.execute("UPDATE admin_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL", (_now(), _hash_bytes(token, self._captcha_pepper))).rowcount
                self._audit(conn, "admin_logout", None, bool(changed))
            return {"ok": bool(changed)}
        finally:
            conn.close()

    def admin_list_users(self) -> Dict[str, Any]:
        conn = self._conn()
        try:
            users = [dict(row) for row in conn.execute("SELECT user_id,username,created_at,updated_at,failed_logins,locked_until FROM users ORDER BY user_id DESC").fetchall()]
            for item in users: item["locked"] = bool(item.get("locked_until") and float(item["locked_until"]) > _now())
            admins = [dict(row) for row in conn.execute("SELECT admin_id,username,role,created_at,updated_at,failed_logins,locked_until,disabled FROM admins ORDER BY admin_id DESC").fetchall()]
            for item in admins: item["locked"] = bool(item.get("disabled") or (item.get("locked_until") and float(item["locked_until"]) > _now()))
            return {"users": users, "admins": admins}
        finally:
            conn.close()

    def admin_set_user_lock(self, user_id: int, locked: bool) -> Dict[str, Any]:
        conn = self._conn()
        try:
            with conn:
                now = _now()
                if locked:
                    changed = conn.execute("UPDATE users SET locked_until=?,updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (now + self.login_lock_seconds, int(user_id))).rowcount
                    if changed:
                        # Locking an account must take effect for already-issued
                        # cookies as well as for new logins.
                        conn.execute("UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (now, int(user_id)))
                else:
                    changed = conn.execute("UPDATE users SET locked_until=NULL,failed_logins=0,updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (int(user_id),)).rowcount
            return {"ok": bool(changed)}
        finally:
            conn.close()

    @staticmethod
    def _username(value: str) -> tuple[str, str]:
        if not isinstance(value, str):
            raise AuthError("用户名格式无效")
        value = value.strip()
        if not USERNAME_RE.fullmatch(value):
            raise AuthError("用户名需为3至32位字母、数字、下划线或短横线")
        return value, value.casefold()

    @staticmethod
    def _password(value: str) -> str:
        if not isinstance(value, str) or not 8 <= len(value) <= 256:
            raise AuthError("密码长度需为8至256位")
        return value

    def _audit(self, conn, action: str, username_norm: Optional[str], success: bool,
               user_id: Optional[int] = None, **metadata):
        safe = {str(k): str(v)[:200] for k, v in metadata.items() if k not in {"password", "answer", "token", "nonce"}}
        conn.execute("INSERT INTO auth_audit(occurred_at,action,user_id,username_norm,success,metadata_json) VALUES(?,?,?,?,?,?)",
                     (_now(), action, user_id, username_norm, 1 if success else 0, json.dumps(safe, ensure_ascii=False, sort_keys=True)))

    def _rate_ok(self, conn, key: str, limit: int) -> bool:
        now = _now(); digest = _hash_bytes(key, self._captcha_pepper)
        row = conn.execute("SELECT window_start,attempts FROM auth_rate_limits WHERE rate_key_hash=?", (digest,)).fetchone()
        if not row or now - float(row["window_start"]) >= self.rate_window:
            conn.execute("INSERT INTO auth_rate_limits(rate_key_hash,window_start,attempts) VALUES(?,?,1) ON CONFLICT(rate_key_hash) DO UPDATE SET window_start=excluded.window_start,attempts=1", (digest, now))
            return True
        if int(row["attempts"]) >= limit:
            return False
        conn.execute("UPDATE auth_rate_limits SET attempts=attempts+1 WHERE rate_key_hash=?", (digest,))
        return True

    def _challenge(self, kind: str, prompt: str, *, answer: Optional[str] = None,
                   token: Optional[str] = None, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        challenge_id = secrets.token_urlsafe(18)
        now = _now(); expires = now + self.captcha_ttl
        conn = self._conn()
        try:
            with conn:
                conn.execute("INSERT INTO captcha_challenges(challenge_id,kind,answer_hash,token_hash,prompt,visual_payload,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?)",
                             (challenge_id, kind, _hash_bytes(challenge_id + answer, self._captcha_pepper) if answer is not None else None,
                              _hash_bytes(challenge_id + token, self._captcha_pepper) if token is not None else None,
                              prompt, json.dumps(payload or {}, ensure_ascii=False, sort_keys=True), now, expires))
        finally:
            conn.close()
        return {"challenge_id": challenge_id, "kind": kind, "prompt": prompt,
                "image": _svg_text(prompt), "expires_at": _utc(expires)}

    def create_math_challenge(self) -> Dict[str, Any]:
        a, b = secrets.randbelow(19) + 2, secrets.randbelow(19) + 2
        op = secrets.choice(("+", "-", "×"))
        if op == "+": answer = a + b
        elif op == "-":
            if b > a: a, b = b, a
            answer = a - b
        else: answer = a * b
        expression = "%d %s %d = ?" % (a, op, b)
        # Keep the expression out of the database.  It is rendered into the
        # image returned to the user, while only a verifier hash is persisted.
        result = self._challenge("math", "算术验证码", answer=str(answer), payload={"operation": op})
        result["image"] = _svg_text(expression)
        # The arithmetic expression is visible only inside the image.  Never
        # return it as a plaintext JSON field that would bypass the captcha.
        result["prompt"] = "请输入图片中的算式答案"
        return result

    def _verify_challenge(self, conn, challenge_id: str, kind: str, *, answer=None) -> bool:
        if not isinstance(challenge_id, str) or not challenge_id or len(challenge_id) > 256:
            return False
        row = conn.execute("SELECT * FROM captcha_challenges WHERE challenge_id=? AND kind=?", (challenge_id, kind)).fetchone()
        now = _now()
        if not row or row["used_at"] is not None or float(row["expires_at"]) < now or int(row["attempts"]) >= self.max_captcha_attempts:
            return False
        valid = False
        if kind == "math" and answer is not None:
            valid = hmac.compare_digest(row["answer_hash"] or "", _hash_bytes(challenge_id + str(answer).strip(), self._captcha_pepper))
        # The compare above is only preparation.  The conditional UPDATE is
        # the one-use gate: concurrent requests cannot both consume a valid
        # challenge after the first one sets used_at.
        if valid:
            changed = conn.execute(
                "UPDATE captcha_challenges SET attempts=attempts+1,used_at=? "
                "WHERE challenge_id=? AND kind=? AND used_at IS NULL "
                "AND expires_at>? AND attempts<?",
                (now, challenge_id, kind, now, self.max_captcha_attempts),
            ).rowcount
            return changed == 1
        conn.execute(
            "UPDATE captcha_challenges SET attempts=attempts+1 "
            "WHERE challenge_id=? AND kind=? AND used_at IS NULL "
            "AND expires_at>? AND attempts<?",
            (challenge_id, kind, now, self.max_captcha_attempts),
        )
        return False

    def verify_math(self, challenge_id: str, answer: str) -> Dict[str, Any]:
        conn = self._conn()
        with conn:
            ok = self._verify_challenge(conn, challenge_id, "math", answer=answer)
            self._audit(conn, "captcha_math", None, ok)
        return {"ok": ok, "error": None if ok else "验证码错误或已过期"}

    def _consume_captcha(self, conn, data: Dict[str, Any]) -> bool:
        return self._verify_challenge(conn, data.get("math_challenge_id", ""), "math", answer=data.get("math_answer"))

    def register(self, username: str, password: str, *, rate_key: Optional[str] = None, **captcha) -> Dict[str, Any]:
        display, norm = self._username(username); password = self._password(password)
        conn = self._conn()
        try:
            with conn:
                if not self._consume_captcha(conn, captcha):
                    self._audit(conn, "register", norm, False, reason="captcha_or_rate")
                    return {"ok": False, "error": "验证码错误、过期或请求过于频繁"}
                if not self._rate_ok(conn, "register:" + norm, 10):
                    self._audit(conn, "register", norm, False, reason="rate")
                    return {"ok": False, "error": "验证码错误、过期或请求过于频繁"}
                if rate_key is not None and not self._rate_ok(conn, "register-ip:" + str(rate_key)[:128], 20):
                    self._audit(conn, "register", norm, False, reason="ip_rate")
                    return {"ok": False, "error": "验证码错误、过期或请求过于频繁"}
                if conn.execute("SELECT 1 FROM users WHERE username_norm=?", (norm,)).fetchone():
                    self._audit(conn, "register", norm, False, reason="duplicate")
                    return {"ok": False, "error": "用户名已存在"}
                record = _password_record(password)
                try:
                    cur = conn.execute("INSERT INTO users(username,username_norm,password_hash,password_salt,password_iterations) VALUES(?,?,?,?,?)", (display, norm, record["password_hash"], record["password_salt"], record["password_iterations"]))
                except sqlite3.IntegrityError:
                    # A concurrent registration may win between the lookup
                    # above and this insert; expose the same safe error.
                    self._audit(conn, "register", norm, False, reason="duplicate")
                    return {"ok": False, "error": "用户名已存在"}
                self._audit(conn, "register", norm, True, int(cur.lastrowid))
                return {"ok": True, "user_id": int(cur.lastrowid), "username": display}
        finally:
            conn.close()

    def login(self, username: str, password: str, *, rate_key: Optional[str] = None, **captcha) -> Dict[str, Any]:
        display, norm = self._username(username); password = self._password(password)
        conn = self._conn()
        try:
            with conn:
                if not self._consume_captcha(conn, captcha):
                    self._audit(conn, "login", norm, False, reason="captcha_or_rate")
                    return {"ok": False, "error": "用户名、密码或验证码错误"}
                if not self._rate_ok(conn, "login:" + norm, self.max_login_attempts):
                    self._audit(conn, "login", norm, False, reason="rate")
                    return {"ok": False, "error": "用户名、密码或验证码错误"}
                if rate_key is not None and not self._rate_ok(conn, "login-ip:" + str(rate_key)[:128], 30):
                    self._audit(conn, "login", norm, False, reason="ip_rate")
                    return {"ok": False, "error": "用户名、密码或验证码错误"}
                row = conn.execute("SELECT * FROM users WHERE username_norm=?", (norm,)).fetchone()
                password_ok = _password_matches(password, row)
                locked = bool(row and row["locked_until"] and float(row["locked_until"]) > _now())
                if not row or locked or not password_ok:
                    # Do not extend a lockout merely because a client keeps
                    # probing an already locked account.
                    if row and not locked:
                        failed = int(row["failed_logins"] or 0) + 1
                        locked = _now() + self.login_lock_seconds if failed >= self.max_login_attempts else None
                        conn.execute("UPDATE users SET failed_logins=?,locked_until=?,updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (failed, locked, row["user_id"]))
                    self._audit(conn, "login", norm, False, reason="credentials")
                    return {"ok": False, "error": "用户名、密码或验证码错误"}
                conn.execute("UPDATE users SET failed_logins=0,locked_until=NULL,updated_at=CURRENT_TIMESTAMP WHERE user_id=?", (row["user_id"],))
                token = secrets.token_urlsafe(32); sid = secrets.token_urlsafe(16); now = _now(); expires = now + self.session_ttl
                conn.execute("INSERT INTO auth_sessions(session_id,user_id,token_hash,created_at,expires_at) VALUES(?,?,?,?,?)", (sid, row["user_id"], _hash_bytes(token, self._captcha_pepper), now, expires))
                self._audit(conn, "login", norm, True, row["user_id"])
                return {"ok": True, "session_id": sid, "session_token": token, "expires_at": _utc(expires), "user": {"user_id": row["user_id"], "username": row["username"]}}
        finally:
            conn.close()

    def validate_session(self, token: str) -> Dict[str, Any]:
        if not isinstance(token, str) or not token or len(token) > 256:
            return {"ok": False, "error": "会话无效"}
        conn = self._conn()
        try:
            now = _now()
            row = conn.execute("SELECT s.*,u.username FROM auth_sessions s JOIN users u ON u.user_id=s.user_id WHERE s.token_hash=? AND s.revoked_at IS NULL AND s.expires_at>? AND (u.locked_until IS NULL OR u.locked_until<=?)", (_hash_bytes(token, self._captcha_pepper), now, now)).fetchone()
            return {"ok": bool(row), "user": {"user_id": row["user_id"], "username": row["username"]} if row else None, "error": None if row else "会话无效"}
        finally:
            conn.close()

    def logout(self, token: str) -> Dict[str, Any]:
        if not isinstance(token, str) or not token or len(token) > 256:
            return {"ok": False}
        conn = self._conn()
        try:
            with conn:
                changed = conn.execute("UPDATE auth_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL", (_now(), _hash_bytes(token, self._captcha_pepper))).rowcount
                self._audit(conn, "logout", None, bool(changed))
            return {"ok": bool(changed)}
        finally:
            conn.close()

    def cleanup(self) -> Dict[str, int]:
        now = _now(); conn = self._conn()
        with conn:
            sessions = conn.execute("DELETE FROM auth_sessions WHERE expires_at<? OR revoked_at IS NOT NULL", (now,)).rowcount
            challenges = conn.execute("DELETE FROM captcha_challenges WHERE expires_at<? OR used_at IS NOT NULL", (now,)).rowcount
            rates = conn.execute("DELETE FROM auth_rate_limits WHERE window_start<?", (now - self.rate_window * 2,)).rowcount
            audits = conn.execute("DELETE FROM auth_audit WHERE occurred_at<?", (now - self.audit_retention_seconds,)).rowcount
        conn.close(); return {"sessions": sessions, "challenges": challenges, "rates": rates, "audits": audits}
