"""Audited billing operations for a recently MFA-authenticated administrator.

The HTTP boundary authenticates administrators and enforces recent MFA. This
store accepts only that server-derived identity; it never accepts reviewer IDs
from adjustment payloads. Core postings and the operations journal commit in
the same SQLite transaction. Conversation content and credentials are excluded.
"""
from decimal import Decimal, InvalidOperation
import json
import math
import re
import uuid

from .points import (BUCKETS, MICRO, TRIAL, PointsError, _decimal, _hash,
                     _identity, _json, _operation, cost_to_micro, points_string)


SCHEMA = '''
CREATE TABLE IF NOT EXISTS billing_admin_drafts(
 id TEXT PRIMARY KEY, payload TEXT NOT NULL, admin_id INTEGER NOT NULL,
 status TEXT NOT NULL, price_id TEXT, created REAL NOT NULL, published REAL);
CREATE TABLE IF NOT EXISTS billing_admin_adjustments(
 id TEXT PRIMARY KEY, principal TEXT NOT NULL, points TEXT NOT NULL,
 admin_id INTEGER NOT NULL, reason TEXT NOT NULL, expires REAL,
 status TEXT NOT NULL, reviewed_by INTEGER, created REAL NOT NULL, approved REAL);
CREATE TABLE IF NOT EXISTS billing_admin_audit(
 id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL UNIQUE,
 digest TEXT NOT NULL, action TEXT NOT NULL, admin_id INTEGER NOT NULL,
 parameters TEXT NOT NULL, result TEXT NOT NULL, created REAL NOT NULL);
CREATE TRIGGER IF NOT EXISTS billing_admin_audit_immutable_update
 BEFORE UPDATE ON billing_admin_audit BEGIN SELECT RAISE(ABORT,'immutable billing audit'); END;
CREATE TRIGGER IF NOT EXISTS billing_admin_audit_immutable_delete
 BEFORE DELETE ON billing_admin_audit BEGIN SELECT RAISE(ABORT,'immutable billing audit'); END;
CREATE INDEX IF NOT EXISTS billing_admin_pending_adjustments ON billing_admin_adjustments(status,created);
'''


def _admin(value):
    if type(value) is not int or not 1 <= value < 10**18:
        raise PointsError('需要已认证的管理员身份', 403, 'admin_required')
    return value


def _reason(value):
    if not isinstance(value, str) or not 3 <= len(value.strip()) <= 200 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise PointsError('必须填写3至200字的操作原因')
    if re.search(r'(?i)(?:\b(?:sk|fc)-[a-z0-9_-]{12,}|\bbearer\s+\S+|api[_ -]?key\s*[:=]\s*\S+)', value):
        raise PointsError('操作原因不能包含API凭据')
    return value.strip()


def _canonical(value):
    try:
        result = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError):
        raise PointsError('操作参数格式无效') from None
    if len(result.encode()) > 16384:
        raise PointsError('操作参数过长', 413)
    return result


def _page(limit, cursor):
    if type(limit) is not int or not 1 <= limit <= 200 or type(cursor) is not int or not 0 <= cursor <= 10**15:
        raise PointsError('分页参数无效')


def _reference(value):
    if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= 200 or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise PointsError('供应商请求ID无效')
    return value


class BillingAdminStore:
    def __init__(self, points):
        self.points = points
        with points._connect() as conn:
            conn.executescript(SCHEMA)

    def _run(self, action, admin_id, operation_id, parameters, work):
        admin_id, operation_id = _admin(admin_id), _operation(operation_id)
        serialized = _canonical(parameters)
        digest = _hash(_canonical({'action': action, 'admin_id': admin_id, 'parameters': parameters}))
        with self.points._tx() as conn:
            old = conn.execute('SELECT digest,result FROM billing_admin_audit WHERE operation_id=?', (operation_id,)).fetchone()
            if old:
                if old['digest'] != digest:
                    raise PointsError('同一操作标识不能用于不同参数或管理员', 409, 'idempotency_conflict')
                return json.loads(old['result'])
            result = work(conn)
            conn.execute('INSERT INTO billing_admin_audit(operation_id,digest,action,admin_id,parameters,result,created) VALUES(?,?,?,?,?,?,?)',
                         (operation_id, digest, action, admin_id, serialized, _canonical(result), self.points._now()))
            return result

    def configure_policy(self, values, admin_id, operation_id):
        return self._run('policy.configure', admin_id, operation_id, {'policy': values},
                         lambda conn: self.points.configure(values))

    def rebuild_projection(self, admin_id, operation_id, reason):
        reason = _reason(reason)
        return self._run('projection.rebuild', admin_id, operation_id, {'reason': reason},
                         lambda conn: self.points.rebuild_projection(admin_id=admin_id))

    def _preview(self, payload, created):
        preview = {**payload, 'unit': 'million_tokens', 'status': 'draft',
                   'effective_at': payload.get('effective_at', created)}
        sample = {bucket: 1000 if bucket in ('input_uncached', 'billable_output') else 0 for bucket in BUCKETS}
        preview['sample'] = {'input_tokens': 1000, 'output_tokens': 1000,
                             'supplier_cost_cny': str(self.points._cost(sample, preview['rates'])),
                             'user_points': points_string(cost_to_micro(self.points._cost(sample, preview['user_rates'])))}
        return preview

    def draft_price(self, data, admin_id, operation_id):
        def work(conn):
            # Use the core's exact validator without leaving a published row.
            conn.execute('SAVEPOINT price_preview')
            try:
                preview = self.points.publish_price(data, admin_id=admin_id)
            finally:
                conn.execute('ROLLBACK TO SAVEPOINT price_preview')
                conn.execute('RELEASE SAVEPOINT price_preview')
            payload = {key: preview[key] for key in ('provider', 'gateway', 'model', 'rates', 'user_rates', 'adapter', 'cache_read_supported', 'cache_write_supported', 'currency')}
            payload['effective_at'] = preview['effective_at']
            identity = uuid.uuid4().hex
            created = self.points._now()
            conn.execute("INSERT INTO billing_admin_drafts VALUES(?,?,?,'draft',NULL,?,NULL)", (identity, _json(payload), admin_id, created))
            return {'draft_id': identity, 'status': 'draft', 'preview': self._preview(payload, created)}
        return self._run('draft_price', admin_id, operation_id, {'price': data}, work)

    def publish_draft(self, draft_id, admin_id, operation_id):
        draft_id = _operation(draft_id)
        def work(conn):
            draft = conn.execute('SELECT * FROM billing_admin_drafts WHERE id=?', (draft_id,)).fetchone()
            if not draft:
                raise PointsError('价格草稿不存在', 404, 'draft_missing')
            if draft['status'] != 'draft':
                raise PointsError('价格草稿已经发布', 409, 'draft_published')
            payload = json.loads(draft['payload'])
            # An immediate draft becomes effective at publication. A schedule
            # that elapsed while awaiting review becomes effective now.
            if 'effective_at' in payload:
                payload['effective_at'] = max(payload['effective_at'], self.points._now())
            price = self.points.publish_price(payload, admin_id=admin_id)
            conn.execute("UPDATE billing_admin_drafts SET status='published',price_id=?,published=? WHERE id=?", (price['id'], self.points._now(), draft_id))
            return {'draft_id': draft_id, 'status': 'published', 'price': price}
        return self._run('publish_draft', admin_id, operation_id, {'draft_id': draft_id}, work)

    def disable_price(self, price_id, admin_id, operation_id, reason):
        price_id, reason = _operation(price_id), _reason(reason)
        def work(conn):
            self.points.disable_price(price_id)
            return {'price_id': price_id, 'status': 'disabled'}
        return self._run('disable_price', admin_id, operation_id, {'price_id': price_id, 'reason': reason}, work)

    def refund(self, request_id, admin_id, operation_id, reason):
        request_id, reason = _operation(request_id), _reason(reason)
        return self._run('refund', admin_id, operation_id, {'request_id': request_id, 'reason': reason},
                         lambda conn: self.points.refund(request_id, 'admin:' + _hash(operation_id), reason, admin_id=admin_id))

    def adjust(self, actor, points, admin_id, operation_id, reason, expires_at=None):
        principal, reason = _identity(actor), _reason(reason)
        if isinstance(points, (bool, float)) or not isinstance(points, (str, int, Decimal)) or len(str(points)) > 80:
            raise PointsError('积分必须使用十进制字符串')
        try:
            decimal = Decimal(str(points))
        except (InvalidOperation, ValueError):
            raise PointsError('积分格式无效') from None
        if not decimal.is_finite() or not Decimal('-100000') <= decimal <= Decimal('100000') or not decimal or decimal * MICRO != (decimal * MICRO).to_integral_value():
            raise PointsError('调整积分范围或精度无效')
        amount = format(decimal.normalize(), 'f')
        if expires_at is not None and (isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)) or not math.isfinite(expires_at)):
            raise PointsError('调整有效期无效')
        params = {'principal': principal, 'points': amount, 'reason': reason, 'expires_at': expires_at}
        def work(conn):
            if decimal > 0 and (expires_at is None or not self.points._now() < expires_at <= self.points._now() + 366 * 86400):
                raise PointsError('赠送调整必须明确有效期')
            self.points._wallet_ids(conn, principal)
            if abs(decimal * MICRO) <= TRIAL:
                result = self.points.adjust(principal, amount, 'admin:' + _hash(operation_id), reason, admin_id=admin_id, expires_at=expires_at)
                return {'status': 'applied', **result}
            identity = uuid.uuid4().hex
            conn.execute("INSERT INTO billing_admin_adjustments VALUES(?,?,?,?,?,?,'pending_approval',NULL,?,NULL)",
                         (identity, principal, amount, admin_id, reason, expires_at, self.points._now()))
            return {'adjustment_id': identity, 'status': 'pending_approval', **params}
        return self._run('adjust', admin_id, operation_id, params, work)

    def approve_adjustment(self, adjustment_id, admin_id, operation_id):
        adjustment_id = _operation(adjustment_id)
        def work(conn):
            proposal = conn.execute('SELECT * FROM billing_admin_adjustments WHERE id=?', (adjustment_id,)).fetchone()
            if not proposal:
                raise PointsError('待复核调整不存在', 404, 'adjustment_missing')
            if proposal['admin_id'] == admin_id:
                raise PointsError('调整发起人不能自行复核', 403, 'distinct_reviewer_required')
            if proposal['status'] != 'pending_approval':
                raise PointsError('调整已经复核完成', 409, 'adjustment_approved')
            result = self.points.adjust(proposal['principal'], proposal['points'], 'approval:' + adjustment_id, proposal['reason'],
                                        admin_id=proposal['admin_id'], expires_at=proposal['expires'], reviewed_by=admin_id)
            conn.execute("UPDATE billing_admin_adjustments SET status='approved',reviewed_by=?,approved=? WHERE id=?", (admin_id, self.points._now(), adjustment_id))
            return {'adjustment_id': adjustment_id, 'status': 'approved', 'result': result}
        return self._run('approve_adjustment', admin_id, operation_id, {'adjustment_id': adjustment_id}, work)

    def reconcile(self, request_id, call_id, usage, provider_request_id, admin_id, operation_id, reason):
        request_id, call_id, reason = _operation(request_id), _operation(call_id), _reason(reason)
        provider_request_id = _reference(provider_request_id)
        allowed = {'prompt_tokens', 'completion_tokens', 'total_tokens', 'prompt_tokens_details', 'cache_creation_input_tokens',
                   'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens', 'input_tokens', 'output_tokens', 'cache_read_input_tokens'}
        if not isinstance(usage, dict) or set(usage) - allowed or ('prompt_tokens_details' in usage and (not isinstance(usage['prompt_tokens_details'], dict) or set(usage['prompt_tokens_details']) - {'cached_tokens'})):
            raise PointsError('对账仅接受用量计数字段')
        params = {'request_id': request_id, 'call_id': call_id, 'usage': usage, 'provider_request_id': provider_request_id, 'reason': reason}
        def work(conn):
            result = self.points.record_call(request_id, call_id, usage, provider_request_id=provider_request_id)
            if not result.get('complete'):
                raise PointsError('供应商用量不足以核算，不能完成对账', 422, 'usage_incomplete')
            return {'request_id': request_id, 'call_id': call_id, **result}
        return self._run('reconcile', admin_id, operation_id, params, work)

    def reconcile_service(self, request_id, call_id, actual_cost_cny, admin_id, operation_id, reason, *, provider_request_id=None):
        request_id, call_id, reason = _operation(request_id), _operation(call_id), _reason(reason)
        cost, reference = str(_decimal(actual_cost_cny)), _reference(provider_request_id)
        params = {'request_id': request_id, 'call_id': call_id, 'actual_cost_cny': cost, 'provider_request_id': reference, 'reason': reason}
        def work(conn):
            if reference is not None:
                call = conn.execute("SELECT c.*,p.gateway FROM points_calls c JOIN points_requests r ON r.id=c.request_id JOIN points_quotes q ON q.id=r.quote_id JOIN points_prices p ON p.id=q.price_id WHERE c.request_id=? AND c.id=? AND c.kind<>'model'", (request_id, call_id)).fetchone()
                if not call:
                    raise PointsError('服务调用未获授权', 409, 'call_missing')
                provider_key = _hash('service:' + call['kind'] + '\n' + call['gateway'] + '\n' + reference)
                other = conn.execute('SELECT request_id,id FROM points_calls WHERE provider_key=?', (provider_key,)).fetchone()
                if other and (other['request_id'], other['id']) != (request_id, call_id):
                    raise PointsError('供应商调用已关联其他请求', 409, 'provider_call_duplicate')
                if call['provider_key'] and call['provider_key'] != provider_key:
                    raise PointsError('供应商调用标识不能覆盖', 409, 'provider_call_conflict')
                conn.execute('UPDATE points_calls SET provider_key=?,provider_request_id=? WHERE request_id=? AND id=?', (provider_key, reference, request_id, call_id))
            self.points.record_service_call(request_id, call_id, cost)
            return {'request_id': request_id, 'call_id': call_id, 'complete': True, 'cost_cny': cost}
        return self._run('reconcile_service', admin_id, operation_id, params, work)

    def pending(self, *, limit=50, cursor=0):
        _page(limit, cursor)
        with self.points._connect() as conn:
            rows = conn.execute('''SELECT * FROM (
              SELECT rowid*2 AS cursor,'adjustment' AS kind,id AS item_id,principal,points,admin_id,reason,expires,created,NULL AS request_id,NULL AS call_id,NULL AS provider_request_id,NULL AS call_kind,'pending_approval' AS state,NULL AS cost_bound_cny
              FROM billing_admin_adjustments WHERE status='pending_approval' AND rowid*2>?
              UNION ALL SELECT c.rowid*2+1,'usage',c.request_id||':'||c.id,r.principal,NULL,NULL,NULL,NULL,c.created,c.request_id,c.id,c.provider_request_id,c.kind,c.state,c.bound_cost
              FROM points_calls c JOIN points_requests r ON r.id=c.request_id WHERE c.state<>'known' AND r.state IN ('usage_pending','usage_released','platform_absorbed','settled') AND c.rowid*2+1>?)
              ORDER BY cursor LIMIT ?''', (cursor, cursor, limit + 1)).fetchall()
            items = [{**dict(r), 'id': r['item_id'], 'adjustment_id': r['item_id'] if r['kind'] == 'adjustment' else None} for r in rows[:limit]]
            return {'items': items, 'next_cursor': items[-1]['cursor'] if len(rows) > limit else None}

    def drafts(self, *, limit=50, cursor=0, status=None):
        _page(limit, cursor)
        if status not in (None, 'draft', 'published'):
            raise PointsError('草稿状态无效')
        with self.points._connect() as conn:
            rows = conn.execute('SELECT rowid AS cursor,* FROM billing_admin_drafts WHERE rowid>? AND (? IS NULL OR status=?) ORDER BY rowid LIMIT ?', (cursor, status, status, limit + 1)).fetchall()
            items = []
            for row in rows[:limit]:
                payload = json.loads(row['payload'])
                items.append({'cursor': row['cursor'], 'draft_id': row['id'], 'status': row['status'], 'admin_id': row['admin_id'],
                              'price_id': row['price_id'], 'created_at': row['created'], 'published_at': row['published'],
                              'price': payload, 'preview': self._preview(payload, row['created'])})
            return {'items': items, 'next_cursor': items[-1]['cursor'] if len(rows) > limit else None}

    def adjustments(self, *, limit=50, cursor=0, status='pending_approval'):
        _page(limit, cursor)
        if status not in (None, 'pending_approval', 'approved'):
            raise PointsError('调整状态无效')
        with self.points._connect() as conn:
            rows = conn.execute('SELECT rowid AS cursor,* FROM billing_admin_adjustments WHERE rowid>? AND (? IS NULL OR status=?) ORDER BY rowid LIMIT ?', (cursor, status, status, limit + 1)).fetchall()
            items = [{'cursor': r['cursor'], 'adjustment_id': r['id'], 'principal': r['principal'], 'points': r['points'], 'admin_id': r['admin_id'],
                      'reason': r['reason'], 'expires_at': r['expires'], 'status': r['status'], 'reviewed_by': r['reviewed_by'],
                      'created_at': r['created'], 'approved_at': r['approved']} for r in rows[:limit]]
            return {'items': items, 'next_cursor': items[-1]['cursor'] if len(rows) > limit else None}

    def finance_audit(self, *, limit=50, cursor=0):
        _page(limit, cursor)
        with self.points._connect() as conn:
            rows = conn.execute('SELECT * FROM billing_admin_audit WHERE id>? ORDER BY id LIMIT ?', (cursor, limit + 1)).fetchall()
            items = [{'id': r['id'], 'operation_id': r['operation_id'], 'action': r['action'], 'admin_id': r['admin_id'],
                      'parameters': json.loads(r['parameters']), 'result': json.loads(r['result']), 'created_at': r['created']} for r in rows[:limit]]
            return {'items': items, 'next_cursor': items[-1]['id'] if len(rows) > limit else None}

    def report(self, *, start_at=None, end_at=None, limit=100, cursor=0):
        _page(limit, cursor)
        now = self.points._now()
        end_at = now if end_at is None else end_at
        if isinstance(end_at, bool) or not isinstance(end_at, (int, float)) or not math.isfinite(end_at):
            raise PointsError('导出时间范围必须在31天以内')
        start_at = end_at - 30 * 86400 if start_at is None else start_at
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (start_at, end_at)) or not 0 <= start_at < end_at <= now or end_at - start_at > 31 * 86400:
            raise PointsError('导出时间范围必须在31天以内')
        with self.points._connect() as conn:
            rows = conn.execute('''SELECT r.rowid AS cursor,r.*,p.id AS price_id,p.model,p.gateway,p.provider FROM points_requests r
              JOIN points_quotes q ON q.id=r.quote_id JOIN points_prices p ON p.id=q.price_id
              WHERE r.rowid>? AND r.created>=? AND r.created<=? ORDER BY r.rowid LIMIT ?''', (cursor, start_at, end_at, limit + 1)).fetchall()
            items = []
            for row in rows[:limit]:
                calls = conn.execute('SELECT id,kind,state,provider_request_id,usage,cost,user_cost,day FROM points_calls WHERE request_id=? ORDER BY created,id', (row['id'],)).fetchall()
                refunded = conn.execute("SELECT COALESCE(SUM(amount),0) FROM points_ledger WHERE request_id=? AND event='reversal'", (row['id'],)).fetchone()[0]
                complete = all(c['state'] == 'known' for c in calls)
                known_cost = str(sum((Decimal(c['cost']) for c in calls if c['cost'] is not None), Decimal(0)))
                model_calls = [c for c in calls if c['kind'] == 'model']
                user_complete = all(c['state'] == 'known' and c['user_cost'] is not None for c in model_calls)
                known_user_cost = str(sum((Decimal(c['user_cost']) for c in model_calls if c['user_cost'] is not None), Decimal(0)))
                items.append({'cursor': row['cursor'], 'request_id': row['id'], 'principal': row['principal'], 'price_id': row['price_id'],
                              'provider': row['provider'], 'gateway': row['gateway'], 'model': row['model'], 'state': row['state'],
                              'charged_points': points_string(row['charged_micro']), 'refunded_points': points_string(refunded),
                              'supplier_cost_cny': known_cost if complete else None, 'known_supplier_cost_cny': known_cost,
                              'user_priced_cost_cny': known_user_cost if user_complete else None,
                              'known_user_priced_cost_cny': known_user_cost, 'user_priced_cost_complete': user_complete,
                              'cost_complete': complete, 'cash_revenue_cny': '0', 'created_at': row['created'],
                              'calls': [{'call_id': c['id'], 'kind': c['kind'], 'state': c['state'], 'provider_request_id': c['provider_request_id'],
                                         'usage': json.loads(c['usage']) if c['usage'] else None, 'cost_cny': c['cost'],
                                         'user_cost_cny': c['user_cost'], 'billing_day': c['day']} for c in calls]})
            return {'items': items, 'next_cursor': items[-1]['cursor'] if len(rows) > limit else None, 'window': {'start_at': start_at, 'end_at': end_at}}
