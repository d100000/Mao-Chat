"""Small, private abuse-control appeal queue with no grant/unlock authority.

HTTP callers authenticate actors, require appropriate human verification and
recent administrator authentication. This store binds claims and decisions
atomically but never changes wallets, bans, challenges or platform budgets.
Only the optional appeal note is stored, never an HTTP body or transcript.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .security import SecurityError

FIXED_CODES = {'benefit_pending', 'account_restricted', 'rate_limited', 'verification_required'}
_PRINCIPAL = re.compile(r'(?:u:[1-9][0-9]{0,17}|g:[a-f0-9]{32})\Z')
_OPERATION = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}\Z')
_CREDENTIAL = re.compile(r'(?i)(?:\b(?:sk|fc)-[a-z0-9_-]{12,}|\bbearer\s+\S+|(?:api[_ -]?key|password|密码|secret|token)\s*[:=：]\s*\S+)')

SCHEMA = '''
CREATE TABLE IF NOT EXISTS security_appeals(
 appeal_id TEXT PRIMARY KEY,principal_hash TEXT NOT NULL,risk_event_id TEXT,
 code TEXT NOT NULL,note TEXT NOT NULL,status TEXT NOT NULL,
 created_at REAL NOT NULL,expires_at REAL NOT NULL,
 reviewed_at REAL,reviewed_by INTEGER,review_reason TEXT);
CREATE INDEX IF NOT EXISTS idx_security_appeals_owner
 ON security_appeals(principal_hash,created_at);
CREATE INDEX IF NOT EXISTS idx_security_appeals_status
 ON security_appeals(status,created_at);
CREATE INDEX IF NOT EXISTS idx_security_appeals_expiry ON security_appeals(expires_at);
CREATE TABLE IF NOT EXISTS security_appeal_operations(
 operation_hash TEXT PRIMARY KEY,params_hmac TEXT NOT NULL,appeal_id TEXT NOT NULL,
 action TEXT NOT NULL,actor_hash TEXT NOT NULL,result_json TEXT NOT NULL,
 created_at REAL NOT NULL,expires_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_security_appeal_operation_expiry
 ON security_appeal_operations(expires_at);
CREATE TABLE IF NOT EXISTS security_appeal_audit(
 audit_id INTEGER PRIMARY KEY AUTOINCREMENT,appeal_id TEXT NOT NULL,
 action TEXT NOT NULL,actor_hash TEXT NOT NULL,metadata_json TEXT NOT NULL,
 created_at REAL NOT NULL,expires_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_security_appeal_audit_expiry ON security_appeal_audit(expires_at);
'''


class SecurityAppealsStore:
    def __init__(self, db_path, secret, *, retention_days=180, max_appeals=10000, clock=None):
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError('appeal secret must contain at least 32 bytes')
        if type(retention_days) is not int or not 1 <= retention_days <= 180:
            raise ValueError('appeal retention must be between 1 and 180 days')
        if type(max_appeals) is not int or not 1 <= max_appeals <= 100000:
            raise ValueError('invalid appeal capacity')
        self.db_path, self.secret = Path(db_path), secret
        self.retention_seconds, self.max_appeals = retention_days * 86400, max_appeals
        self.clock = clock or time.time
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            descriptor = os.open(str(self.db_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    def _conn(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _hash(self, purpose, value):
        return hmac.new(self.secret, (purpose + ':' + value).encode(), hashlib.sha256).hexdigest()

    def _principal_hash(self, value):
        if not isinstance(value, str) or not _PRINCIPAL.fullmatch(value):
            raise SecurityError('申诉身份无效', 401, 'identity_invalid')
        # Share the risk audit's purpose to verify ownership without raw IDs.
        return self._hash('principal', value)

    @staticmethod
    def _operation(value):
        if not isinstance(value, str) or not _OPERATION.fullmatch(value):
            raise SecurityError('申诉操作标识无效', 400, 'operation_required')
        return value

    @staticmethod
    def _text(value, *, required=False, maximum=500):
        if not isinstance(value, str) or len(value) > maximum:
            raise SecurityError('说明长度或格式无效', 400, 'invalid_request')
        value = value.strip()
        if (required and len(value) < 3) or any(ord(c) < 32 and c not in '\n\t' or ord(c) == 127 for c in value):
            raise SecurityError('说明长度或格式无效', 400, 'invalid_request')
        if _CREDENTIAL.search(value):
            raise SecurityError('说明不能包含密码或访问凭据', 400, 'invalid_request')
        return value

    @staticmethod
    def _admin(conn, admin_id):
        if type(admin_id) is not int or not 1 <= admin_id < 10**18:
            raise SecurityError('需要管理员身份', 403, 'admin_required')
        try:
            row = conn.execute('SELECT 1 FROM admins WHERE admin_id=? AND disabled=0', (admin_id,)).fetchone()
        except sqlite3.OperationalError:
            row = None
        if not row:
            raise SecurityError('需要有效管理员身份', 403, 'admin_required')

    def _now(self):
        value = self.clock()
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 253402300799:
            raise ValueError('invalid appeal clock')
        return float(value)

    @staticmethod
    def _public(row, admin=False):
        result = {key: row[key] for key in ('appeal_id', 'risk_event_id', 'code', 'note',
            'status', 'created_at', 'expires_at', 'reviewed_at', 'review_reason')}
        if admin:
            result.update(principal_hash=row['principal_hash'], reviewed_by=row['reviewed_by'])
        return result

    def _cleanup(self, conn, now):
        conn.execute('DELETE FROM security_appeal_operations WHERE expires_at<=?', (now,))
        conn.execute('DELETE FROM security_appeal_audit WHERE expires_at<=?', (now,))
        return conn.execute('DELETE FROM security_appeals WHERE expires_at<=?', (now,)).rowcount

    def _receipt(self, conn, action, actor_hash, operation_id, parameters):
        operation_hash = self._hash('appeal-operation', actor_hash + ':' + self._operation(operation_id))
        encoded = json.dumps([action, parameters], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        digest = self._hash('appeal-parameters', encoded)
        old = conn.execute('SELECT * FROM security_appeal_operations WHERE operation_hash=?', (operation_hash,)).fetchone()
        if old:
            if not hmac.compare_digest(old['params_hmac'], digest):
                raise SecurityError('同一操作标识不能用于不同申诉内容', 409, 'idempotency_conflict')
            return operation_hash, digest, json.loads(old['result_json'])
        return operation_hash, digest, None

    @staticmethod
    def _save_receipt(conn, operation_hash, digest, appeal_id, action, actor_hash, result, now, expires):
        conn.execute('INSERT INTO security_appeal_operations VALUES(?,?,?,?,?,?,?,?)',
            (operation_hash, digest, appeal_id, action, actor_hash,
             json.dumps(result, ensure_ascii=False, sort_keys=True), now, expires))

    def submit(self, principal, operation_id, *, risk_event_id=None, code=None, note=''):
        ph, note, now = self._principal_hash(principal), self._text(note), self._now()
        if risk_event_id is not None and (not isinstance(risk_event_id, str) or not re.fullmatch(r'risk_[a-f0-9]{32}', risk_event_id)):
            raise SecurityError('风险记录无效', 400, 'invalid_request')
        if risk_event_id is None and (not isinstance(code, str) or code not in FIXED_CODES):
            raise SecurityError('请选择有效的申诉类别', 400, 'invalid_request')
        with self._conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            self._cleanup(conn, now)
            params = {'risk_event_id': risk_event_id, 'code': code, 'note_hmac': self._hash('appeal-note', note)}
            oh, digest, old = self._receipt(conn, 'submit', ph, operation_id, params)
            if old is not None:
                return old
            if risk_event_id:
                try:
                    event = conn.execute('SELECT code FROM risk_audit_events WHERE risk_event_id=? '
                        'AND principal_hash=? AND expires_at>?', (risk_event_id, ph, now)).fetchone()
                except sqlite3.OperationalError:
                    event = None
                if event is None:
                    raise SecurityError('风险记录不存在或不属于当前身份', 404, 'risk_event_missing')
                if code is not None and code != event['code']:
                    raise SecurityError('风险记录与类别不一致', 400, 'invalid_request')
                code = event['code']
            if conn.execute('SELECT COUNT(*) FROM security_appeals').fetchone()[0] >= self.max_appeals:
                raise SecurityError('申诉服务繁忙，请稍后重试', 503, 'appeal_capacity')
            if conn.execute("SELECT COUNT(*) FROM security_appeals WHERE principal_hash=? AND status='pending'", (ph,)).fetchone()[0] >= 3:
                raise SecurityError('请先等待已有申诉处理', 429, 'appeal_limit')
            if conn.execute('SELECT COUNT(*) FROM security_appeals WHERE principal_hash=? AND created_at>?', (ph, now - 86400)).fetchone()[0] >= 10:
                raise SecurityError('申诉请求较多，请稍后重试', 429, 'appeal_limit')
            identity, expires = uuid.uuid4().hex, now + self.retention_seconds
            conn.execute("INSERT INTO security_appeals VALUES(?,?,?,?,?,'pending',?,?,NULL,NULL,NULL)",
                         (identity, ph, risk_event_id, code, note, now, expires))
            result = {'ok': True, 'appeal_id': identity, 'status': 'pending',
                      'created_at': now, 'expires_at': expires, 'risk_event_id': risk_event_id, 'code': code}
            self._save_receipt(conn, oh, digest, identity, 'submit', ph, result, now, expires)
            conn.execute('INSERT INTO security_appeal_audit(appeal_id,action,actor_hash,metadata_json,created_at,expires_at) VALUES(?,?,?,?,?,?)',
                         (identity, 'submit', ph, json.dumps({'code': code, 'risk_event_id': risk_event_id,
                          'note_hmac': params['note_hmac']}), now, expires))
            return result

    @staticmethod
    def _id(value):
        if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{32}', value):
            raise SecurityError('申诉记录无效', 400, 'invalid_request')
        return value

    def get_own(self, principal, appeal_id):
        ph, identity = self._principal_hash(principal), self._id(appeal_id)
        with self._conn() as conn:
            row = conn.execute('SELECT * FROM security_appeals WHERE appeal_id=? AND principal_hash=? AND expires_at>?',
                               (identity, ph, self._now())).fetchone()
        if not row:
            raise SecurityError('申诉记录不存在', 404, 'appeal_missing')
        return {'ok': True, **self._public(row)}

    def get_admin(self, appeal_id):
        with self._conn() as conn:
            row = conn.execute('SELECT * FROM security_appeals WHERE appeal_id=? AND expires_at>?',
                               (self._id(appeal_id), self._now())).fetchone()
        if not row:
            raise SecurityError('申诉记录不存在', 404, 'appeal_missing')
        return {'ok': True, **self._public(row, admin=True)}

    def matches_principal(self, appeal_id, principal):
        ph, identity = self._principal_hash(principal), self._id(appeal_id)
        with self._conn() as conn:
            row = conn.execute('SELECT principal_hash FROM security_appeals WHERE appeal_id=? AND expires_at>?',
                               (identity, self._now())).fetchone()
        return bool(row and hmac.compare_digest(ph, row['principal_hash']))

    def _list(self, ph=None, *, limit=50, offset=0, status=None, admin=False):
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= self.max_appeals:
            raise SecurityError('申诉分页参数无效', 400, 'invalid_request')
        if status not in (None, 'pending', 'approved', 'denied'):
            raise SecurityError('申诉状态无效', 400, 'invalid_request')
        where, args = ['expires_at>?'], [self._now()]
        if ph:
            where.append('principal_hash=?'); args.append(ph)
        if status:
            where.append('status=?'); args.append(status)
        clause = ' AND '.join(where)
        with self._conn() as conn:
            conn.execute('BEGIN')
            total = conn.execute('SELECT COUNT(*) FROM security_appeals WHERE ' + clause, args).fetchone()[0]
            rows = conn.execute('SELECT * FROM security_appeals WHERE ' + clause +
                ' ORDER BY created_at DESC,rowid DESC LIMIT ? OFFSET ?', (*args, limit, offset)).fetchall()
        more = offset + len(rows) < total
        return {'ok': True, 'items': [self._public(row, admin=admin) for row in rows],
                'total': total, 'limit': limit, 'offset': offset, 'has_more': more,
                'next_offset': offset + len(rows) if more else None}

    def list_own(self, principal, *, limit=20, offset=0):
        return self._list(self._principal_hash(principal), limit=limit, offset=offset)

    def list_admin(self, *, limit=50, offset=0, status=None):
        return self._list(limit=limit, offset=offset, status=status, admin=True)

    def review(self, appeal_id, decision, admin_id, operation_id, reason):
        identity, reason, now = self._id(appeal_id), self._text(reason, required=True, maximum=200), self._now()
        if decision not in ('approved', 'denied'):
            raise SecurityError('复核结论无效', 400, 'invalid_request')
        with self._conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            self._cleanup(conn, now)
            self._admin(conn, admin_id)
            actor_hash = self._hash('appeal-admin', str(admin_id))
            params = {'appeal_id': identity, 'decision': decision, 'reason_hmac': self._hash('appeal-review-reason', reason)}
            oh, digest, old = self._receipt(conn, 'review', actor_hash, operation_id, params)
            if old is not None:
                return old
            row = conn.execute('SELECT * FROM security_appeals WHERE appeal_id=?', (identity,)).fetchone()
            if not row:
                raise SecurityError('申诉记录不存在', 404, 'appeal_missing')
            if row['status'] != 'pending':
                raise SecurityError('申诉已经复核，请查看原处理结论', 409, 'appeal_closed')
            conn.execute('UPDATE security_appeals SET status=?,reviewed_at=?,reviewed_by=?,review_reason=? WHERE appeal_id=?',
                         (decision, now, admin_id, reason, identity))
            result = {'ok': True, 'appeal_id': identity, 'status': decision, 'reviewed_at': now}
            self._save_receipt(conn, oh, digest, identity, 'review', actor_hash, result, now, row['expires_at'])
            conn.execute('INSERT INTO security_appeal_audit(appeal_id,action,actor_hash,metadata_json,created_at,expires_at) VALUES(?,?,?,?,?,?)',
                         (identity, 'review', actor_hash, json.dumps({'decision': decision,
                          'reason_hmac': params['reason_hmac'], 'admin_id': admin_id}), now, row['expires_at']))
            return result

    def cleanup(self):
        with self._conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            return self._cleanup(conn, self._now())
