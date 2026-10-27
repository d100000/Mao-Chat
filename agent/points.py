"""Server-authoritative trial credits, price snapshots and bounded cost accounting.

All monetary arithmetic uses Decimal and integer micro-points.  The caller must
derive actors from authenticated sessions/capabilities, never request JSON.
No conversation content or provider credentials belong in this store.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
import uuid
from zoneinfo import ZoneInfo

MICRO = 1_000_000
TRIAL = 100 * MICRO
BUCKETS = ('input_uncached', 'cache_read', 'cache_write', 'billable_output')
ACTIVE = ('reserved', 'running', 'usage_pending')
FINAL = ('settled', 'platform_absorbed', 'usage_released')
SHANGHAI = ZoneInfo('Asia/Shanghai')
MAX_PENDING_QUOTES_PER_PRINCIPAL = 10
MAX_PENDING_QUOTES_TOTAL = 10_000


class PointsError(ValueError):
    def __init__(self, message, status=400, code='invalid_request'):
        super().__init__(message)
        self.status, self.code = status, code


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _decimal(value, *, maximum='1000000000'):
    if isinstance(value, (bool, float)):
        raise PointsError('金额必须使用十进制字符串')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise PointsError('金额格式无效') from None
    if not result.is_finite() or result < 0 or result > Decimal(maximum) or result.as_tuple().exponent < -12:
        raise PointsError('金额范围或精度无效')
    return result


def _count(value, maximum=10**9):
    if type(value) is not int or not 0 <= value <= maximum:
        raise PointsError('Token或数量范围无效')
    return value


def points_string(micro):
    return format(Decimal(micro) / MICRO, '.6f')


def cost_to_micro(cost):
    return int((Decimal(cost) * 1000 * MICRO).to_integral_value(rounding=ROUND_CEILING))


def _identity(actor):
    value = actor.get('principal') if isinstance(actor, dict) else actor
    if not isinstance(value, str) or not re.fullmatch(r'(?:u:[1-9][0-9]{0,17}|g:[a-f0-9]{32})', value):
        raise PointsError('身份无效', 401, 'identity_invalid')
    return value


def _operation(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}', value):
        raise PointsError('操作标识无效')
    return value


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
        raise PointsError('请求摘要无效')
    return value


def normalize_usage(usage, adapter='openai', *, cache_read_supported=False, cache_write_supported=False):
    """Return mutually exclusive buckets or None for incomplete/ambiguous usage.

    A missing field is never invented as zero for a supported billing bucket.
    Details such as reasoning_tokens remain part of completion_tokens.
    """
    if not isinstance(usage, dict):
        return None
    try:
        if adapter == 'anthropic':
            uncached = _count(usage['input_tokens'])
            output = _count(usage['output_tokens'])
            read = _count(usage['cache_read_input_tokens']) if cache_read_supported else _count(usage.get('cache_read_input_tokens', 0))
            write = _count(usage['cache_creation_input_tokens']) if cache_write_supported else _count(usage.get('cache_creation_input_tokens', 0))
            total_input = uncached + read + write
            # Some compatible gateways emit both formats, including dummy
            # Anthropic zero fields. Conflicting aliases cannot prove usage.
            for alias, expected in (('prompt_tokens', total_input), ('completion_tokens', output),
                                    ('total_tokens', total_input + output)):
                if alias in usage and _count(usage[alias]) != expected:
                    return None
        elif adapter in ('openai', 'deepseek'):
            total = _count(usage['prompt_tokens'])
            output = _count(usage['completion_tokens'])
            detail = usage.get('prompt_tokens_details') or {}
            if not isinstance(detail, dict):
                return None
            aliases = [usage[k] for k in ('prompt_cache_hit_tokens',) if k in usage]
            if 'cached_tokens' in detail:
                aliases.append(detail['cached_tokens'])
            if aliases:
                read = _count(aliases[0])
                if any(_count(x) != read for x in aliases):
                    return None
            elif cache_read_supported or adapter == 'deepseek':
                return None
            else:
                read = 0
            write = _count(usage.get('cache_creation_input_tokens', 0))
            if cache_write_supported and 'cache_creation_input_tokens' not in usage:
                return None
            uncached = total - read - write
            if uncached < 0:
                return None
            if 'prompt_cache_miss_tokens' in usage and _count(usage['prompt_cache_miss_tokens']) != uncached:
                return None
            if 'total_tokens' in usage and _count(usage['total_tokens']) != total + output:
                return None
        else:
            return None
        if (not cache_read_supported and read) or (not cache_write_supported and write):
            return None
        return dict(zip(BUCKETS, (uncached, read, write, output)))
    except (KeyError, TypeError, PointsError):
        return None


SCHEMA = '''
CREATE TABLE IF NOT EXISTS points_policy(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS points_principals(
 principal TEXT PRIMARY KEY, benefit_pending INTEGER NOT NULL DEFAULT 0,
 claim_used INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS points_browsers(
 id INTEGER PRIMARY KEY AUTOINCREMENT, token_hash TEXT NOT NULL UNIQUE,
 entitlement TEXT NOT NULL UNIQUE, granted INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS points_wallets(
 id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL,
 entitlement TEXT UNIQUE, claimed_to TEXT, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS points_wallet_owner ON points_wallets(owner);
CREATE TABLE IF NOT EXISTS points_wallet_capabilities(
 token_hash TEXT PRIMARY KEY, wallet_id INTEGER NOT NULL REFERENCES points_wallets(id), created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS points_guests(
 id INTEGER PRIMARY KEY AUTOINCREMENT, principal TEXT NOT NULL UNIQUE,
 token_hash TEXT NOT NULL UNIQUE, browser_id INTEGER NOT NULL REFERENCES points_browsers(id),
 wallet_id INTEGER NOT NULL REFERENCES points_wallets(id), content_claimed_to TEXT,
 created REAL NOT NULL, last_active REAL NOT NULL);
CREATE TABLE IF NOT EXISTS points_lots(
 id INTEGER PRIMARY KEY AUTOINCREMENT, wallet_id INTEGER NOT NULL REFERENCES points_wallets(id),
 grant_key TEXT NOT NULL UNIQUE, source TEXT NOT NULL, amount INTEGER NOT NULL,
 available INTEGER NOT NULL, held INTEGER NOT NULL DEFAULT 0,
 spent INTEGER NOT NULL DEFAULT 0, expired INTEGER NOT NULL DEFAULT 0,
 created REAL NOT NULL, expires REAL NOT NULL,
 CHECK(amount>=0 AND available>=0 AND held>=0 AND spent>=0 AND expired>=0),
 CHECK(amount=available+held+spent+expired));
CREATE INDEX IF NOT EXISTS points_lots_wallet_expiry ON points_lots(wallet_id,expires,id);
CREATE TABLE IF NOT EXISTS points_ledger(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_key TEXT NOT NULL UNIQUE,
 wallet_id INTEGER NOT NULL REFERENCES points_wallets(id), lot_id INTEGER REFERENCES points_lots(id),
 event TEXT NOT NULL, amount INTEGER NOT NULL, delta_available INTEGER NOT NULL DEFAULT 0,
 delta_held INTEGER NOT NULL DEFAULT 0, delta_spent INTEGER NOT NULL DEFAULT 0,
 delta_expired INTEGER NOT NULL DEFAULT 0, request_id TEXT, original_id INTEGER,
 metadata TEXT NOT NULL DEFAULT '{}', created REAL NOT NULL);
CREATE TRIGGER IF NOT EXISTS points_ledger_immutable BEFORE UPDATE ON points_ledger
 BEGIN SELECT RAISE(ABORT,'immutable points ledger'); END;
CREATE INDEX IF NOT EXISTS points_ledger_lot_order ON points_ledger(lot_id,id);
CREATE INDEX IF NOT EXISTS points_ledger_request_event ON points_ledger(request_id,event,lot_id);
CREATE TABLE IF NOT EXISTS points_prices(
 id TEXT PRIMARY KEY, provider TEXT NOT NULL, gateway TEXT NOT NULL, model TEXT NOT NULL,
 rates TEXT NOT NULL, user_rates TEXT NOT NULL, adapter TEXT NOT NULL,
 cache_read_supported INTEGER NOT NULL, cache_write_supported INTEGER NOT NULL,
 status TEXT NOT NULL, effective REAL NOT NULL, created REAL NOT NULL, admin_id INTEGER);
CREATE INDEX IF NOT EXISTS points_prices_lookup ON points_prices(provider,gateway,model,effective);
CREATE TABLE IF NOT EXISTS points_quotes(
 id TEXT PRIMARY KEY, principal TEXT NOT NULL, digest TEXT NOT NULL, price_id TEXT NOT NULL REFERENCES points_prices(id),
 input_cap INTEGER NOT NULL, output_cap INTEGER NOT NULL, call_cap INTEGER NOT NULL,
 max_micro INTEGER NOT NULL, cash_max TEXT NOT NULL, extra_cash TEXT NOT NULL,
 created REAL NOT NULL, expires REAL NOT NULL, used_request TEXT);
CREATE INDEX IF NOT EXISTS points_unused_quote_expiry ON points_quotes(expires,principal) WHERE used_request IS NULL;
CREATE INDEX IF NOT EXISTS points_principal_unused_quotes ON points_quotes(principal,expires) WHERE used_request IS NULL;
CREATE TABLE IF NOT EXISTS points_requests(
 id TEXT PRIMARY KEY, principal TEXT NOT NULL, wallet_id INTEGER NOT NULL REFERENCES points_wallets(id),
 client_key TEXT NOT NULL, digest TEXT NOT NULL, quote_id TEXT NOT NULL UNIQUE REFERENCES points_quotes(id),
 state TEXT NOT NULL, max_micro INTEGER NOT NULL, charged_micro INTEGER NOT NULL DEFAULT 0,
 supplier_cost TEXT, user_cost TEXT, platform_cost TEXT, created REAL NOT NULL, updated REAL NOT NULL,
 delivered INTEGER NOT NULL DEFAULT 0, cancelled INTEGER NOT NULL DEFAULT 0,
 failure INTEGER NOT NULL DEFAULT 0, over_budget INTEGER NOT NULL DEFAULT 0,
 UNIQUE(principal,client_key));
CREATE INDEX IF NOT EXISTS points_requests_wallet_state ON points_requests(wallet_id,state);
CREATE INDEX IF NOT EXISTS points_requests_retention ON points_requests(state,updated);
CREATE TABLE IF NOT EXISTS points_allocations(
 request_id TEXT NOT NULL REFERENCES points_requests(id), lot_id INTEGER NOT NULL REFERENCES points_lots(id),
 reserved INTEGER NOT NULL, charged INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(request_id,lot_id));
CREATE TABLE IF NOT EXISTS points_cash_commitments(
 request_id TEXT PRIMARY KEY REFERENCES points_requests(id), day TEXT NOT NULL, remaining TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS points_calls(
 id TEXT NOT NULL, request_id TEXT NOT NULL REFERENCES points_requests(id),
 provider_key TEXT UNIQUE, provider_request_id TEXT, kind TEXT NOT NULL DEFAULT 'model', day TEXT NOT NULL,
 bound_input INTEGER NOT NULL DEFAULT 0, bound_output INTEGER NOT NULL DEFAULT 0,
 bound_cost TEXT NOT NULL, cost TEXT, user_cost TEXT, usage TEXT,
 state TEXT NOT NULL, billable INTEGER NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
 PRIMARY KEY(request_id,id));
CREATE INDEX IF NOT EXISTS points_calls_day ON points_calls(day,state);
CREATE INDEX IF NOT EXISTS points_calls_created ON points_calls(created);
CREATE TABLE IF NOT EXISTS points_operations(
 key TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS points_cash_days(day TEXT PRIMARY KEY, limit_cny TEXT NOT NULL);
'''


class PointsStore:
    def __init__(self, db_path, *, clock=None, trial_days=180, pending_seconds=86400, request_lease_seconds=900):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or time.time
        self.trial_seconds = max(1, int(trial_days)) * 86400
        self.pending_seconds = max(1, int(pending_seconds))
        self.request_lease_seconds = max(900,int(request_lease_seconds))
        self._transactions = threading.local()
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            conn.execute('BEGIN IMMEDIATE')
            if 'provider_request_id' not in {r[1] for r in conn.execute('PRAGMA table_info(points_calls)')}:
                conn.execute('ALTER TABLE points_calls ADD COLUMN provider_request_id TEXT')

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        return conn

    @contextmanager
    def _tx(self):
        current = getattr(self._transactions, 'connection', None)
        if current is not None:
            savepoint = 'nested_' + uuid.uuid4().hex
            current.execute('SAVEPOINT ' + savepoint)
            try:
                yield current
                current.execute('RELEASE SAVEPOINT ' + savepoint)
            except BaseException:
                current.execute('ROLLBACK TO SAVEPOINT ' + savepoint)
                current.execute('RELEASE SAVEPOINT ' + savepoint)
                raise
            return
        conn = self._connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            self._transactions.connection = conn
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            self._transactions.connection = None
            conn.close()

    def _now(self):
        return float(self.clock())

    def _day(self, timestamp=None):
        return datetime.fromtimestamp(self._now() if timestamp is None else timestamp, SHANGHAI).date().isoformat()

    def _day_end(self, timestamp=None):
        current = datetime.fromtimestamp(self._now() if timestamp is None else timestamp, SHANGHAI)
        return datetime.combine(current.date() + timedelta(days=1), datetime.min.time(), SHANGHAI).timestamp()

    @staticmethod
    def _token_hash(token):
        return _hash(token) if isinstance(token, str) and 32 <= len(token) <= 128 else ''

    def _ensure_user(self, conn, principal):
        conn.execute('INSERT OR IGNORE INTO points_principals(principal,created) VALUES(?,?)', (principal, self._now()))
        if principal.startswith('u:') and not conn.execute('SELECT 1 FROM points_wallets WHERE owner=?', (principal,)).fetchone():
            conn.execute('INSERT INTO points_wallets(owner,created) VALUES(?,?)', (principal, self._now()))

    def _guest_actor(self, row):
        return {'principal': row['principal'], 'kind': 'guest', 'storage_id': -int(row['id']), 'user_id': -int(row['id'])}

    def user_actor(self, user_id):
        if type(user_id) is not int or not 1 <= user_id < 10**18:
            raise PointsError('账户身份无效', 401)
        principal = 'u:' + str(user_id)
        with self._tx() as conn:
            self._ensure_user(conn, principal)
        return {'principal': principal, 'kind': 'user', 'storage_id': user_id, 'user_id': user_id}

    def bootstrap(self, guest_token=None, browser_token=None, wallet_token=None, *, rotate_workspace=False):
        """Separate content, wallet-recovery and deduplication capabilities."""
        output = {}
        with self._tx() as conn:
            guest = conn.execute('SELECT * FROM points_guests WHERE token_hash=? AND content_claimed_to IS NULL AND last_active>?', (self._token_hash(guest_token),self._now()-self.trial_seconds)).fetchone()
            if guest and not rotate_workspace:
                conn.execute('UPDATE points_guests SET last_active=? WHERE id=?', (self._now(), guest['id']))
                return {'actor': self._guest_actor(guest)}
            browser = conn.execute('SELECT * FROM points_browsers WHERE token_hash=?', (self._token_hash(browser_token),)).fetchone()
            if not browser:
                raw = secrets.token_urlsafe(32)
                cur = conn.execute('INSERT INTO points_browsers(token_hash,entitlement,created) VALUES(?,?,?)', (_hash(raw), uuid.uuid4().hex, self._now()))
                browser = conn.execute('SELECT * FROM points_browsers WHERE id=?', (cur.lastrowid,)).fetchone()
                output['browser_token'] = raw
            wallet = conn.execute('SELECT w.* FROM points_wallet_capabilities c JOIN points_wallets w ON c.wallet_id=w.id WHERE c.token_hash=? AND w.claimed_to IS NULL AND w.owner LIKE ?', (self._token_hash(wallet_token), 'g:%')).fetchone()
            principal = 'g:' + uuid.uuid4().hex
            if not wallet:
                # A deduplication marker can suppress a new grant but cannot
                # recover an existing wallet. New empty wallets get no regrant.
                entitlement = browser['entitlement'] if not browser['granted'] else None
                existing = conn.execute('SELECT 1 FROM points_wallets WHERE entitlement=?', (entitlement,)).fetchone() if entitlement else None
                cur = conn.execute('INSERT INTO points_wallets(owner,entitlement,created) VALUES(?,?,?)', (principal, None if existing else entitlement, self._now()))
                wallet = conn.execute('SELECT * FROM points_wallets WHERE id=?', (cur.lastrowid,)).fetchone()
                raw_wallet = secrets.token_urlsafe(32)
                conn.execute('INSERT INTO points_wallet_capabilities VALUES(?,?,?)', (_hash(raw_wallet), wallet['id'], self._now()))
                output['wallet_token'] = raw_wallet
            raw_guest = secrets.token_urlsafe(32)
            conn.execute('INSERT INTO points_guests(principal,token_hash,browser_id,wallet_id,created,last_active) VALUES(?,?,?,?,?,?)', (principal, _hash(raw_guest), browser['id'], wallet['id'], self._now(), self._now()))
            self._ensure_user(conn, principal)
            guest = conn.execute('SELECT * FROM points_guests WHERE principal=?', (principal,)).fetchone()
            output.update(actor=self._guest_actor(guest), guest_token=raw_guest)
            return output

    def resolve_guest(self, token):
        with self._tx() as conn:
            row = conn.execute('SELECT * FROM points_guests WHERE token_hash=? AND content_claimed_to IS NULL AND last_active>?', (self._token_hash(token), self._now() - self.trial_seconds)).fetchone()
            if not row:
                return None
            conn.execute('UPDATE points_guests SET last_active=? WHERE id=?', (self._now(), row['id']))
            return self._guest_actor(row)

    def resolve_wallet(self, token):
        """An anonymous wallet capability never authenticates chat content."""
        with self._connect() as conn:
            row = conn.execute('SELECT w.* FROM points_wallet_capabilities c JOIN points_wallets w ON w.id=c.wallet_id WHERE c.token_hash=? AND w.claimed_to IS NULL AND w.owner LIKE ?', (self._token_hash(token), 'g:%')).fetchone()
            if not row:
                return None
            return {'principal':row['owner'], 'kind':'guest', 'storage_id':None, 'wallet_id':row['id'], 'wallet_only':True}

    def _wallet_ids(self, conn, actor):
        principal = _identity(actor)
        if isinstance(actor, dict) and actor.get('wallet_only') is True:
            row = conn.execute('SELECT id FROM points_wallets WHERE id=? AND owner=? AND claimed_to IS NULL', (actor.get('wallet_id'), principal)).fetchone()
            if not row:
                raise PointsError('匿名钱包凭证失效', 401, 'wallet_claimed')
            return [row['id']]
        if principal.startswith('u:'):
            self._ensure_user(conn, principal)
            return [r['id'] for r in conn.execute('SELECT id FROM points_wallets WHERE owner=? ORDER BY id', (principal,))]
        row = conn.execute('SELECT g.wallet_id,w.claimed_to FROM points_guests g JOIN points_wallets w ON w.id=g.wallet_id WHERE g.principal=? AND g.content_claimed_to IS NULL', (principal,)).fetchone()
        if not row:
            raise PointsError('访客身份已失效', 401, 'guest_expired')
        if row['claimed_to']:
            raise PointsError('访客余额已归入账户', 401, 'wallet_claimed')
        return [row['wallet_id']]

    def _eligible(self, conn, actor):
        principal = _identity(actor)
        self._ensure_user(conn, principal)
        return not conn.execute('SELECT benefit_pending FROM points_principals WHERE principal=?', (principal,)).fetchone()[0]

    def set_benefit_pending(self, actor, pending=True):
        if type(pending) is not bool:
            raise PointsError('资格状态无效')
        principal = _identity(actor)
        with self._tx() as conn:
            self._ensure_user(conn, principal)
            conn.execute('UPDATE points_principals SET benefit_pending=? WHERE principal=?', (int(pending), principal))

    def _entry(self, conn, operation, wallet, lot, event, amount=0, *, available=0, held=0, spent=0, expired=0, request_id=None, original_id=None, metadata=None):
        conn.execute('''INSERT INTO points_ledger(operation_key,wallet_id,lot_id,event,amount,delta_available,delta_held,delta_spent,delta_expired,request_id,original_id,metadata,created)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''', (operation, wallet, lot, event, amount, available, held, spent, expired, request_id, original_id, _json(metadata or {}), self._now()))

    def _grant(self, conn, wallet, key, source, amount, expires):
        existing = conn.execute('SELECT id FROM points_lots WHERE grant_key=?', (key,)).fetchone()
        if existing:
            return existing[0]
        cur = conn.execute('INSERT INTO points_lots(wallet_id,grant_key,source,amount,available,created,expires) VALUES(?,?,?,?,?,?,?)', (wallet, key, source, amount, amount, self._now(), expires))
        self._entry(conn, key, wallet, cur.lastrowid, 'grant', amount, available=amount)
        return cur.lastrowid

    def grant_guest(self, actor):
        principal = _identity(actor)
        if not principal.startswith('g:') or (isinstance(actor, dict) and actor.get('wallet_only')):
            raise PointsError('仅访客领取体验额度')
        with self._tx() as conn:
            wallets = self._wallet_ids(conn, actor)
            if not self._eligible(conn, actor):
                raise PointsError('需完成补贴资格验证', 403, 'benefit_pending')
            wallet = conn.execute('SELECT * FROM points_wallets WHERE id=?', (wallets[0],)).fetchone()
            entitlement = wallet['entitlement']
            if entitlement:
                browser = conn.execute('SELECT * FROM points_browsers WHERE entitlement=?', (entitlement,)).fetchone()
                if browser and not browser['granted']:
                    self._grant(conn, wallet['id'], 'guest:' + entitlement, 'guest_trial', TRIAL, self._now() + self.trial_seconds)
                    conn.execute('UPDATE points_browsers SET granted=1 WHERE id=?', (browser['id'],))
        return self.wallet(actor, grant_daily=False)

    def _daily(self, conn, actor, wallets):
        # A date key and its expiry are one business decision. In particular,
        # never read the clock again for expiry after midnight changed the day.
        timestamp = self._now()
        principal = _identity(actor)
        if principal.startswith('u:') and self._eligible(conn, actor):
            self._grant(conn, wallets[0], 'daily:' + principal + ':' + self._day(timestamp), 'daily_gift', TRIAL, self._day_end(timestamp))

    def _expire(self, conn, wallets=None, limit=1000):
        sql = 'SELECT * FROM points_lots WHERE expires<=? AND available>0'
        args = [self._now()]
        if wallets is not None:
            sql += ' AND wallet_id IN (' + ','.join('?' for _ in wallets) + ')'
            args += wallets
        sql += ' ORDER BY id LIMIT ?'; args.append(limit)
        for lot in conn.execute(sql, args).fetchall():
            amount = lot['available']
            conn.execute('UPDATE points_lots SET available=0,expired=expired+? WHERE id=?', (amount, lot['id']))
            self._entry(conn, 'expire:' + str(lot['id']), lot['wallet_id'], lot['id'], 'expire', amount, available=-amount, expired=amount)

    def _wallet_public(self, conn, actor, wallets):
        rows = conn.execute('SELECT * FROM points_lots WHERE wallet_id IN (' + ','.join('?' for _ in wallets) + ') ORDER BY expires,id', wallets).fetchall()
        available, held = sum(r['available'] for r in rows), sum(r['held'] for r in rows)
        eligibility = self._eligible(conn, actor)
        return {'principal': _identity(actor), 'kind': 'user' if _identity(actor).startswith('u:') else 'guest',
                'available': points_string(available), 'held': points_string(held), 'available_micro': available, 'held_micro': held,
                'benefit_pending': not eligibility, 'trial_granted': any(r['source'] == 'guest_trial' for r in rows),
                'next_daily_at': self._day_end() if _identity(actor).startswith('u:') else None,
                'lots': [{'id': r['id'], 'source': r['source'], 'available': points_string(r['available']), 'held': points_string(r['held']), 'spent': points_string(r['spent']), 'expired': points_string(r['expired']), 'expires_at': r['expires']} for r in rows]}

    def wallet(self, actor, *, grant_daily=True):
        with self._tx() as conn:
            wallets = self._wallet_ids(conn, actor)
            self._expire(conn, wallets)
            self._recover_stale(conn,wallets)
            self._pending_deadlines(conn, wallets)
            if grant_daily:
                self._daily(conn, actor, wallets)
            return self._wallet_public(conn, actor, wallets)

    def policy(self):
        with self._connect() as conn:
            return self._policy_public(conn)

    def _policy_public(self, conn):
        row = conn.execute("SELECT value FROM points_policy WHERE key='daily_cash_limit_cny'").fetchone()
        return {'points_per_cny': 1000, 'trial_points': 100, 'daily_points': 100, 'trial_days': self.trial_seconds // 86400,
                'pending_seconds': self.pending_seconds, 'timezone': 'Asia/Shanghai', 'daily_cash_limit_cny': row[0] if row else None,
                'billing_ready': bool(row), 'price_required': True}

    def configure(self, values):
        if not isinstance(values, dict) or set(values) - {'daily_cash_limit_cny'}:
            raise PointsError('积分策略字段无效')
        with self._tx() as conn:
            if 'daily_cash_limit_cny' in values:
                amount = _decimal(values['daily_cash_limit_cny'], maximum='1000000')
                if amount <= 0:
                    raise PointsError('必须配置有限的正数现金预算', 422)
                conn.execute("INSERT INTO points_policy VALUES('daily_cash_limit_cny',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(amount),))
                # A change only affects future admissions; lowering the limit
                # below existing commitments never erases them.
                conn.execute('INSERT INTO points_cash_days VALUES(?,?) ON CONFLICT(day) DO UPDATE SET limit_cny=excluded.limit_cny', (self._day(), str(amount)))
            return self._policy_public(conn)

    @staticmethod
    def _gateway(value):
        from urllib.parse import urlsplit
        if not isinstance(value, str) or len(value) > 500:
            raise PointsError('网关地址无效')
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise PointsError('网关地址无效')
        return value.rstrip('/')

    def publish_price(self, data, admin_id=None):
        allowed = {'provider', 'gateway', 'model', 'rates', 'user_rates', 'adapter', 'cache_read_supported', 'cache_write_supported', 'effective_at', 'currency'}
        if not isinstance(data, dict) or set(data) - allowed:
            raise PointsError('价格字段无效', 422)
        if data.get('currency', 'CNY') != 'CNY':
            raise PointsError('首版仅支持人民币价格', 422)
        model, provider = data.get('model'), data.get('provider', 'openai_compatible')
        if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}', model):
            raise PointsError('计费模型无效', 422)
        if not isinstance(provider, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,59}', provider):
            raise PointsError('供应商无效', 422)
        gateway = self._gateway(data.get('gateway'))
        adapter = data.get('adapter', 'openai')
        if adapter not in ('openai', 'deepseek', 'anthropic'):
            raise PointsError('用量映射无效', 422)
        normalized = []
        for key in ('rates', 'user_rates'):
            rates = data.get(key, data.get('rates'))
            if not isinstance(rates, dict) or set(rates) != set(BUCKETS):
                raise PointsError('必须明确配置四类Token单价', 422)
            normalized.append({k: str(_decimal(rates[k], maximum='1000000')) for k in BUCKETS})
        read, write = data.get('cache_read_supported', adapter == 'deepseek'), data.get('cache_write_supported', False)
        if type(read) is not bool or type(write) is not bool:
            raise PointsError('缓存计费能力必须明确为布尔值', 422)
        effective = data.get('effective_at', self._now())
        if isinstance(effective, bool) or not isinstance(effective, (int, float)) or not self._now() - 1 <= effective <= self._now() + 366 * 86400:
            raise PointsError('价格生效时间无效', 422)
        identity = uuid.uuid4().hex
        with self._tx() as conn:
            conn.execute('INSERT INTO points_prices VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', (identity, provider, gateway, model, _json(normalized[0]), _json(normalized[1]), adapter, int(read), int(write), 'published', effective, self._now(), admin_id))
            return self._price_public(conn.execute('SELECT * FROM points_prices WHERE id=?', (identity,)).fetchone())

    def disable_price(self, price_id):
        with self._tx() as conn:
            if not conn.execute("UPDATE points_prices SET status='disabled' WHERE id=?", (price_id,)).rowcount:
                raise PointsError('价格版本不存在', 404)

    def prices(self, *, public=False):
        with self._connect() as conn:
            rows = conn.execute('SELECT * FROM points_prices ORDER BY created DESC,rowid DESC LIMIT 200').fetchall()
            return [self._price_public(r, public=public) for r in rows]

    @staticmethod
    def _price_public(row, *, public=False):
        data = {'id': row['id'], 'provider': row['provider'], 'gateway': row['gateway'], 'model': row['model'], 'currency': 'CNY', 'unit': 'million_tokens',
                'rates': json.loads(row['user_rates'] if public else row['rates']), 'user_rates': json.loads(row['user_rates']), 'adapter': row['adapter'],
                'cache_read_supported': bool(row['cache_read_supported']), 'cache_write_supported': bool(row['cache_write_supported']), 'status': row['status'], 'effective_at': row['effective']}
        return data

    @staticmethod
    def _cost(buckets, rates):
        return sum((Decimal(buckets[k]) * Decimal(rates[k]) / 1_000_000 for k in BUCKETS), Decimal(0))

    @staticmethod
    def _bound_cost(input_tokens, output_tokens, rates):
        # Cache creation can cost more than ordinary input. A conservative
        # bound cannot assume that either cache reads or writes are cheaper.
        input_rate = max(Decimal(rates[k]) for k in BUCKETS[:3])
        return (Decimal(input_tokens)*input_rate + Decimal(output_tokens)*Decimal(rates['billable_output'])) / 1_000_000

    def quote(self, actor, request_digest, *, model, gateway, provider='openai_compatible', input_tokens, output_tokens, max_calls=8, cash_extra_cny='0'):
        principal, digest = _identity(actor), _digest(request_digest)
        if isinstance(actor, dict) and actor.get('wallet_only'):
            raise PointsError('请在新的访客空间继续对话', 403, 'content_capability_required')
        input_tokens, output_tokens = _count(input_tokens, 2_000_000), _count(output_tokens, 100_000)
        max_calls = _count(max_calls, 32)
        if not max_calls:
            raise PointsError('模型调用上限必须大于零')
        gateway = self._gateway(gateway)
        extra = _decimal(cash_extra_cny, maximum=str(max_calls * 1000))
        with self._tx() as conn:
            self._wallet_ids(conn, actor)
            # Quotes authorize no spend yet. Retain only bounded live offers;
            # used snapshots stay attached to their immutable billed request.
            conn.execute('DELETE FROM points_quotes WHERE used_request IS NULL AND expires<=?', (self._now(),))
            pending = conn.execute('SELECT COUNT(*) FROM points_quotes WHERE principal=? AND used_request IS NULL AND expires>?', (principal,self._now())).fetchone()[0]
            if pending >= MAX_PENDING_QUOTES_PER_PRINCIPAL:
                raise PointsError('待确认报价过多，请使用现有报价或稍后重试',429,'quote_limit')
            if conn.execute('SELECT COUNT(*) FROM points_quotes WHERE used_request IS NULL AND expires>?',(self._now(),)).fetchone()[0] >= MAX_PENDING_QUOTES_TOTAL:
                raise PointsError('报价服务繁忙，请稍后重试',503,'quote_capacity')
            price = conn.execute("SELECT * FROM points_prices WHERE provider=? AND gateway=? AND model=? AND effective<=? ORDER BY effective DESC,created DESC,rowid DESC LIMIT 1", (provider, gateway, model, self._now())).fetchone()
            if not price or price['status'] != 'published':
                raise PointsError('当前模型尚未配置有效计费价格', 422, 'price_unavailable')
            cash = self._bound_cost(input_tokens*max_calls, output_tokens*max_calls, json.loads(price['rates'])) + extra
            maximum = cost_to_micro(self._bound_cost(input_tokens*max_calls, output_tokens*max_calls, json.loads(price['user_rates'])))
            identity = uuid.uuid4().hex
            conn.execute('INSERT INTO points_quotes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', (identity, principal, digest, price['id'], input_tokens, output_tokens, max_calls, maximum, str(cash), str(extra), self._now(), self._now() + 300, None))
            return {'quote_id': identity, 'max_points': points_string(maximum), 'max_micro': maximum, 'expires_at': self._now() + 300,
                    'input_tokens': input_tokens, 'output_tokens': output_tokens, 'max_calls': max_calls, 'price': self._price_public(price, public=True), 'request_digest': digest}

    def _cash_day(self, conn, day):
        row = conn.execute('SELECT limit_cny FROM points_cash_days WHERE day=?', (day,)).fetchone()
        if not row:
            policy = conn.execute("SELECT value FROM points_policy WHERE key='daily_cash_limit_cny'").fetchone()
            if not policy:
                raise PointsError('平台费用预算尚未配置', 503, 'budget_unavailable')
            conn.execute('INSERT INTO points_cash_days VALUES(?,?)', (day, policy[0]))
            return Decimal(policy[0])
        return Decimal(row[0])

    def _cash_used(self, conn, day):
        remaining = sum((Decimal(r[0]) for r in conn.execute('SELECT remaining FROM points_cash_commitments WHERE day=?', (day,))), Decimal(0))
        costs = sum((Decimal(r['cost'] if r['cost'] is not None else r['bound_cost']) for r in conn.execute('SELECT cost,bound_cost FROM points_calls WHERE day=?', (day,))), Decimal(0))
        return costs + remaining

    def reserve(self, actor, quote_id, client_request_id, request_digest):
        principal, digest, operation = _identity(actor), _digest(request_digest), _operation(client_request_id)
        if isinstance(actor, dict) and actor.get('wallet_only'):
            raise PointsError('请在新的访客空间继续对话', 403, 'content_capability_required')
        with self._tx() as conn:
            wallets = self._wallet_ids(conn, actor)
            old = conn.execute('SELECT * FROM points_requests WHERE principal=? AND client_key=?', (principal, operation)).fetchone()
            if old:
                if old['digest'] != digest:
                    raise PointsError('同一请求标识不能用于不同内容', 409, 'idempotency_conflict')
                return {**self._request_public(conn, old), 'duplicate': True}
            quote = conn.execute('SELECT q.*,p.status AS price_status FROM points_quotes q JOIN points_prices p ON p.id=q.price_id WHERE q.id=? AND q.principal=?', (quote_id, principal)).fetchone()
            if not quote or quote['digest'] != digest or quote['used_request'] or quote['expires'] <= self._now() or quote['price_status'] != 'published':
                raise PointsError('报价失效，请重新确认', 422, 'quote_invalid')
            self._expire(conn, wallets)
            self._recover_stale(conn,wallets)
            self._pending_deadlines(conn, wallets)
            self._daily(conn, actor, wallets)
            placeholders = ','.join('?' for _ in wallets)
            if conn.execute('SELECT 1 FROM points_requests WHERE wallet_id IN (' + placeholders + ") AND state IN ('reserved','running','usage_pending') LIMIT 1", wallets).fetchone():
                raise PointsError('已有生成或待结算请求，请稍后再试', 409, 'wallet_busy')
            lots = conn.execute('SELECT * FROM points_lots WHERE wallet_id IN (' + placeholders + ') AND available>0 AND expires>? ORDER BY expires,id', (*wallets, self._now())).fetchall()
            if sum(r['available'] for r in lots) < quote['max_micro']:
                raise PointsError('积分不足以覆盖本轮最高费用', 402, 'insufficient_points')
            cash_day = self._day()
            limit = self._cash_day(conn, cash_day)
            if self._cash_used(conn, cash_day) + Decimal(quote['cash_max']) > limit:
                raise PointsError('平台今日费用预算已满，请稍后再试', 503, 'cash_budget_exhausted')
            identity = uuid.uuid4().hex
            conn.execute('''INSERT INTO points_requests(id,principal,wallet_id,client_key,digest,quote_id,state,max_micro,created,updated)
              VALUES(?,?,?,?,?,?,'reserved',?,?,?)''', (identity, principal, wallets[0], operation, digest, quote_id, quote['max_micro'], self._now(), self._now()))
            left = quote['max_micro']
            for lot in lots:
                if not left:
                    break
                amount = min(left, lot['available']); left -= amount
                conn.execute('UPDATE points_lots SET available=available-?,held=held+? WHERE id=?', (amount, amount, lot['id']))
                conn.execute('INSERT INTO points_allocations VALUES(?,?,?,0)', (identity, lot['id'], amount))
                self._entry(conn, 'reserve:' + identity + ':' + str(lot['id']), lot['wallet_id'], lot['id'], 'reserve', amount, available=-amount, held=amount, request_id=identity)
            conn.execute('INSERT INTO points_cash_commitments VALUES(?,?,?)', (identity, cash_day, quote['cash_max']))
            conn.execute('UPDATE points_quotes SET used_request=? WHERE id=?', (identity, quote_id))
            row = conn.execute('SELECT * FROM points_requests WHERE id=?', (identity,)).fetchone()
            return {**self._request_public(conn, row), 'duplicate': False}

    def _request_and_quote(self, conn, request_id):
        req = conn.execute('SELECT * FROM points_requests WHERE id=?', (request_id,)).fetchone()
        if not req:
            raise PointsError('请求不存在', 404, 'request_missing')
        quote = conn.execute('SELECT q.*,p.provider,p.gateway,p.model,p.rates,p.user_rates,p.adapter,p.cache_read_supported,p.cache_write_supported,p.status AS price_status FROM points_quotes q JOIN points_prices p ON p.id=q.price_id WHERE q.id=?', (req['quote_id'],)).fetchone()
        return req, quote

    def _admit_call(self, conn, req, quote, call_id, bound, *, input_tokens=0, output_tokens=0, is_retry=False, kind='model'):
        _operation(call_id)
        existing = conn.execute('SELECT * FROM points_calls WHERE request_id=? AND id=?', (req['id'], call_id)).fetchone()
        if existing:
            raise PointsError('该模型调用已派发，不能重放', 409, 'call_already_started')
        if req['state'] not in ('reserved', 'running') or req['over_budget']:
            raise PointsError('请求已结束或处于费用保护状态', 409, 'request_not_running')
        if quote['price_status'] != 'published':
            raise PointsError('模型计费已暂停', 503, 'price_disabled')
        commitment = conn.execute('SELECT * FROM points_cash_commitments WHERE request_id=?', (req['id'],)).fetchone()
        remaining = Decimal(commitment['remaining'])
        if bound > remaining:
            raise PointsError('当前调用超过已授权费用预算', 402, 'call_budget_exhausted')
        day = self._day()
        limit = self._cash_day(conn, day)
        # Actual supplier usage can exceed an earlier conservative promise.
        # Recheck the shared daily projection before every dispatch, including
        # requests already reserved by another wallet. An exact full budget
        # may consume existing promises without counting them a second time.
        projected = self._cash_used(conn, day)
        if commitment['day'] != day:
            projected += remaining
        if projected > limit:
            raise PointsError('平台今日实际费用及承诺已超预算，本轮不能继续调用', 503, 'cash_budget_exhausted')
        if commitment['day'] != day:
            conn.execute('UPDATE points_cash_commitments SET day=? WHERE request_id=?', (day, req['id']))
        # The child's bound replaces part of the parent's remaining promise.
        conn.execute('UPDATE points_cash_commitments SET remaining=? WHERE request_id=?', (str(remaining - bound), req['id']))
        conn.execute('INSERT INTO points_calls(id,request_id,kind,day,bound_input,bound_output,bound_cost,state,billable,created,updated) VALUES(?,?,?,?,?,?,?,\'inflight\',?,?,?)', (call_id, req['id'], kind, day, input_tokens, output_tokens, str(bound), int(not is_retry and kind == 'model'), self._now(), self._now()))
        conn.execute("UPDATE points_requests SET state='running',updated=? WHERE id=?", (self._now(), req['id']))

    def before_call(self, request_id, call_id, input_tokens, output_tokens, *, is_retry=False, model=None, gateway=None):
        input_tokens, output_tokens = _count(input_tokens, 2_000_000), _count(output_tokens, 100_000)
        if type(is_retry) is not bool:
            raise PointsError('重试标志无效')
        with self._tx() as conn:
            req, quote = self._request_and_quote(conn, request_id)
            if (model is not None and model != quote['model']) or (gateway is not None and self._gateway(gateway) != quote['gateway']):
                raise PointsError('调用模型与授权报价不一致', 422, 'model_mismatch')
            calls = conn.execute("SELECT COUNT(*) FROM points_calls WHERE request_id=? AND kind='model'", (request_id,)).fetchone()[0]
            if calls >= quote['call_cap'] or input_tokens > quote['input_cap'] or output_tokens > quote['output_cap']:
                raise PointsError('调用超出本轮输入输出或阶段上限', 402, 'call_budget_exhausted')
            bound = self._bound_cost(input_tokens, output_tokens, json.loads(quote['rates']))
            self._admit_call(conn, req, quote, call_id, bound, input_tokens=input_tokens, output_tokens=output_tokens, is_retry=is_retry)
            return {'call_id': call_id, 'request_id': request_id, 'cost_bound_cny': str(bound)}

    def before_service_call(self, request_id, call_id, cost_bound_cny, *, service='firecrawl'):
        if service != 'firecrawl':
            raise PointsError('外部服务无效')
        bound = _decimal(cost_bound_cny, maximum='1000')
        if bound <= 0:
            raise PointsError('外部服务必须配置可核验的正数费用上界', 422, 'service_price_unavailable')
        with self._tx() as conn:
            req, quote = self._request_and_quote(conn, request_id)
            if conn.execute("SELECT COUNT(*) FROM points_calls WHERE request_id=? AND kind<>'model'", (request_id,)).fetchone()[0] >= 16:
                raise PointsError('外部服务调用次数超限', 429, 'service_call_limit')
            service_total = sum((Decimal(r[0]) for r in conn.execute("SELECT bound_cost FROM points_calls WHERE request_id=? AND kind<>'model'", (request_id,))), Decimal(0))
            if service_total + bound > Decimal(quote['extra_cash']):
                raise PointsError('搜索调用超出平台授权预算', 503, 'service_budget_exhausted')
            self._admit_call(conn, req, quote, call_id, bound, kind=service)

    def record_call(self, request_id, call_id, usage, *, provider_request_id=None, status='ok'):
        if status not in ('ok', 'error', 'cancelled'):
            raise PointsError('调用状态无效')
        with self._tx() as conn:
            req, quote = self._request_and_quote(conn, request_id)
            call = conn.execute('SELECT * FROM points_calls WHERE request_id=? AND id=?', (request_id, call_id)).fetchone()
            if not call or call['kind'] != 'model':
                raise PointsError('调用未获授权', 409, 'call_missing')
            buckets = normalize_usage(usage, quote['adapter'], cache_read_supported=bool(quote['cache_read_supported']), cache_write_supported=bool(quote['cache_write_supported']))
            provider_key = None
            if provider_request_id is not None:
                if not isinstance(provider_request_id, str) or not 1 <= len(provider_request_id) <= 200 or any(ord(c) < 32 or ord(c) == 127 for c in provider_request_id):
                    raise PointsError('供应商请求ID无效')
                provider_key = _hash(quote['provider'] + '\n' + quote['gateway'] + '\n' + provider_request_id)
                owner = conn.execute('SELECT request_id,id FROM points_calls WHERE provider_key=?', (provider_key,)).fetchone()
                if owner and (owner['request_id'], owner['id']) != (request_id, call_id):
                    raise PointsError('供应商调用已关联其他请求', 409, 'provider_call_duplicate')
                if call['provider_key'] and call['provider_key'] != provider_key:
                    raise PointsError('供应商调用标识不能覆盖',409,'provider_call_conflict')
            if call['state'] == 'known':
                if buckets is not None and _json(buckets) == call['usage']:
                    if provider_key and not call['provider_key']:
                        conn.execute('UPDATE points_calls SET provider_key=?,provider_request_id=? WHERE request_id=? AND id=?',(provider_key,provider_request_id,request_id,call_id))
                    elif provider_key and not call['provider_request_id']:
                        conn.execute('UPDATE points_calls SET provider_request_id=? WHERE request_id=? AND id=?',(provider_request_id,request_id,call_id))
                    return {'complete': True, 'cost_cny': call['cost'], 'duplicate': True}
                raise PointsError('用量已结算，不能覆盖历史', 409, 'usage_conflict')
            if buckets is None:
                conn.execute("UPDATE points_calls SET state='unknown',provider_key=COALESCE(provider_key,?),provider_request_id=COALESCE(provider_request_id,?),updated=? WHERE request_id=? AND id=?", (provider_key, provider_request_id, self._now(), request_id, call_id))
                # Repeated missing usage must stop billing rather than produce
                # unlimited free generations. A new verified price re-enables.
                recent = conn.execute("SELECT c.state FROM points_calls c JOIN points_requests r ON r.id=c.request_id JOIN points_quotes q ON q.id=r.quote_id WHERE q.price_id=? AND c.kind='model' ORDER BY c.created DESC LIMIT 3", (quote['price_id'],)).fetchall()
                if len(recent) == 3 and all(r[0] == 'unknown' for r in recent):
                    conn.execute("UPDATE points_prices SET status='disabled' WHERE id=?", (quote['price_id'],))
                return {'complete': False, 'cost_cny': None}
            cost = self._cost(buckets, json.loads(quote['rates']))
            user_cost = self._cost(buckets, json.loads(quote['user_rates'])) if call['billable'] else Decimal(0)
            conn.execute("UPDATE points_calls SET state='known',provider_key=COALESCE(provider_key,?),provider_request_id=COALESCE(provider_request_id,?),usage=?,cost=?,user_cost=?,updated=? WHERE request_id=? AND id=?", (provider_key, provider_request_id, _json(buckets), str(cost), str(user_cost), self._now(), request_id, call_id))
            # Only actual liability plus unused promise remains reserved.
            commitment = conn.execute('SELECT * FROM points_cash_commitments WHERE request_id=?', (request_id,)).fetchone()
            delta = Decimal(call['bound_cost']) - cost
            if req['state'] not in FINAL and commitment and commitment['day'] == call['day']:
                conn.execute('UPDATE points_cash_commitments SET remaining=? WHERE request_id=?', (str(max(Decimal(0), Decimal(commitment['remaining']) + delta)), request_id))
            if (cost > Decimal(call['bound_cost']) or
                    sum(buckets[k] for k in BUCKETS[:3]) > call['bound_input'] or
                    buckets['billable_output'] > call['bound_output']):
                conn.execute('UPDATE points_requests SET over_budget=1 WHERE id=?', (request_id,))
            if req['state'] == 'usage_pending':
                self._settle(conn, request_id, delivered=bool(req['delivered']), cancelled=bool(req['cancelled']), failure=bool(req['failure']))
            elif req['state'] in FINAL:
                self._refresh_final_cost(conn, request_id)
            return {'complete': True, 'cost_cny': str(cost), 'usage': buckets}

    def _refresh_final_cost(self, conn, request_id):
        req = conn.execute('SELECT * FROM points_requests WHERE id=?', (request_id,)).fetchone()
        calls = conn.execute("SELECT * FROM points_calls WHERE request_id=? AND kind='model'", (request_id,)).fetchall()
        if any(c['cost'] is None or c['user_cost'] is None for c in calls):
            return
        supplier = sum((Decimal(c['cost']) for c in calls), Decimal(0))
        user_priced = sum((Decimal(c['user_cost']) for c in calls), Decimal(0))
        normal = sum((Decimal(c['cost']) for c in calls if c['billable']), Decimal(0))
        attributed = min(normal, Decimal(req['charged_micro'])/(1000*MICRO))
        conn.execute('UPDATE points_requests SET supplier_cost=?,user_cost=?,platform_cost=?,updated=? WHERE id=?', (str(supplier), str(user_priced), str(supplier-attributed), self._now(), request_id))

    def record_service_call(self, request_id, call_id, actual_cost_cny=None):
        with self._tx() as conn:
            call = conn.execute("SELECT * FROM points_calls WHERE request_id=? AND id=? AND kind<>'model'", (request_id, call_id)).fetchone()
            if not call:
                raise PointsError('服务调用未获授权', 409)
            if call['state'] == 'known':
                if actual_cost_cny is not None and Decimal(call['cost']) != _decimal(actual_cost_cny):
                    raise PointsError('服务费用不能覆盖', 409)
                return
            if actual_cost_cny is None:
                conn.execute("UPDATE points_calls SET state='unknown',updated=? WHERE request_id=? AND id=?", (self._now(), request_id, call_id))
                return
            cost = _decimal(actual_cost_cny)
            req, _ = self._request_and_quote(conn, request_id)
            conn.execute("UPDATE points_calls SET state='known',cost=?,user_cost='0',updated=? WHERE request_id=? AND id=?", (str(cost), self._now(), request_id, call_id))
            commitment = conn.execute('SELECT * FROM points_cash_commitments WHERE request_id=?', (request_id,)).fetchone()
            if req['state'] not in FINAL and commitment['day'] == call['day']:
                delta = Decimal(call['bound_cost']) - cost
                conn.execute('UPDATE points_cash_commitments SET remaining=? WHERE request_id=?', (str(max(Decimal(0), Decimal(commitment['remaining']) + delta)), request_id))
            if cost > Decimal(call['bound_cost']):
                conn.execute('UPDATE points_requests SET over_budget=1 WHERE id=?', (request_id,))

    def _release_allocations(self, conn, req, charge):
        left = charge
        for allocation in conn.execute('SELECT a.*,l.wallet_id,l.expires FROM points_allocations a JOIN points_lots l ON l.id=a.lot_id WHERE a.request_id=? ORDER BY l.expires,l.id', (req['id'],)).fetchall():
            spent = min(left, allocation['reserved']); left -= spent
            released = allocation['reserved'] - spent
            expired = released if allocation['expires'] <= self._now() else 0
            available = released - expired
            conn.execute('UPDATE points_lots SET held=held-?,spent=spent+?,available=available+?,expired=expired+? WHERE id=?', (allocation['reserved'], spent, available, expired, allocation['lot_id']))
            conn.execute('UPDATE points_allocations SET charged=? WHERE request_id=? AND lot_id=?', (spent, req['id'], allocation['lot_id']))
            if spent:
                self._entry(conn, 'settle:' + req['id'] + ':' + str(allocation['lot_id']), allocation['wallet_id'], allocation['lot_id'], 'settle', spent, held=-spent, spent=spent, request_id=req['id'])
            if released:
                self._entry(conn, 'release:' + req['id'] + ':' + str(allocation['lot_id']), allocation['wallet_id'], allocation['lot_id'], 'release_expired' if expired else 'release', released, available=available, held=-released, expired=expired, request_id=req['id'])
        if left:
            raise RuntimeError('Credit allocation invariant failed')

    def _settle(self, conn, request_id, *, delivered=True, cancelled=False, failure=False):
        req, _ = self._request_and_quote(conn, request_id)
        if req['state'] in FINAL:
            return self._request_public(conn, req)
        calls = conn.execute("SELECT * FROM points_calls WHERE request_id=? AND kind='model'", (request_id,)).fetchall()
        incomplete = any(r['state'] != 'known' for r in calls)
        if incomplete and not failure:
            conn.execute("UPDATE points_requests SET state='usage_pending',delivered=?,cancelled=?,failure=?,updated=? WHERE id=?", (int(delivered), int(cancelled), int(failure), self._now(), request_id))
            return self._request_public(conn, conn.execute('SELECT * FROM points_requests WHERE id=?', (request_id,)).fetchone())
        cost = sum((Decimal(r['cost']) for r in calls if r['cost'] is not None), Decimal(0))
        user_cost = sum((Decimal(r['user_cost']) for r in calls if r['user_cost'] is not None), Decimal(0))
        charge = 0 if failure or (not delivered and not cancelled) else min(req['max_micro'], cost_to_micro(user_cost))
        self._release_allocations(conn, req, charge)
        # Attribution is distinct from the rounded credit charge and price
        # margin. First version grants are still entirely platform-funded.
        normal_cost = sum((Decimal(r['cost']) for r in calls if r['cost'] is not None and r['billable']), Decimal(0))
        attributed = min(normal_cost, Decimal(charge) / (1000 * MICRO)) if charge else Decimal(0)
        state = 'platform_absorbed' if failure else 'settled'
        conn.execute('UPDATE points_requests SET state=?,charged_micro=?,supplier_cost=?,user_cost=?,platform_cost=?,delivered=?,cancelled=?,failure=?,updated=? WHERE id=?', (state, charge, None if incomplete else str(cost), str(user_cost), str(cost-attributed), int(delivered), int(cancelled), int(failure), self._now(), request_id))
        conn.execute("UPDATE points_cash_commitments SET remaining='0' WHERE request_id=?", (request_id,))
        return self._request_public(conn, conn.execute('SELECT * FROM points_requests WHERE id=?', (request_id,)).fetchone())

    def settle(self, request_id, *, delivered=True, cancelled=False, failure=False):
        if any(type(x) is not bool for x in (delivered, cancelled, failure)):
            raise PointsError('结算状态无效')
        with self._tx() as conn:
            return self._settle(conn, request_id, delivered=delivered, cancelled=cancelled, failure=failure)

    def heartbeat(self, request_id):
        """Extend a durable worker lease when a bounded provider call is alive."""
        with self._tx() as conn:
            changed=conn.execute("UPDATE points_requests SET updated=? WHERE id=? AND state IN ('reserved','running')",(self._now(),request_id)).rowcount
            if not changed:
                raise PointsError('生成请求已失效',409,'request_not_running')

    def _recover_stale(self, conn, wallets=None, limit=500):
        sql="SELECT * FROM points_requests WHERE state IN ('reserved','running') AND updated<?"
        args=[self._now()-self.request_lease_seconds]
        if wallets is not None:
            sql+=' AND wallet_id IN ('+','.join('?' for _ in wallets)+')';args+=wallets
        sql+=' ORDER BY updated LIMIT ?';args.append(limit)
        count=0
        for req in conn.execute(sql,args).fetchall():
            unknown=conn.execute("SELECT 1 FROM points_calls WHERE request_id=? AND state<>'known' LIMIT 1",(req['id'],)).fetchone()
            if unknown:
                # A crashed worker cannot establish delivery. User credit is
                # never charged solely because a lease expired; unknown cash
                # obligations remain held for supplier reconciliation.
                conn.execute("UPDATE points_requests SET state='usage_pending',failure=1,delivered=0,updated=? WHERE id=?",(self._now(),req['id']))
            else:
                self._settle(conn,req['id'],delivered=False,failure=True)
            count+=1
        return count

    def recover_stale_requests(self, *, limit=500):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise PointsError('恢复批次无效')
        with self._tx() as conn:
            count=self._recover_stale(conn,limit=limit)
            self._pending_deadlines(conn,limit=limit)
            return {'recovered_requests':count}

    def _pending_deadlines(self, conn, wallets=None, limit=500):
        sql = "SELECT * FROM points_requests WHERE state='usage_pending' AND created<=?"
        args = [self._now()-self.pending_seconds]
        if wallets is not None:
            sql += ' AND wallet_id IN (' + ','.join('?' for _ in wallets) + ')'; args += wallets
        sql += ' ORDER BY created LIMIT ?'; args.append(limit)
        for req in conn.execute(sql, args).fetchall():
            self._release_allocations(conn, req, 0)
            conn.execute("UPDATE points_requests SET state='usage_released',charged_micro=0,updated=? WHERE id=?", (self._now(), req['id']))
            conn.execute("UPDATE points_cash_commitments SET remaining='0' WHERE request_id=?", (req['id'],))

    def _request_public(self, conn, req):
        calls = conn.execute('SELECT * FROM points_calls WHERE request_id=? ORDER BY created,id', (req['id'],)).fetchall()
        return {'request_id': req['id'], 'state': req['state'], 'status': req['state'], 'max_points': points_string(req['max_micro']),
                'charged_points': points_string(req['charged_micro']), 'charged_micro': req['charged_micro'],
                'supplier_cost_cny': req['supplier_cost'], 'user_cost_cny': req['user_cost'], 'platform_cost_cny': req['platform_cost'],
                'created_at': req['created'], 'updated_at': req['updated'], 'delivered': bool(req['delivered']), 'over_budget': bool(req['over_budget']),
                'calls': [{'call_id': r['id'], 'kind': r['kind'], 'state': r['state'], 'usage': json.loads(r['usage']) if r['usage'] else None,
                           'cost_cny': r['cost'], 'user_cost_cny': r['user_cost'], 'billable': bool(r['billable'])} for r in calls]}

    def request_status(self, actor, request_id):
        with self._tx() as conn:
            wallets = self._wallet_ids(conn, actor)
            self._recover_stale(conn,wallets)
            self._pending_deadlines(conn, wallets)
            req = conn.execute('SELECT * FROM points_requests WHERE id=? AND wallet_id IN (' + ','.join('?' for _ in wallets) + ')', (request_id, *wallets)).fetchone()
            if not req:
                raise PointsError('请求不存在', 404, 'request_missing')
            return self._request_public(conn, req)

    def ledger(self, actor, *, limit=50, cursor=0):
        if type(limit) is not int or not 1 <= limit <= 200 or type(cursor) is not int or not 0 <= cursor <= 10**15:
            raise PointsError('分页参数无效')
        with self._tx() as conn:
            wallets = self._wallet_ids(conn, actor)
            self._expire(conn, wallets)
            rows = conn.execute('SELECT * FROM points_ledger WHERE wallet_id IN (' + ','.join('?' for _ in wallets) + ') AND id>? ORDER BY id LIMIT ?', (*wallets, cursor, limit+1)).fetchall()
            result = [{'id': r['id'], 'event': r['event'], 'points': points_string(r['amount']), 'lot_id': r['lot_id'], 'request_id': r['request_id'], 'created_at': r['created'], 'metadata': json.loads(r['metadata'])} for r in rows[:limit]]
            return {'items': result, 'next_cursor': result[-1]['id'] if len(rows)>limit else None}

    def _operation_result(self, conn, key, digest):
        row = conn.execute('SELECT * FROM points_operations WHERE key=?', (key,)).fetchone()
        if row:
            if row['digest'] != digest:
                raise PointsError('同一操作标识不能用于不同参数', 409, 'idempotency_conflict')
            return json.loads(row['result'])
        return None

    def _save_operation(self, conn, key, digest, result):
        conn.execute('INSERT INTO points_operations VALUES(?,?,?,?)', (key, digest, _json(result), self._now()))

    def _claim_wallet(self, conn, guest_actor, user_actor, key):
        guest, user = _identity(guest_actor), _identity(user_actor)
        if not guest.startswith('g:') or not user.startswith('u:'):
            raise PointsError('访客归入身份无效')
        self._ensure_user(conn, user)
        if isinstance(guest_actor, dict) and guest_actor.get('wallet_only'):
            wallet_id = self._wallet_ids(conn, guest_actor)[0]
            row = conn.execute('SELECT id AS wallet_id,claimed_to FROM points_wallets WHERE id=?', (wallet_id,)).fetchone()
        else:
            row = conn.execute('SELECT g.wallet_id,w.claimed_to FROM points_guests g JOIN points_wallets w ON w.id=g.wallet_id WHERE g.principal=?', (guest,)).fetchone()
        if not row:
            raise PointsError('访客空间不存在', 404)
        if row['claimed_to'] and row['claimed_to'] != user:
            raise PointsError('访客余额已归入其他账户', 409, 'claim_conflict')
        used = conn.execute('SELECT claim_used FROM points_principals WHERE principal=?', (user,)).fetchone()[0]
        claimed = not used and not row['claimed_to']
        if claimed:
            conn.execute('UPDATE points_wallets SET owner=?,claimed_to=? WHERE id=?', (user, user, row['wallet_id']))
            conn.execute('UPDATE points_principals SET claim_used=1 WHERE principal=?', (user,))
            self._entry(conn, key, row['wallet_id'], None, 'transfer', metadata={'to':user})
        return {'wallet_claimed':claimed or row['claimed_to']==user, 'already_claimed':bool(row['claimed_to']), 'claim_used':bool(used), 'source_storage_id':guest_actor.get('storage_id') if isinstance(guest_actor,dict) else None}

    def claim_guest(self, guest_actor, user_actor, operation_id):
        guest, user = _identity(guest_actor), _identity(user_actor)
        key = 'claim-wallet:' + _operation(operation_id)
        digest = _hash(_json([guest,user]))
        with self._tx() as conn:
            old = self._operation_result(conn, key, digest)
            if old is not None:
                return old
            result = self._claim_wallet(conn, guest_actor, user_actor, key)
            self._save_operation(conn, key, digest, result)
            return result

    def claim_content(self, guest_actor, user_actor, operation_id):
        guest, user = _identity(guest_actor), _identity(user_actor)
        if not guest.startswith('g:') or not user.startswith('u:') or (isinstance(guest_actor,dict) and guest_actor.get('wallet_only')):
            raise PointsError('内容归入身份无效')
        key = 'claim-content:' + _operation(operation_id)
        digest = _hash(_json([guest,user]))
        with self._tx() as conn:
            old = self._operation_result(conn, key, digest)
            if old is not None:
                return old
            row = conn.execute('SELECT * FROM points_guests WHERE principal=?', (guest,)).fetchone()
            if not row:
                raise PointsError('访客空间不存在', 404)
            if row['content_claimed_to'] and row['content_claimed_to'] != user:
                raise PointsError('访客空间已归入其他账户', 409, 'claim_conflict')
            conn.execute('UPDATE points_guests SET content_claimed_to=? WHERE principal=?', (user, guest))
            result = {'content_claimed': True, 'source_storage_id': -row['id'], 'target_storage_id': int(user[2:]), 'already_claimed': bool(row['content_claimed_to'])}
            self._save_operation(conn, key, digest, result)
            return result

    def claim_both(self, guest_actor, user_actor, operation_id):
        """Atomically commit independent wallet and content claims.

        The HTTP layer can first copy recoverable content files under its
        resource lock, then publish this commit; old content cookies cease
        authenticating immediately at this commit.
        """
        guest, user = _identity(guest_actor), _identity(user_actor)
        if not guest.startswith('g:') or not user.startswith('u:') or (isinstance(guest_actor,dict) and guest_actor.get('wallet_only')):
            raise PointsError('归入身份无效')
        key = 'claim-both:' + _operation(operation_id)
        digest = _hash(_json([guest,user]))
        with self._tx() as conn:
            old = self._operation_result(conn,key,digest)
            if old is not None:
                return old
            row = conn.execute('SELECT * FROM points_guests WHERE principal=?',(guest,)).fetchone()
            if not row or (row['content_claimed_to'] and row['content_claimed_to']!=user):
                raise PointsError('访客空间不可归入该账户',409,'claim_conflict')
            result = self._claim_wallet(conn,guest_actor,user_actor,key+':wallet')
            conn.execute('UPDATE points_guests SET content_claimed_to=? WHERE principal=?',(user,guest))
            result.update(content_claimed=True,source_storage_id=-row['id'],target_storage_id=int(user[2:]))
            self._save_operation(conn,key,digest,result)
            return result

    def content_owner(self, storage_id):
        if type(storage_id) is not int:
            raise PointsError('空间身份无效')
        if storage_id >= 0:
            return storage_id
        with self._connect() as conn:
            row = conn.execute('SELECT content_claimed_to FROM points_guests WHERE id=?', (-storage_id,)).fetchone()
            return int(row[0][2:]) if row and row[0] else storage_id

    def refund(self, request_id, operation_id, reason, admin_id=None):
        if not isinstance(reason, str) or not 3 <= len(reason.strip()) <= 200:
            raise PointsError('补偿必须填写原因')
        key = 'refund:' + _operation(operation_id)
        digest = _hash(_json([request_id,reason,admin_id]))
        with self._tx() as conn:
            old = self._operation_result(conn, key, digest)
            if old is not None:
                return old
            req, _ = self._request_and_quote(conn, request_id)
            if req['state'] not in FINAL:
                raise PointsError('请求尚未终结', 409)
            prior = conn.execute("SELECT 1 FROM points_ledger WHERE request_id=? AND event='reversal' LIMIT 1", (request_id,)).fetchone()
            if prior:
                raise PointsError('本轮已退款', 409, 'refund_duplicate')
            for allocation in conn.execute('SELECT a.*,l.wallet_id,l.expires FROM points_allocations a JOIN points_lots l ON l.id=a.lot_id WHERE a.request_id=? AND a.charged>0', (request_id,)).fetchall():
                amount = allocation['charged']
                expired = amount if allocation['expires'] <= self._now() else 0
                conn.execute('UPDATE points_lots SET spent=spent-?,available=available+?,expired=expired+? WHERE id=?', (amount, amount-expired, expired, allocation['lot_id']))
                original = conn.execute("SELECT id FROM points_ledger WHERE request_id=? AND lot_id=? AND event='settle'", (request_id, allocation['lot_id'])).fetchone()[0]
                self._entry(conn, key+':'+str(allocation['lot_id']), allocation['wallet_id'], allocation['lot_id'], 'reversal', amount, available=amount-expired, spent=-amount, expired=expired, request_id=request_id, original_id=original, metadata={'reason':reason.strip(),'admin_id':admin_id})
            result = {'request_id': request_id, 'refunded_points': points_string(req['charged_micro'])}
            self._save_operation(conn, key, digest, result)
            if req['supplier_cost'] is not None:
                conn.execute('UPDATE points_requests SET platform_cost=?,updated=? WHERE id=?', (req['supplier_cost'],self._now(),request_id))
            return result

    def adjust(self, actor, points, operation_id, reason, *, admin_id, expires_at=None, reviewed_by=None):
        """Audited operations adjustment; never overwrites a wallet balance.

        The HTTP layer must separately require current administrator MFA.
        Changes larger than 100 points need a distinct reviewing admin.
        """
        principal=_identity(actor)
        if type(admin_id) is not int or admin_id <= 0:
            raise PointsError('需要管理员身份',403)
        if not isinstance(reason,str) or not 3 <= len(reason.strip()) <= 200:
            raise PointsError('调整必须填写原因')
        if isinstance(points,(bool,float)):
            raise PointsError('积分必须使用十进制字符串')
        try:
            decimal=Decimal(str(points))
        except (InvalidOperation,ValueError,TypeError):
            raise PointsError('积分格式无效') from None
        if not decimal.is_finite() or not Decimal('-100000') <= decimal <= Decimal('100000'):
            raise PointsError('调整积分范围无效')
        micro=decimal*MICRO
        if micro != micro.to_integral_value() or not micro:
            raise PointsError('积分需精确到六位小数且不能为零')
        micro=int(micro)
        if abs(micro)>TRIAL and (type(reviewed_by) is not int or reviewed_by<=0 or reviewed_by==admin_id):
            raise PointsError('超过100积分的调整需要另一名管理员复核',403,'review_required')
        if micro>0:
            if isinstance(expires_at,bool) or not isinstance(expires_at,(int,float)) or not self._now()<expires_at<=self._now()+366*86400:
                raise PointsError('赠送调整必须明确有效期')
        key='adjust:'+_operation(operation_id)
        digest=_hash(_json([principal,str(decimal),reason.strip(),admin_id,expires_at,reviewed_by]))
        with self._tx() as conn:
            old=self._operation_result(conn,key,digest)
            if old is not None:
                return old
            wallets=self._wallet_ids(conn,actor)
            self._expire(conn,wallets)
            metadata={'reason':reason.strip(),'admin_id':admin_id,'reviewed_by':reviewed_by}
            if micro>0:
                lot=self._grant(conn,wallets[0],key,'adjustment',micro,expires_at)
                self._entry(conn,key+':audit',wallets[0],lot,'adjustment_audit',metadata=metadata)
            else:
                rows=conn.execute('SELECT * FROM points_lots WHERE wallet_id IN ('+','.join('?' for _ in wallets)+') AND available>0 AND expires>? ORDER BY expires,id',(*wallets,self._now())).fetchall()
                if sum(r['available'] for r in rows)<-micro:
                    raise PointsError('可用积分不足，不能扣成负数或扣除冻结',402,'insufficient_points')
                left=-micro
                for lot in rows:
                    if not left:break
                    amount=min(left,lot['available']);left-=amount
                    conn.execute('UPDATE points_lots SET available=available-?,spent=spent+? WHERE id=?',(amount,amount,lot['id']))
                    self._entry(conn,key+':'+str(lot['id']),lot['wallet_id'],lot['id'],'adjustment_debit',amount,available=-amount,spent=amount,metadata=metadata)
            result={'principal':principal,'adjustment_points':points_string(micro),'operation_id':operation_id}
            self._save_operation(conn,key,digest,result)
            return result

    def summary(self):
        with self._tx() as conn:
            day = self._day()
            row = conn.execute("SELECT value FROM points_policy WHERE key='daily_cash_limit_cny'").fetchone()
            limit = self._cash_day(conn, day) if row else None
            used = self._cash_used(conn, day)
            # Finance summaries are deliberately bounded to a recent window;
            # ledger exports use cursors rather than loading all history.
            cutoff = self._now() - 30*86400
            calls = conn.execute('SELECT * FROM points_calls WHERE created>=?', (cutoff,)).fetchall()
            requests = conn.execute('SELECT * FROM points_requests WHERE created>=?', (cutoff,)).fetchall()
            supplier = sum((Decimal(r['cost']) for r in calls if r['cost'] is not None), Decimal(0))
            user_cost = sum((Decimal(r['user_cost']) for r in requests if r['user_cost'] is not None), Decimal(0))
            charged = sum(r['charged_micro'] for r in requests)
            refunded = conn.execute("SELECT COALESCE(SUM(amount),0) FROM points_ledger WHERE event='reversal' AND created>=?", (cutoff,)).fetchone()[0]
            model_usage = {}
            for r in conn.execute('''SELECT p.model,p.gateway,p.id AS price_id,c.* FROM points_calls c
              JOIN points_requests r ON r.id=c.request_id JOIN points_quotes q ON q.id=r.quote_id
              JOIN points_prices p ON p.id=q.price_id WHERE c.created>=? AND c.kind='model' ''', (cutoff,)):
                key = r['price_id']
                group = model_usage.setdefault(key, {'model':r['model'],'gateway':r['gateway'],'price_id':key,'calls':0,'unknown_calls':0,
                    'supplier_cost_cny':Decimal(0),'input_uncached':0,'cache_read':0,'cache_write':0,'billable_output':0})
                group['calls'] += 1
                group['unknown_calls'] += int(r['state'] != 'known')
                if r['cost'] is not None:
                    group['supplier_cost_cny'] += Decimal(r['cost'])
                if r['usage']:
                    for bucket, value in json.loads(r['usage']).items():
                        group[bucket] += value
            for group in model_usage.values():
                group['supplier_cost_cny'] = str(group['supplier_cost_cny'])
            return {'budget_day': day, 'daily_cash_limit_cny': str(limit) if limit is not None else None,
                    'cash_committed_cny': str(used), 'budget_warning': bool(limit and used >= limit*Decimal('.8')),
                    'budget_exhausted': bool(limit is not None and used >= limit), 'supplier_cost_cny': str(supplier),
                    'user_priced_cost_cny': str(user_cost), 'charged_points': points_string(charged), 'refunded_points':points_string(refunded),
                    'net_charged_points':points_string(charged-refunded), 'cash_revenue_cny':'0', 'window_days':30,
                    'calls': len(calls), 'unknown_calls': sum(r['state'] != 'known' for r in calls),
                    'requests': len(requests), 'pending_requests': sum(r['state']=='usage_pending' for r in requests),
                    'model_usage': list(model_usage.values())}

    def cleanup(self, *, limit=500):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise PointsError('清理批次无效')
        with self._tx() as conn:
            self._expire(conn, limit=limit)
            recovered=self._recover_stale(conn,limit=limit)
            self._pending_deadlines(conn, limit=limit)
            cutoff=self._now()-180*86400
            pruned_requests=pruned_lots=0
            # A paid call with unknown supplier liability is not complete for
            # retention purposes, even after user holds have been released.
            candidates=conn.execute("""SELECT r.id,r.quote_id FROM points_requests r
              WHERE r.state IN ('settled','platform_absorbed','usage_released') AND r.updated<?
              AND NOT EXISTS(SELECT 1 FROM points_calls c WHERE c.request_id=r.id AND c.state<>'known')
              AND NOT EXISTS(SELECT 1 FROM points_allocations a JOIN points_lots l ON l.id=a.lot_id
                WHERE a.request_id=r.id AND (l.expires>=? OR l.held>0)) ORDER BY r.updated LIMIT ?""",(cutoff,cutoff,limit)).fetchall()
            for row in candidates:
                conn.execute('DELETE FROM points_calls WHERE request_id=?',(row['id'],))
                conn.execute('DELETE FROM points_allocations WHERE request_id=?',(row['id'],))
                conn.execute('DELETE FROM points_cash_commitments WHERE request_id=?',(row['id'],))
                conn.execute('DELETE FROM points_requests WHERE id=?',(row['id'],))
                conn.execute('DELETE FROM points_quotes WHERE id=?',(row['quote_id'],))
                pruned_requests+=1
            old_lots=conn.execute('''SELECT id FROM points_lots l WHERE l.expires<? AND l.held=0
              AND NOT EXISTS(SELECT 1 FROM points_allocations a WHERE a.lot_id=l.id) ORDER BY l.expires LIMIT ?''',(cutoff,limit)).fetchall()
            for row in old_lots:
                conn.execute('DELETE FROM points_ledger WHERE lot_id=?',(row['id'],))
                conn.execute('DELETE FROM points_lots WHERE id=?',(row['id'],))
                pruned_lots+=1
            conn.execute('DELETE FROM points_operations WHERE key IN (SELECT key FROM points_operations WHERE created<? ORDER BY created LIMIT ?)',(cutoff,limit))
            quotes = conn.execute('DELETE FROM points_quotes WHERE id IN (SELECT id FROM points_quotes WHERE used_request IS NULL AND expires<=? ORDER BY expires LIMIT ?)', (self._now(), limit)).rowcount
            stale=[-r[0] for r in conn.execute('SELECT id FROM points_guests WHERE last_active<? AND content_claimed_to IS NULL ORDER BY last_active LIMIT ?',(self._now()-self.trial_seconds,limit))]
            return {'expired_quotes_deleted':quotes,'financial_requests_pruned':pruned_requests,'expired_lots_pruned':pruned_lots,'recovered_requests':recovered,
                    'stale_guest_storage_ids':stale}

    def rebuild_projection(self, *, admin_id):
        """Rebuild lot projections from validated postings during maintenance.

        BillingAdminStore supplies the administrator journal and idempotency.
        This method changes no source posting, grant, expiry, or request total.
        """
        if type(admin_id) is not int or not 1 <= admin_id < 10**18:
            raise PointsError('需要管理员身份', 403, 'admin_required')
        def invalid():
            raise PointsError('账本来源或请求分配不一致，未重建余额，请人工核对', 409, 'projection_source_invalid')
        with self._tx() as conn:
            if conn.execute("SELECT 1 FROM points_requests WHERE state IN ('reserved','running','usage_pending') LIMIT 1").fetchone():
                raise PointsError('仍有生成或待结算请求，请全部结束后再重建', 409, 'projection_busy')
            if conn.execute("SELECT 1 FROM points_requests WHERE state NOT IN ('settled','platform_absorbed','usage_released') LIMIT 1").fetchone():
                invalid()
            if conn.execute('''SELECT 1 FROM points_allocations a LEFT JOIN points_requests r ON r.id=a.request_id
              LEFT JOIN points_lots l ON l.id=a.lot_id WHERE r.id IS NULL OR l.id IS NULL LIMIT 1''').fetchone():
                invalid()
            # SQLite owns the temporary projection, keeping memory bounded as
            # the journal is streamed in lot/id order under one write lock.
            conn.execute('DROP TABLE IF EXISTS temp.points_projection_rebuild')
            conn.execute('CREATE TEMP TABLE points_projection_rebuild(lot_id INTEGER PRIMARY KEY,available INTEGER,held INTEGER,spent INTEGER,expired INTEGER)')
            ledger_entries, checked_lots = 0, 0
            current, vector, grant_count = None, [0, 0, 0, 0], 0
            def finish():
                nonlocal checked_lots
                if current is None:
                    return
                if grant_count != 1 or sum(vector) != current['lot_amount'] or vector[1] != 0:
                    invalid()
                conn.execute('INSERT INTO points_projection_rebuild VALUES(?,?,?,?,?)', (current['lot_id'], *vector))
                checked_lots += 1
            for entry in conn.execute('''SELECT e.*,l.amount AS lot_amount,l.wallet_id AS lot_wallet,l.grant_key,w.id AS wallet_exists
              FROM points_ledger e LEFT JOIN points_lots l ON l.id=e.lot_id
              LEFT JOIN points_wallets w ON w.id=e.wallet_id ORDER BY e.lot_id,e.id'''):
                ledger_entries += 1
                values = tuple(entry[key] for key in ('delta_available','delta_held','delta_spent','delta_expired'))
                amount, event = entry['amount'], entry['event']
                if type(amount) is not int or amount < 0 or any(type(v) is not int for v in values) or entry['wallet_exists'] is None:
                    invalid()
                if entry['lot_id'] is None:
                    if event != 'transfer' or amount != 0 or any(values) or entry['original_id'] is not None:
                        invalid()
                    continue
                if entry['lot_amount'] is None or type(entry['lot_amount']) is not int or entry['lot_amount'] < 0 or entry['wallet_id'] != entry['lot_wallet']:
                    invalid()
                if current is None or current['lot_id'] != entry['lot_id']:
                    finish()
                    current, vector, grant_count = entry, [0, 0, 0, 0], 0
                expected = {
                    'grant': (amount,0,0,0), 'reserve': (-amount,amount,0,0),
                    'settle': (0,-amount,amount,0), 'release': (amount,-amount,0,0),
                    'release_expired': (0,-amount,0,amount), 'expire': (-amount,0,0,amount),
                    'adjustment_debit': (-amount,0,amount,0), 'adjustment_audit': (0,0,0,0),
                }
                if event == 'reversal':
                    original = conn.execute('SELECT * FROM points_ledger WHERE id=?', (entry['original_id'],)).fetchone()
                    if (not original or original['event'] != 'settle' or original['lot_id'] != entry['lot_id']
                            or original['request_id'] != entry['request_id'] or original['id'] >= entry['id']
                            or values[1] != 0 or values[2] != -amount or values[0] < 0 or values[3] < 0
                            or values[0]+values[3] != amount or amount <= 0):
                        invalid()
                elif event not in expected or values != expected[event] or entry['original_id'] is not None:
                    invalid()
                if event == 'grant':
                    grant_count += 1
                    if grant_count != 1 or any(vector) or amount != entry['lot_amount'] or entry['operation_key'] != entry['grant_key'] or entry['request_id'] is not None:
                        invalid()
                elif grant_count != 1:
                    invalid()
                if event in {'reserve','settle','release','release_expired','reversal'} and (not entry['request_id'] or amount <= 0):
                    invalid()
                if event == 'adjustment_audit' and amount != 0:
                    invalid()
                vector = [a+b for a,b in zip(vector,values)]
                if min(vector) < 0 or sum(vector) != entry['lot_amount']:
                    invalid()
            finish()
            if conn.execute('SELECT 1 FROM points_lots l LEFT JOIN points_projection_rebuild p ON p.lot_id=l.id WHERE p.lot_id IS NULL LIMIT 1').fetchone():
                invalid()
            if conn.execute('''SELECT 1 FROM points_ledger e JOIN points_ledger original ON original.id=e.original_id
              WHERE e.event='reversal' GROUP BY e.original_id HAVING SUM(e.amount)>original.amount LIMIT 1''').fetchone():
                invalid()
            requests_checked = 0
            for request in conn.execute('SELECT * FROM points_requests ORDER BY id'):
                requests_checked += 1
                quote = conn.execute('SELECT max_micro FROM points_quotes WHERE id=?', (request['quote_id'],)).fetchone()
                wallet = conn.execute('SELECT owner FROM points_wallets WHERE id=?', (request['wallet_id'],)).fetchone()
                if (type(request['max_micro']) is not int or type(request['charged_micro']) is not int
                        or not 0 <= request['charged_micro'] <= request['max_micro']
                        or not quote or quote['max_micro'] != request['max_micro'] or not wallet
                        or (request['state'] in ('platform_absorbed','usage_released') and request['charged_micro'] != 0)):
                    invalid()
                if request['state'] == 'settled' and conn.execute("SELECT 1 FROM points_calls WHERE request_id=? AND kind='model' AND state<>'known' LIMIT 1", (request['id'],)).fetchone():
                    invalid()
                reserved = charged = 0
                for allocation in conn.execute('''SELECT a.*,w.owner FROM points_allocations a
                  JOIN points_lots l ON l.id=a.lot_id JOIN points_wallets w ON w.id=l.wallet_id WHERE a.request_id=?''', (request['id'],)):
                    if (type(allocation['reserved']) is not int or type(allocation['charged']) is not int
                            or not 0 <= allocation['charged'] <= allocation['reserved'] or allocation['owner'] != wallet['owner']):
                        invalid()
                    amounts = {row['event']: row['amount'] for row in conn.execute('''SELECT event,SUM(amount) AS amount FROM points_ledger
                      WHERE request_id=? AND lot_id=? AND event IN ('reserve','settle','release','release_expired') GROUP BY event''', (request['id'],allocation['lot_id']))}
                    if (amounts.get('reserve',0) != allocation['reserved'] or amounts.get('settle',0) != allocation['charged']
                            or amounts.get('release',0)+amounts.get('release_expired',0) != allocation['reserved']-allocation['charged']):
                        invalid()
                    reserved += allocation['reserved']; charged += allocation['charged']
                if reserved != request['max_micro'] or charged != request['charged_micro']:
                    invalid()
                if conn.execute('''SELECT 1 FROM points_ledger e LEFT JOIN points_allocations a ON a.request_id=e.request_id AND a.lot_id=e.lot_id
                  WHERE e.request_id=? AND e.event IN ('reserve','settle','release','release_expired') AND a.lot_id IS NULL LIMIT 1''', (request['id'],)).fetchone():
                    invalid()
            repaired = conn.execute('''SELECT COUNT(*) FROM points_lots l JOIN points_projection_rebuild p ON p.lot_id=l.id
              WHERE l.available<>p.available OR l.held<>p.held OR l.spent<>p.spent OR l.expired<>p.expired''').fetchone()[0]
            conn.execute('''UPDATE points_lots SET available=(SELECT available FROM points_projection_rebuild WHERE lot_id=points_lots.id),
              held=(SELECT held FROM points_projection_rebuild WHERE lot_id=points_lots.id),
              spent=(SELECT spent FROM points_projection_rebuild WHERE lot_id=points_lots.id),
              expired=(SELECT expired FROM points_projection_rebuild WHERE lot_id=points_lots.id)''')
            conn.execute('DROP TABLE points_projection_rebuild')
            return {'status':'rebuilt','checked_lots':checked_lots,'repaired_lots':repaired,
                    'ledger_entries':ledger_entries,'requests_checked':requests_checked}

    def audit(self):
        """Verify projections against immutable per-lot posting vectors."""
        with self._connect() as conn:
            mismatches=[]
            for lot in conn.execute('SELECT * FROM points_lots'):
                totals=conn.execute('SELECT COALESCE(SUM(delta_available),0),COALESCE(SUM(delta_held),0),COALESCE(SUM(delta_spent),0),COALESCE(SUM(delta_expired),0) FROM points_ledger WHERE lot_id=?', (lot['id'],)).fetchone()
                actual=tuple(lot[k] for k in ('available','held','spent','expired'))
                if tuple(totals)!=actual or sum(actual)!=lot['amount']:
                    mismatches.append(lot['id'])
            return {'ok':not mismatches,'mismatched_lots':mismatches}
