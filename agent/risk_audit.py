"""Bounded security decision audit; callers supply server-owned rule labels.

This store never accepts an exception message, request body or credentials.
HMAC identifiers remain personal data and expire with the configured policy.
Only authenticated administrators may receive list/summary results; ordinary
clients should receive the opaque event ID and a generic decision code.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

RULES_VERSION = 'credits-security-v1.1'
BUCKET_SECONDS = 3600
_LABEL = re.compile(r'[A-Za-z][A-Za-z0-9_.:-]{0,79}\Z')
_OUTCOMES = {'blocked', 'allowed', 'pending', 'error', 'challenged'}

SCHEMA = '''
CREATE TABLE IF NOT EXISTS risk_audit_events(
 risk_event_id TEXT PRIMARY KEY,action TEXT NOT NULL,code TEXT NOT NULL,
 outcome TEXT NOT NULL,rules_version TEXT NOT NULL,principal_hash TEXT,
 source_hash TEXT,operation_hash TEXT,dedupe_hash TEXT UNIQUE,
 occurred_at REAL NOT NULL,expires_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_risk_audit_time
 ON risk_audit_events(occurred_at,risk_event_id);
CREATE INDEX IF NOT EXISTS idx_risk_audit_expiry ON risk_audit_events(expires_at);
CREATE INDEX IF NOT EXISTS idx_risk_audit_rule
 ON risk_audit_events(code,occurred_at);
CREATE TABLE IF NOT EXISTS risk_audit_counts(
 bucket INTEGER NOT NULL,action TEXT NOT NULL,code TEXT NOT NULL,
 outcome TEXT NOT NULL,rules_version TEXT NOT NULL,event_count INTEGER NOT NULL,
 PRIMARY KEY(bucket,action,code,outcome,rules_version));
'''


class RiskAuditStore:
    def __init__(self, db_path, secret, *, retention_days=180, max_events=50000,
                 max_counters=10000, rules_version=RULES_VERSION, clock=None):
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError('risk audit secret must contain at least 32 bytes')
        for value, maximum in ((retention_days, 3650), (max_events, 1000000),
                               (max_counters, 100000)):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError('invalid risk audit retention or capacity')
        self._label(rules_version)
        self.db_path, self.secret = Path(db_path), secret
        self.retention_seconds = retention_days * 86400
        self.max_events, self.max_counters = max_events, max_counters
        self.rules_version, self.clock = rules_version, clock or time.time
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

    @staticmethod
    def _label(value):
        if not isinstance(value, str) or not _LABEL.fullmatch(value):
            raise ValueError('invalid risk audit rule label')
        return value

    def _hash(self, purpose, value):
        if value is None:
            return None
        if not isinstance(value, str) or not 1 <= len(value) <= 256:
            raise ValueError('invalid risk audit identifier')
        return hmac.new(self.secret, (purpose + ':' + value).encode(),
                        hashlib.sha256).hexdigest()

    def _source_hash(self, source):
        if source is None:
            return None
        if not isinstance(source, str) or '%' in source or len(source) > 64:
            raise ValueError('invalid risk audit source')
        try:
            address = ipaddress.ip_address(source.strip())
        except ValueError:
            raise ValueError('invalid risk audit source') from None
        normalized = str(getattr(address, 'ipv4_mapped', None) or address)
        return self._hash('source', normalized)

    @staticmethod
    def _timestamp(value):
        if (type(value) not in (int, float) or not math.isfinite(value)
                or not 0 <= value <= 253402300799):
            raise ValueError('invalid risk audit time range')
        return float(value)

    def _cleanup(self, conn, now):
        deleted = conn.execute('DELETE FROM risk_audit_events WHERE expires_at<=?',
                               (now,)).rowcount
        # Counts contain no subject IDs and have hour precision. Discard the
        # boundary bucket too, so no counter exceeds the configured lifetime.
        conn.execute('DELETE FROM risk_audit_counts WHERE bucket<?',
                     (now - self.retention_seconds,))
        return deleted

    def record(self, action, code, *, principal=None, source=None,
               outcome='blocked', operation_id=None):
        action, code = self._label(action), self._label(code)
        if not isinstance(outcome, str) or outcome not in _OUTCOMES:
            raise ValueError('invalid risk audit outcome')
        ph, sh = self._hash('principal', principal), self._source_hash(source)
        oh = self._hash('operation', operation_id)
        dedupe = None
        if oh is not None:
            # An operation retry from another network retains its event ID.
            # Distinct decisions/rules for the same operation remain visible.
            data = json.dumps([action, code, outcome, self.rules_version,
                               ph or sh, oh], separators=(',', ':'))
            dedupe = hmac.new(self.secret, ('decision:' + data).encode(),
                              hashlib.sha256).hexdigest()
        now = self._timestamp(self.clock())
        bucket = int(now // BUCKET_SECONDS) * BUCKET_SECONDS
        with self._conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            self._cleanup(conn, now)
            if dedupe:
                old = conn.execute('SELECT risk_event_id FROM risk_audit_events '
                                   'WHERE dedupe_hash=?', (dedupe,)).fetchone()
                if old:
                    return old[0]
            identity = 'risk_' + uuid.uuid4().hex
            conn.execute('INSERT INTO risk_audit_events VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                         (identity, action, code, outcome, self.rules_version,
                          ph, sh, oh, dedupe, now, now + self.retention_seconds))
            conn.execute('''INSERT INTO risk_audit_counts VALUES(?,?,?,?,?,1)
                ON CONFLICT(bucket,action,code,outcome,rules_version)
                DO UPDATE SET event_count=event_count+1''',
                         (bucket, action, code, outcome, self.rules_version))
            count = conn.execute('SELECT COUNT(*) FROM risk_audit_events').fetchone()[0]
            if count > self.max_events:
                conn.execute('''DELETE FROM risk_audit_events WHERE risk_event_id IN
                    (SELECT risk_event_id FROM risk_audit_events
                     ORDER BY occurred_at,rowid LIMIT ?)''',
                             (count - self.max_events,))
            count = conn.execute('SELECT COUNT(*) FROM risk_audit_counts').fetchone()[0]
            if count > self.max_counters:
                conn.execute('''DELETE FROM risk_audit_counts WHERE rowid IN
                    (SELECT rowid FROM risk_audit_counts ORDER BY bucket,rowid LIMIT ?)''',
                             (count - self.max_counters,))
            return identity

    def _window(self, since, until):
        now = self._timestamp(self.clock())
        lower = max(0, now - self.retention_seconds)
        since = lower if since is None else self._timestamp(since)
        until = now + 1 if until is None else self._timestamp(until)
        if since > until or until - since > self.retention_seconds + 1:
            raise ValueError('invalid risk audit time range')
        return max(lower, since), min(now + 1, until)

    def list(self, *, limit=50, offset=0, action=None, code=None, outcome=None,
             since=None, until=None):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('invalid risk audit page limit')
        if type(offset) is not int or not 0 <= offset <= self.max_events:
            raise ValueError('invalid risk audit page offset')
        since, until = self._window(since, until)
        where, args = ['occurred_at>=?', 'occurred_at<?', 'expires_at>?'], [since, until, self.clock()]
        for field, value in (('action', action), ('code', code), ('outcome', outcome)):
            if value is None:
                continue
            if field == 'outcome':
                if not isinstance(value, str) or value not in _OUTCOMES:
                    raise ValueError('invalid risk audit outcome')
            else:
                self._label(value)
            where.append(field + '=?'); args.append(value)
        clause = ' AND '.join(where)
        with self._conn() as conn:
            conn.execute('BEGIN')
            total = conn.execute('SELECT COUNT(*) FROM risk_audit_events WHERE ' + clause,
                                 args).fetchone()[0]
            rows = conn.execute('SELECT risk_event_id,action,code,outcome,rules_version,'
                                'principal_hash,source_hash,operation_hash,occurred_at,expires_at '
                                'FROM risk_audit_events WHERE ' + clause +
                                ' ORDER BY occurred_at DESC,rowid DESC LIMIT ? OFFSET ?',
                                (*args, limit, offset)).fetchall()
        more = offset + len(rows) < total
        return {'items': [dict(row) for row in rows], 'total': total,
                'limit': limit, 'offset': offset, 'has_more': more,
                'next_offset': offset + len(rows) if more else None}

    def summary(self, *, since=None, until=None, limit=100):
        """Hourly aggregates survive detail capacity eviction, with bounded rows.

        Report the expanded hour window explicitly; no subject hashes are
        included. Counters older than the retention cutoff are always hidden.
        """
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('invalid risk audit summary limit')
        since, until = self._window(since, until)
        lower = int(since // BUCKET_SECONDS) * BUCKET_SECONDS
        upper = lower if since >= until else int(math.ceil(until / BUCKET_SECONDS)) * BUCKET_SECONDS
        with self._conn() as conn:
            conn.execute('BEGIN')
            params = (lower, upper, self.clock() - self.retention_seconds)
            rows = conn.execute('''SELECT action,code,outcome,rules_version,
                SUM(event_count) AS event_count FROM risk_audit_counts
                WHERE bucket>=? AND bucket<? AND bucket>=?
                GROUP BY action,code,outcome,rules_version
                ORDER BY event_count DESC,code,action,outcome LIMIT ?''',
                                (*params, limit + 1)).fetchall()
            total = conn.execute('''SELECT COALESCE(SUM(event_count),0)
                FROM risk_audit_counts WHERE bucket>=? AND bucket<? AND bucket>=?''',
                                 params).fetchone()[0]
            action_rows = conn.execute('''SELECT action,SUM(event_count) AS count
                FROM risk_audit_counts WHERE bucket>=? AND bucket<? AND bucket>=?
                GROUP BY action ORDER BY count DESC,action LIMIT ?''',
                                       (*params, limit + 1)).fetchall()
            outcome_rows = conn.execute('''SELECT outcome,SUM(event_count) AS count
                FROM risk_audit_counts WHERE bucket>=? AND bucket<? AND bucket>=?
                GROUP BY outcome ORDER BY outcome''', params).fetchall()
            retained = conn.execute('SELECT COUNT(*) FROM risk_audit_events WHERE expires_at>?',
                                    (self.clock(),)).fetchone()[0]
            counters = conn.execute('SELECT COUNT(*) FROM risk_audit_counts WHERE bucket>=?',
                                    (self.clock() - self.retention_seconds,)).fetchone()[0]
        rules = [dict(row) for row in rows[:limit]]
        actions = {row['action']: row['count'] for row in action_rows[:limit]}
        outcomes = {row['outcome']: row['count'] for row in outcome_rows}
        return {'rules_version': self.rules_version, 'total': total,
                'by_rule': rules, 'by_action': actions, 'by_outcome': outcomes,
                'has_more_rules': len(rows) > limit, 'has_more_actions': len(action_rows) > limit,
                'since': lower, 'until': upper, 'bucket_seconds': BUCKET_SECONDS,
                'retained_events': retained, 'max_events': self.max_events,
                'max_counters': self.max_counters, 'retained_counters': counters,
                'capacity_warning': retained * 5 >= self.max_events * 4 or counters * 5 >= self.max_counters * 4,
                'detail_capacity_reached': retained >= self.max_events,
                'counter_capacity_reached': counters >= self.max_counters}

    def cleanup(self):
        with self._conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            return self._cleanup(conn, self._timestamp(self.clock()))
