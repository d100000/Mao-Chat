"""Audited operator configuration with versioned private files.

SQLite publishes a reference only after its complete 0600 file is durable.
An interrupted uncommitted write cannot activate a partial configuration.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import uuid

from .billing_admin import _admin, _operation, _reason
from .points import PointsError, _decimal


SCHEMA = '''
CREATE TABLE IF NOT EXISTS security_configuration(
 id TEXT PRIMARY KEY,operation_id TEXT NOT NULL UNIQUE,digest TEXT NOT NULL,
 admin_id INTEGER NOT NULL,public_json TEXT NOT NULL,secret_file TEXT NOT NULL,
 content_hash TEXT NOT NULL,reason TEXT NOT NULL,created REAL NOT NULL);
CREATE TRIGGER IF NOT EXISTS security_configuration_immutable_update
 BEFORE UPDATE ON security_configuration BEGIN SELECT RAISE(ABORT,'immutable security configuration'); END;
CREATE TRIGGER IF NOT EXISTS security_configuration_immutable_delete
 BEFORE DELETE ON security_configuration BEGIN SELECT RAISE(ABORT,'immutable security configuration'); END;
'''


class SecurityConfigStore:
    def __init__(self, db_path, secret):
        self.db_path, self.secret = Path(db_path), secret
        self.directory = Path(str(self.db_path) + '.security-config')
        with self._conn() as conn: conn.executescript(SCHEMA)

    def _conn(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _encoded(value):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()

    def _load(self, conn):
        row = conn.execute('SELECT * FROM security_configuration ORDER BY created DESC,rowid DESC LIMIT 1').fetchone()
        if row is None:
            return {'site_key': os.environ.get('MAO_TURNSTILE_SITE_KEY', ''),
                    'secret': os.environ.get('MAO_TURNSTILE_SECRET', ''),
                    'hostname': os.environ.get('MAO_PUBLIC_HOSTNAME', '').strip().lower().rstrip('.'),
                    'search_call_bound_cny': os.environ.get('MAO_SEARCH_CALL_BOUND_CNY', '')}
        path = self.directory / row['secret_file']
        try:
            content = path.read_bytes()
            if not hmac.compare_digest(hashlib.sha256(content).hexdigest(), row['content_hash']): raise ValueError()
            result = json.loads(content)
            if not isinstance(result, dict): raise ValueError()
            return result
        except (OSError, ValueError):
            raise PointsError('安全配置文件不可用，请在后台重新保存配置', 503, 'security_config_unavailable') from None

    def load(self):
        with self._conn() as conn: return self._load(conn)

    @staticmethod
    def public(values):
        return {'site_key': values.get('site_key', ''), 'hostname': values.get('hostname', ''),
                'secret_configured': bool(values.get('secret')),
                'configured': bool(values.get('site_key') and values.get('hostname') and values.get('secret')),
                'search_call_bound_cny': values.get('search_call_bound_cny', '')}

    @staticmethod
    def validate(values):
        if set(values) - {'site_key', 'hostname', 'secret', 'search_call_bound_cny'}:
            raise PointsError('安全配置字段无效')
        for key in ('site_key', 'hostname', 'secret'):
            if key in values and (not isinstance(values[key], str) or len(values[key]) > (512 if key == 'secret' else 253)
                    or any(ord(c) < 33 or ord(c) == 127 for c in values[key])):
                raise PointsError('安全配置字段格式无效')
        result = dict(values)
        if 'hostname' in result:
            result['hostname'] = result['hostname'].lower().rstrip('.')
            if result['hostname'] and not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', result['hostname']):
                raise PointsError('访问域名格式无效，请勿包含协议、端口或路径')
        if 'search_call_bound_cny' in result:
            value = result['search_call_bound_cny']
            if value in ('', None): result['search_call_bound_cny'] = ''
            else:
                amount = _decimal(value, maximum='1000')
                if amount <= 0: raise PointsError('搜索成本上界必须大于0且不超过1000元')
                result['search_call_bound_cny'] = str(amount)
        return result

    def update(self, values, admin_id, operation_id, reason):
        values, admin_id, operation_id, reason = self.validate(values), _admin(admin_id), _operation(operation_id), _reason(reason)
        digest = hmac.new(self.secret, self._encoded([values, admin_id, reason]), hashlib.sha256).hexdigest()
        filename = None
        with self._conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if not conn.execute('SELECT 1 FROM admins WHERE admin_id=? AND disabled=0', (admin_id,)).fetchone():
                raise PointsError('管理员身份无效', 403)
            old = conn.execute('SELECT * FROM security_configuration WHERE operation_id=?', (operation_id,)).fetchone()
            if old:
                if old['digest'] != digest: raise PointsError('操作标识不能用于不同安全配置', 409, 'idempotency_conflict')
                return {'ok': True, 'config': json.loads(old['public_json']), 'configuration_id': old['id']}
            if conn.execute('SELECT COUNT(*) FROM security_configuration').fetchone()[0] >= 10000:
                raise PointsError('安全配置审计容量已满，请联系运维', 503)
            try: current = self._load(conn)
            except PointsError: current = {'site_key': '', 'hostname': '', 'secret': '', 'search_call_bound_cny': ''}
            # An empty password field means unchanged, never accidental erase.
            if not values.get('secret'): values.pop('secret', None)
            current.update(values)
            encoded = self._encoded(current)
            public = self.public(current)
            identity = uuid.uuid4().hex
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.directory.chmod(0o700)
            staging = None
            try:
                with tempfile.NamedTemporaryFile('wb', dir=self.directory, prefix='.', delete=False) as handle:
                    staging = Path(handle.name)
                    staging.chmod(0o600)
                    handle.write(encoded); handle.flush(); os.fsync(handle.fileno())
                filename = self.directory / (identity + '.json')
                os.replace(staging, filename); staging = None
                directory = os.open(self.directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
                try: os.fsync(directory)
                finally: os.close(directory)
                conn.execute('INSERT INTO security_configuration VALUES(?,?,?,?,?,?,?,?,?)',
                    (identity, operation_id, digest, admin_id, json.dumps(public), filename.name,
                     hashlib.sha256(encoded).hexdigest(), reason, time.time()))
                conn.commit()
            except BaseException:
                conn.rollback()
                if filename: filename.unlink(missing_ok=True)
                raise
            finally:
                if staging: staging.unlink(missing_ok=True)
            return {'ok': True, 'config': public, 'configuration_id': identity}
