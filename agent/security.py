"""Persistent abuse controls and server-verified, operation-bound challenges.

This module holds no authority to award points. A passed proof only permits
the exact operation bound by the caller, whose own transaction remains
responsible for grant/charge idempotency. No network request can enable tests.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import sqlite3
import time
import uuid
from pathlib import Path
from urllib import request, parse


class SecurityError(ValueError):
    def __init__(self, message, status=403, code='verification_required'):
        super().__init__(message)
        self.status, self.code = status, code


def _ip(value):
    if '%' in str(value):
        raise SecurityError('网络来源无效', 400, 'invalid_source')
    try:
        addr = ipaddress.ip_address(str(value).strip())
    except ValueError as exc:
        raise SecurityError('网络来源无效', 400, 'invalid_source') from exc
    return str(addr.ipv4_mapped if getattr(addr, 'ipv4_mapped', None) else addr)


def client_ip(peer, headers=None, trusted_proxies=()):
    """Walk a forwarded chain from the trusted edge, never trusting its client.

    Untrusted peers' forwarding headers are ignored. Every supplied address
    must be a literal IP; malformed trusted chains fail closed.
    """
    address = _ip(peer)
    networks = []
    for item in trusted_proxies:
        try:
            networks.append(ipaddress.ip_network(str(item), strict=False))
        except ValueError as exc:
            raise SecurityError('可信代理配置无效', 503, 'invalid_proxy_config') from exc
    trusted = lambda value: any(ipaddress.ip_address(value) in net for net in networks)
    if not trusted(address):
        return address
    headers = headers or {}
    value = headers.get('X-Forwarded-For', headers.get('x-forwarded-for', ''))
    if not value:
        return address
    if not isinstance(value, str) or len(value) > 1024:
        raise SecurityError('网络来源无效', 400, 'invalid_source')
    chain = value.split(',')
    if len(chain) > 16:
        raise SecurityError('网络来源无效', 400, 'invalid_source')
    for item in reversed(chain):
        if not trusted(address):
            break
        address = _ip(item)
    return address


SCHEMA = '''
CREATE TABLE IF NOT EXISTS security_rate_events (
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, key_hash TEXT NOT NULL, occurred_at REAL NOT NULL, expires_at REAL);
CREATE INDEX IF NOT EXISTS idx_security_rate_key_time ON security_rate_events(key_hash,occurred_at);
CREATE INDEX IF NOT EXISTS idx_security_rate_time ON security_rate_events(occurred_at);
CREATE TABLE IF NOT EXISTS security_challenges (
 challenge_id TEXT PRIMARY KEY, actor_hash TEXT NOT NULL, action TEXT NOT NULL,
 operation_id TEXT NOT NULL, digest TEXT NOT NULL, source_hash TEXT NOT NULL,
 created_at REAL NOT NULL, expires_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 test_hash TEXT, provider_token_hash TEXT, verified_at REAL, proof_hash TEXT UNIQUE, used_at REAL);
CREATE INDEX IF NOT EXISTS idx_security_challenge_actor ON security_challenges(actor_hash,action,expires_at);
CREATE TABLE IF NOT EXISTS security_provider_tokens (
 token_hash TEXT PRIMARY KEY, challenge_id TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS security_verification_leases(
 lease_id TEXT PRIMARY KEY,expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS security_registrations (
 registration_id INTEGER PRIMARY KEY AUTOINCREMENT, browser_hash TEXT NOT NULL,
 source_hash TEXT NOT NULL, occurred_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_security_registration_browser ON security_registrations(browser_hash,occurred_at);
CREATE INDEX IF NOT EXISTS idx_security_registration_source ON security_registrations(source_hash,occurred_at);
CREATE TABLE IF NOT EXISTS security_registration_receipts(
 operation_hash TEXT PRIMARY KEY,browser_hash TEXT NOT NULL,source_hash TEXT NOT NULL,
 result_json TEXT NOT NULL,created_at REAL NOT NULL);
'''


class SecurityService:
    def __init__(self, db_path, secret, *, site_key='', turnstile_secret='', hostname='',
                 verifier=None, test_mode=False, clock=None):
        self.db_path = Path(db_path)
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError('security secret must contain at least 32 bytes')
        self.secret = secret
        self.site_key, self.turnstile_secret = site_key, turnstile_secret
        self.hostname = str(hostname).lower().rstrip('.')
        self.verifier, self.test_mode = verifier, test_mode is True
        self.clock = clock or time.time
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            conn.execute('BEGIN IMMEDIATE')
            if 'expires_at' not in {row[1] for row in conn.execute('PRAGMA table_info(security_rate_events)')}:
                conn.execute('ALTER TABLE security_rate_events ADD COLUMN expires_at REAL')

    def _conn(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _hash(self, value):
        return hmac.new(self.secret, str(value).encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _bounded(value, name, size=200):
        if not isinstance(value, str) or not value or len(value) > size:
            raise SecurityError(name + '无效', 400, 'invalid_request')
        return value

    def check_rate(self, scope, subject, limit, window=60):
        """Atomically consume one token in a persisted exact sliding window."""
        self._bounded(scope, '限流类别', 80); self._bounded(str(subject), '限流标识', 256)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
            raise ValueError('invalid rate limit')
        if isinstance(window, bool) or not isinstance(window, (int, float)) or not 1 <= window <= 86400:
            raise ValueError('invalid rate window')
        now, key = self.clock(), self._hash(scope + ':' + str(subject))
        with self._conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('DELETE FROM security_rate_events WHERE COALESCE(expires_at,occurred_at+86400)<=?', (now,))
            count = conn.execute('SELECT COUNT(*) FROM security_rate_events WHERE key_hash=? AND occurred_at>?', (key, now-window)).fetchone()[0]
            if count >= limit or conn.execute('SELECT COUNT(*) FROM security_rate_events').fetchone()[0] >= 25000:
                return False
            conn.execute('INSERT INTO security_rate_events(key_hash,occurred_at,expires_at) VALUES(?,?,?)', (key, now, now+window))
            return True

    def require_rate(self, scope, subject, limit, window=60):
        if not self.check_rate(scope, subject, limit, window):
            raise SecurityError('请求过于频繁，请稍后重试', 429, 'rate_limited')

    def _ready(self):
        if not self.test_mode and not (self.site_key and self.turnstile_secret and self.hostname):
            raise SecurityError('人机验证尚未配置，请稍后重试', 503, 'verification_unavailable')

    def challenge(self, actor, action, operation_id, digest, source=''):
        self._ready()
        actor = self._bounded(str(actor), '身份')
        self._bounded(action, '操作', 32)
        if not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{0,31}', action):
            raise SecurityError('操作类型无效', 400, 'invalid_request')
        self._bounded(operation_id, '操作ID', 128)
        if not re.fullmatch(r'[A-Za-z0-9_:-]{1,128}', operation_id) or not re.fullmatch(r'[a-f0-9]{64}', str(digest)):
            raise SecurityError('操作标识无效', 400, 'invalid_request')
        self.require_rate('challenge-actor', actor, 5)
        if source: self.require_rate('challenge-source', _ip(source), 30)
        self.require_rate('challenge-global', 'all', 600)
        now, cid = self.clock(), uuid.uuid4().hex
        test_token = secrets.token_urlsafe(32) if self.test_mode else None
        actor_hash = self._hash(actor)
        with self._conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('DELETE FROM security_challenges WHERE expires_at<? AND used_at IS NULL', (now-86400,))
            if conn.execute('SELECT COUNT(*) FROM security_challenges').fetchone()[0] >= 10000:
                raise SecurityError('验证服务繁忙，请稍后重试', 503, 'verification_unavailable')
            if conn.execute('SELECT COUNT(*) FROM security_challenges WHERE actor_hash=? AND action=? AND expires_at>? AND verified_at IS NULL AND attempts<3', (actor_hash, action, now)).fetchone()[0] >= 2:
                raise SecurityError('已有待完成的验证，请稍后重试', 429, 'rate_limited')
            conn.execute('INSERT INTO security_challenges(challenge_id,actor_hash,action,operation_id,digest,source_hash,created_at,expires_at,test_hash) VALUES(?,?,?,?,?,?,?,?,?)',
                         (cid, actor_hash, action, operation_id, digest, self._hash(source), now, now+120, self._hash(test_token) if test_token else None))
        out = {'challenge_id': cid, 'provider': 'test' if self.test_mode else 'turnstile', 'site_key': self.site_key,
               'action': action, 'operation_id': operation_id, 'expires_at': now+120}
        if test_token: out['test_token'] = test_token
        return out

    def _proof(self, cid):
        return hmac.new(self.secret, ('business-proof:' + cid).encode(), hashlib.sha256).hexdigest()

    def _verify_provider(self, token, challenge):
        if self.test_mode:
            return bool(challenge['test_hash'] and hmac.compare_digest(self._hash(token), challenge['test_hash']))
        if self.verifier is not None:
            result = self.verifier(token, challenge)
        else:
            body = parse.urlencode({'secret': self.turnstile_secret, 'response': token,
                                    'idempotency_key': str(uuid.UUID(hex=challenge['challenge_id']))}).encode()
            req = request.Request('https://challenges.cloudflare.com/turnstile/v0/siteverify', data=body, method='POST')
            with request.urlopen(req, timeout=8) as response:
                raw = response.read(16385)
            if len(raw) > 16384: return False
            result = json.loads(raw)
        return bool(isinstance(result, dict) and result.get('success') is True
                    and str(result.get('hostname','')).lower().rstrip('.') == self.hostname
                    and result.get('action') == challenge['action']
                    and result.get('cdata') == challenge['challenge_id'])

    def _verification_slot(self):
        """Bound external verifier fanout across processes, including crashes."""
        with self._conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('DELETE FROM security_verification_leases WHERE expires_at<=?',(self.clock(),))
            if conn.execute('SELECT COUNT(*) FROM security_verification_leases').fetchone()[0]>=8:
                raise SecurityError('验证服务繁忙，请稍后重试',503,'verification_unavailable')
            lease=uuid.uuid4().hex
            conn.execute('INSERT INTO security_verification_leases VALUES(?,?)',(lease,self.clock()+15))
            return lease

    def verify(self, challenge_id, token, actor, source=''):
        self._ready()
        self._bounded(challenge_id, '挑战ID', 64); self._bounded(token, '验证响应', 2048)
        actor = self._bounded(str(actor), '身份')
        self.require_rate('verify-actor', actor, 15)
        if source: self.require_rate('verify-source', _ip(source), 60)
        self.require_rate('verify-global', 'all', 300)
        now, ah = self.clock(), self._hash(actor)
        with self._conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM security_challenges WHERE challenge_id=? AND actor_hash=?', (challenge_id, ah)).fetchone()
            if not row or row['expires_at'] <= now:
                raise SecurityError('验证已过期或无效，请重新验证')
            if row['verified_at'] is not None:
                if not hmac.compare_digest(row['provider_token_hash'] or '', self._hash(token)):
                    raise SecurityError('验证响应无效')
                return {'proof': self._proof(challenge_id), 'expires_at': row['expires_at']}
            if row['attempts'] >= 3:
                raise SecurityError('验证已失效，请重新验证')
            conn.execute('UPDATE security_challenges SET attempts=attempts+1 WHERE challenge_id=?', (challenge_id,))
            challenge = dict(row)
        lease=None
        try:
            if not self.test_mode: lease=self._verification_slot()
            valid = self._verify_provider(token, challenge)
        except Exception:
            # Do not include remote response or credentials in user-visible errors.
            raise SecurityError('验证服务暂不可用，请稍后重试', 503, 'verification_unavailable') from None
        finally:
            if lease:
                with self._conn() as conn: conn.execute('DELETE FROM security_verification_leases WHERE lease_id=?',(lease,))
        if not valid:
            raise SecurityError('验证未通过，请重试')
        proof = self._proof(challenge_id)
        with self._conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM security_challenges WHERE challenge_id=? AND actor_hash=?', (challenge_id, ah)).fetchone()
            if not row or row['expires_at'] <= self.clock(): raise SecurityError('验证已过期')
            token_hash = self._hash(token)
            if row['verified_at'] is None:
                try:
                    conn.execute('INSERT INTO security_provider_tokens VALUES(?,?,?)', (token_hash, challenge_id, row['expires_at']))
                except sqlite3.IntegrityError:
                    raise SecurityError('验证响应已使用') from None
                conn.execute('UPDATE security_challenges SET verified_at=?,provider_token_hash=?,proof_hash=? WHERE challenge_id=?', (self.clock(), token_hash, self._hash(proof), challenge_id))
            elif row['provider_token_hash'] != token_hash:
                raise SecurityError('验证响应已使用')
        return {'proof': proof, 'expires_at': row['expires_at']}

    def consume(self, proof, actor, action, operation_id, digest):
        if not isinstance(proof, str) or not re.fullmatch('[a-f0-9]{64}', proof):
            raise SecurityError('请先完成人机验证')
        with self._conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM security_challenges WHERE proof_hash=?', (self._hash(proof),)).fetchone()
            valid = row and row['actor_hash'] == self._hash(str(actor)) and row['action'] == action and row['operation_id'] == operation_id and row['digest'] == digest
            if not valid or row['verified_at'] is None:
                raise SecurityError('验证与当前操作不匹配')
            if row['used_at'] is not None:
                return True  # Same immutable operation may query/retry its outcome.
            if row['expires_at'] <= self.clock(): raise SecurityError('验证已过期，请重新验证')
            conn.execute('UPDATE security_challenges SET used_at=? WHERE challenge_id=? AND used_at IS NULL', (self.clock(), row['challenge_id']))
            return True

    def risk_eligibility(self, browser, source=''):
        browser = self._bounded(str(browser), '浏览器资格', 256)
        now = self.clock()
        with self._conn() as conn:
            by_browser = conn.execute('SELECT COUNT(*) FROM security_registrations WHERE browser_hash=? AND occurred_at>?', (self._hash(browser), now-86400)).fetchone()[0]
            by_source = conn.execute('SELECT COUNT(*) FROM security_registrations WHERE source_hash=? AND occurred_at>?', (self._hash(_ip(source) if source else ''), now-3600)).fetchone()[0] if source else 0
        return {'eligible': by_browser < 3 and by_source < 10,
                'benefit_pending': by_browser >= 3 or by_source >= 10,
                'reason': 'additional_verification' if by_browser >= 3 or by_source >= 10 else None}

    def record_registration(self, browser, source='', operation_id=None):
        self._bounded(str(browser), '浏览器资格', 256)
        source = _ip(source) if source else ''
        operation_hash=None
        if operation_id is not None:
            self._bounded(operation_id,'注册操作标识',256)
            operation_hash=self._hash('registration-operation:'+operation_id)
        with self._conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if operation_hash:
                receipt=conn.execute('SELECT * FROM security_registration_receipts WHERE operation_hash=?',(operation_hash,)).fetchone()
                if receipt:
                    if not hmac.compare_digest(receipt['browser_hash'],self._hash(browser)):
                        raise SecurityError('注册操作标识与原身份不一致',409,'idempotency_conflict')
                    return json.loads(receipt['result_json'])
            conn.execute('DELETE FROM security_registrations WHERE occurred_at<?', (self.clock()-86400,))
            if conn.execute('SELECT COUNT(*) FROM security_registrations').fetchone()[0] >= 25000:
                raise SecurityError('注册服务繁忙，请稍后重试', 429, 'rate_limited')
            browser_count = conn.execute('SELECT COUNT(*) FROM security_registrations WHERE browser_hash=? AND occurred_at>?', (self._hash(browser), self.clock()-86400)).fetchone()[0]
            source_count = conn.execute('SELECT COUNT(*) FROM security_registrations WHERE source_hash=? AND occurred_at>?', (self._hash(source), self.clock()-3600)).fetchone()[0] if source else 0
            conn.execute('INSERT INTO security_registrations(browser_hash,source_hash,occurred_at) VALUES(?,?,?)', (self._hash(browser), self._hash(source), self.clock()))
            pending = browser_count >= 3 or source_count >= 10
            result={'eligible': not pending, 'benefit_pending': pending,
                    'reason': 'additional_verification' if pending else None}
            if operation_hash:
                conn.execute('INSERT INTO security_registration_receipts VALUES(?,?,?,?,?)',(operation_hash,self._hash(browser),self._hash(source),json.dumps(result,sort_keys=True,separators=(',',':')),self.clock()))
            return result

    def cleanup(self):
        now = self.clock()
        with self._conn() as conn:
            conn.execute('DELETE FROM security_rate_events WHERE COALESCE(expires_at,occurred_at+86400)<=?', (now,))
            conn.execute('DELETE FROM security_challenges WHERE expires_at<?', (now-7*86400,))
            conn.execute('DELETE FROM security_provider_tokens WHERE expires_at<?', (now-86400,))
            conn.execute('DELETE FROM security_registrations WHERE occurred_at<?', (now-86400,))
            conn.execute('DELETE FROM security_verification_leases WHERE expires_at<=?',(now,))
