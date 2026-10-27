"""HTTP integration for server-owned identities, credits and abuse controls."""
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
import uuid
from http import cookies
from urllib.parse import parse_qs

from .billing_runtime import input_bound
from .points import PointsError
from .security import SecurityError, client_ip
from .issues import IssueError, thread_id

GUEST_COOKIE = 'jiaoyuan_guest'
BROWSER_COOKIE = 'jiaoyuan_trial'
WALLET_COOKIE = 'jiaoyuan_wallet'
GEN_FIELDS = {'question', 'mode', 'as_of', 'thread_id', 'temporary', 'search',
              'history', 'memory_thread_ids', 'reading_selection'}
AUTH_FIELDS = {'username', 'password'}
CONTROL_FIELDS = {'quote_id', 'client_request_id', 'operation_id', 'security_proof'}


def canonical_digest(secret, value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
    return hmac.new(secret, encoded, hashlib.sha256).hexdigest()


class CreditsHTTPMixin:
    def _record_risk(self, code, status=403):
        """Use fixed server labels, never the raw URL/body/exception text."""
        try:
            from urllib.parse import urlparse
            path = urlparse(self.path).path
            action = {'/api/auth/register': 'auth.register', '/api/auth/login': 'auth.login',
                      '/api/admin/login': 'admin.login', '/api/points/quote': 'points.quote',
                      '/api/agent/stream': 'generation', '/api/security/verify': 'challenge.verify',
                      '/api/guest/bootstrap': 'guest.bootstrap'}.get(path, 'http.request')
            if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.:-]{0,79}', code): code = 'request_rejected'
            account = self._account()
            guest = self._guest() if not account else None
            principal = 'u:' + str(account['user_id']) if account else guest['principal'] if guest else None
            try: source = self._source()
            except SecurityError: source = None
            return self.server.risk_audit.record(action, code, principal=principal, source=source,
                outcome='challenged' if code == 'challenge_required' else 'error' if status >= 500 else 'blocked')
        except Exception:
            return None

    def _http_state(self):
        conn = sqlite3.connect(str(self.service.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _cookie_value(self, name):
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get('Cookie', ''))
        except cookies.CookieError:
            return None
        return jar[name].value if name in jar else None

    def _cookie_line(self, name, value, max_age=180 * 86400):
        jar = cookies.SimpleCookie(); jar[name] = value
        for key, val in {'path': '/', 'httponly': True, 'samesite': 'Lax', 'max-age': max_age}.items():
            jar[name][key] = val
        if self.server.secure_cookie:
            jar[name]['secure'] = True
        return jar.output(header='').strip()

    def _source(self):
        return client_ip(self.client_address[0], self.headers, self.server.trusted_proxies)

    def _account(self):
        result = self.service.validate_session(self._token() or '')
        return result.get('user') if result.get('ok') else None

    def _guest(self):
        return self.server.points.resolve_guest(self._cookie_value(GUEST_COOKIE))

    def _principal_user(self):
        user = self._account()
        if user:
            actor = self.server.points.user_actor(user['user_id'])
            return {**user, 'is_guest': False, 'actor': actor}
        actor = self._guest()
        if actor:
            return {'user_id': actor['storage_id'], 'username': '访客', 'is_guest': True, 'actor': actor}
        return None

    def _actor(self):
        user = self._principal_user()
        if not user:
            raise PointsError('请重新打开对话页面建立访客会话', 401, 'identity_required')
        return user['actor']

    def _security_actor(self):
        return self._actor()['principal']

    def _proof_actor(self, action):
        if action in {'register', 'login', 'admin_login'}:
            guest = self._guest()
            if guest: return guest['principal']
        return self._security_actor()

    def _registration_subject(self):
        guest = self._guest()
        if guest:
            with self.server.points._connect() as conn:
                row = conn.execute('''SELECT COALESCE(w.entitlement,b.entitlement) AS eligibility
                    FROM points_guests g JOIN points_wallets w ON w.id=g.wallet_id
                    JOIN points_browsers b ON b.id=g.browser_id WHERE g.principal=?''', (guest['principal'],)).fetchone()
            if row: return 'eligibility:' + row['eligibility']
        return self._security_actor()

    def _operation_id(self, data):
        value = data.get('operation_id') or data.get('client_request_id')
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_:-]{7,127}', value):
            raise SecurityError('操作标识无效，请刷新后重试', 400, 'operation_required')
        return value

    def _require_proof(self, data, action, value):
        actor, operation = self._proof_actor(action), self._operation_id(data)
        digest = canonical_digest(self.service.security_key, value)
        proof = data.get('security_proof')
        if proof and self.server.security.consume(proof, actor, action, operation, digest):
            return
        challenge = self.server.security.challenge(actor, action, operation, digest, self._source())
        self._json(403, {'ok': False, 'code': 'challenge_required', 'error': '请完成验证后继续',
                         'challenge': challenge, 'operation_id': operation, 'digest': digest})
        raise _ChallengeSent()

    def _content_available(self, user, tid=None):
        migration = getattr(self.server, 'migration', None)
        if migration:
            migration.assert_available(user['user_id'], tid)

    def _wallet_rate(self, actor, scope, limit):
        with self.server.points._connect() as conn:
            wallets = self.server.points._wallet_ids(conn, actor)
        for wallet in wallets:
            self.server.security.require_rate(scope + '-wallet', str(wallet), limit)

    def _security_public(self):
        security = self.server.security
        return {'provider': 'test' if security.test_mode else 'turnstile',
                'configured': bool(security.test_mode or (security.site_key and security.turnstile_secret and security.hostname)),
                'site_key': security.site_key, 'mfa_required': self.server.require_admin_mfa,
                'rules_version': self.server.risk_audit.summary()['rules_version']}

    def _bootstrap_guest(self, *, rotate=False):
        result = self.server.points.bootstrap(self._cookie_value(GUEST_COOKIE), self._cookie_value(BROWSER_COOKIE),
                                             self._cookie_value(WALLET_COOKIE), rotate_workspace=rotate)
        lines = []
        for field, cookie in [('guest_token', GUEST_COOKIE), ('browser_token', BROWSER_COOKIE), ('wallet_token', WALLET_COOKIE)]:
            if result.get(field):
                lines.append(self._cookie_line(cookie, result[field]))
        actor = result['actor']
        account = self._account()
        if account:
            actor = self.server.points.user_actor(account['user_id'])
        user = {**(account or {'username': '访客'}), 'user_id': actor['storage_id'], 'is_guest': actor['kind'] == 'guest'}
        count = self.server.issues.list(result['actor']['storage_id'], self.server.agent, limit=1).get('total', 0)
        return {'ok': True, 'actor': actor, 'user': user, 'security': self._security_public(),
                'wallet': self.server.points.wallet(actor), 'workspace_topics_count': count}, {'Set-Cookie': lines}

    def _search_policy(self, mode):
        # Search remains a server capability, independent of legacy UI flags.
        # Missing pricing or credentials degrades to local answers.
        from .conversation import SEARCH_POLICY_VERSION
        available = False
        if mode != 'historical' and self.server.search_cash_bound:
            try:
                available = bool(self.server.agent.firecrawl.config.api_key())
            except Exception:
                pass
        return {'version': SEARCH_POLICY_VERSION, 'available': available,
                'bound': self.server.search_cash_bound if available else None}

    def _generation_digest(self, data, user):
        if set(data) - GEN_FIELDS - CONTROL_FIELDS:
            raise PointsError('请求包含不支持的生成字段')
        question, mode = data.get('question'), data.get('mode', 'consult')
        if not isinstance(question, str) or not question.strip() or len(question) > 4000:
            raise PointsError('问题不能为空且不能超过4000字')
        if mode not in {'consult', 'research', 'historical', 'modern'}:
            raise PointsError('回答模式无效')
        if mode == 'historical':
            from datetime import datetime
            date = data.get('as_of')
            if not isinstance(date, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date):
                raise PointsError('历史模式需要YYYY-MM-DD截止日期')
            try: datetime.strptime(date, '%Y-%m-%d')
            except ValueError: raise PointsError('历史模式截止日期无效') from None
        if type(data.get('temporary', False)) is not bool or type(data.get('search', False)) is not bool:
            raise PointsError('生成选项无效')
        tid = thread_id(data.get('thread_id', 'default'))
        self._content_available(user, tid)
        memory_ids = data.get('memory_thread_ids', [])
        if mode != 'consult' and memory_ids:
            raise IssueError('此模式不使用其他议题记忆')
        selected = self.server.issues.memory(user['user_id'], memory_ids, self.server.agent)
        if data.get('temporary'):
            snapshot = {'temporary': True}
        else:
            try:
                snapshot = self.server.issues.load(user['user_id'], tid, self.server.agent)
            except IssueError as exc:
                if exc.deleted: raise IssueError('议题已删除，请新建对话', 410, deleted=True) from None
                raise
            if snapshot['exists'] and (snapshot['metadata'].get('mode') != mode or snapshot['metadata'].get('as_of') != data.get('as_of')):
                raise IssueError('对话模式与截止日期已固定，请新建对话', 409)
        values = {key: data[key] for key in sorted(GEN_FIELDS) if key in data}
        config = self.server.agent.config
        from .knowledge_versions import knowledge_revision
        from .telemetry import _VERSIONS
        from .reader import selected_context
        values['reading_context'] = selected_context(config.db_path, data.get('reading_selection'), mode)
        values['knowledge_revision'] = knowledge_revision(config.db_path)
        from .continuity import CONTEXT_POLICY_VERSION
        values['pipeline_version'] = _VERSIONS['pipeline_version']
        values['context_policy'] = CONTEXT_POLICY_VERSION
        values['search_policy'] = self._search_policy(mode)
        values['context'] = snapshot
        values['selected_context'] = selected
        values['deployment'] = {'model': config.llm.model, 'gateway': config.llm.base_url,
                                'steps': config.max_steps, 'output': config.llm.max_tokens}
        return canonical_digest(self.service.security_key, values)

    def _payload_digest(self, data):
        return canonical_digest(self.service.security_key, {k: data[k] for k in sorted(GEN_FIELDS) if k in data})

    def _remember_quote(self, data, actor, digest):
        operation = self._operation_id(data)
        payload = self._payload_digest(data)
        with self._http_state() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('DELETE FROM http_generation_operations WHERE request_id IS NULL AND created<?', (time.time() - 600,))
            old = conn.execute('SELECT * FROM http_generation_operations WHERE principal=? AND client_key=?', (actor['principal'], operation)).fetchone()
            if old and (old['payload_digest'] != payload or old['request_id']):
                raise PointsError('请求标识已使用，请恢复原请求或重新发送', 409, 'idempotency_conflict')
            conn.execute('''INSERT INTO http_generation_operations(principal,client_key,payload_digest,context_digest,thread_id,temporary,created)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(principal,client_key) DO UPDATE SET context_digest=excluded.context_digest,created=excluded.created''',
                (actor['principal'], operation, payload, digest, data.get('thread_id', 'default'), int(data.get('temporary', False)), time.time()))

    def _quote(self, data):
        user, actor = self._principal_user(), self._actor()
        self.server.security.require_rate('quote', actor['principal'], 20)
        self._wallet_rate(actor, 'quote', 20)
        digest = self._generation_digest(data, user)
        wallet = self.server.points.wallet(actor)
        if actor['kind'] == 'guest' and not wallet['trial_granted']:
            self._require_proof(data, 'guest_grant', {'digest': digest})
            self.server.points.grant_guest(actor)
            wallet = self.server.points.wallet(actor)
        if wallet['benefit_pending']:
            self._require_proof(data, 'benefit_verify', {'principal': actor['principal']})
            self.server.points.set_benefit_pending(actor, False)
            wallet = self.server.points.wallet(actor)
        config = self.server.agent.config
        cap = self.server.billing_input_cap
        search = self._search_policy(data.get('mode', 'consult'))['available']
        extra = '0'
        if search:
            extra = str(Decimal(self.server.search_cash_bound) * config.max_steps)
        quote = self.server.points.quote(actor, digest, provider=config.llm.provider, model=config.llm.model,
                                          gateway=config.llm.base_url, input_tokens=cap,
                                          output_tokens=config.llm.max_tokens, max_calls=config.max_steps,
                                          cash_extra_cny=extra)
        if wallet['available_micro'] < quote['max_micro']:
            raise PointsError('本轮最高需要' + quote['max_points'] + '积分，可用' + wallet['available'] + '积分；请减少上下文或联系管理员配置更低费用模型', 402, 'insufficient_points')
        self._remember_quote(data, actor, digest)
        self._json(200, {'ok': True, **quote, 'wallet': wallet, 'requires_confirmation': True})

    def _reserve_generation(self, data, user):
        actor = user['actor']
        operation = data.get('client_request_id')
        with self._http_state() as conn:
            binding = conn.execute('SELECT * FROM http_generation_operations WHERE principal=? AND client_key=?', (actor['principal'], operation)).fetchone()
        if not binding or binding['payload_digest'] != self._payload_digest(data):
            raise PointsError('报价与当前消息不一致，请重新确认', 422, 'quote_invalid')
        digest = binding['context_digest']
        if not binding['request_id'] and digest != self._generation_digest(data, user):
            raise PointsError('议题已更新，请重新确认费用', 422, 'quote_invalid')
        self.server.security.require_rate('generate', actor['principal'], 6 if user['is_guest'] else 12)
        self._wallet_rate(actor, 'generate', 6 if user['is_guest'] else 12)
        result = self.server.points.reserve(actor, data.get('quote_id'), operation, digest)
        with self._http_state() as conn:
            conn.execute('UPDATE http_generation_operations SET request_id=? WHERE principal=? AND client_key=?', (result['request_id'], actor['principal'], operation))
        return result

    def _remember_delivery(self, request_id, turn_id=None):
        with self._http_state() as conn:
            conn.execute('UPDATE http_generation_operations SET turn_id=? WHERE request_id=?', (turn_id, request_id))

    def _public_charge(self, value):
        value = {k: v for k, v in value.items() if k not in {'supplier_cost_cny', 'platform_cost_cny'}}
        value['calls'] = [{k: v for k, v in call.items() if k != 'cost_cny'} for call in value.get('calls', [])]
        with self.server.points._connect() as conn:
            row = conn.execute('''SELECT p.* FROM points_requests r JOIN points_quotes q ON q.id=r.quote_id
                JOIN points_prices p ON p.id=q.price_id WHERE r.id=?''', (value['request_id'],)).fetchone()
            if row: value['price'] = self.server.points._price_public(row, public=True)
        return value

    def _stream_wallet(self, user):
        try:
            return self.server.points.wallet(user['actor'])
        except PointsError as exc:
            if exc.status != 401: raise
            # A formerly anonymous in-flight stream may finish after its wallet
            # moves. Its old content capability must never reveal account funds.
            return None

    def _cancel_unstarted(self, storage_id, tid):
        with self._http_state() as conn:
            if storage_id < 0:
                guest = conn.execute('SELECT principal FROM points_guests WHERE id=?', (-storage_id,)).fetchone()
                principal = guest['principal'] if guest else None
            else:
                principal = 'u:' + str(storage_id)
            rows = conn.execute('''SELECT r.id FROM points_requests r JOIN http_generation_operations h ON h.request_id=r.id
                WHERE h.principal=? AND h.thread_id=? AND r.state='reserved'
                AND NOT EXISTS(SELECT 1 FROM points_calls c WHERE c.request_id=r.id)''', (principal, tid)).fetchall()
        for row in rows:
            self.server.points.settle(row['id'], delivered=False, cancelled=True)

    def _request_recovery(self, actor, request_id):
        with self._http_state() as conn:
            wallets = self.server.points._wallet_ids(conn, actor)
            holders = ','.join('?' for _ in wallets)
            exact = conn.execute('SELECT id FROM points_requests WHERE id=? AND wallet_id IN (' + holders + ')', (request_id, *wallets)).fetchone()
            if not exact:
                candidates = conn.execute('''SELECT h.request_id FROM http_generation_operations h
                    JOIN points_requests r ON r.id=h.request_id WHERE h.client_key=? AND r.wallet_id IN (''' + holders + ') LIMIT 2', (request_id, *wallets)).fetchall()
                if len(candidates) > 1:
                    raise PointsError('同名操作存在多个结果，请使用已接受的请求ID恢复', 409, 'ambiguous_operation')
                if candidates: request_id = candidates[0]['request_id']
            authorized = conn.execute('SELECT 1 FROM points_requests WHERE id=? AND wallet_id IN (' + holders + ')', (request_id, *wallets)).fetchone()
        if authorized:
            self.server.recover_deliveries(request_id=request_id)
        try:
            result = self.server.points.request_status(actor, request_id)
        except PointsError as exc:
            if exc.status != 404: raise
            with self._http_state() as conn:
                binding = conn.execute('SELECT request_id FROM http_generation_operations WHERE principal=? AND client_key=?', (actor['principal'], request_id)).fetchone()
            if not binding or not binding['request_id']: raise
            request_id = binding['request_id']
            result = self.server.points.request_status(actor, request_id)
        with self._http_state() as conn:
            binding = conn.execute('SELECT * FROM http_generation_operations WHERE request_id=?', (request_id,)).fetchone()
            target = binding['thread_id'] if binding and binding['principal'] == actor['principal'] else None
            if binding and not target and actor['kind'] == 'user':
                moved = conn.execute('''SELECT target_thread FROM guest_migration_threads
                    WHERE source_user=(SELECT -id FROM points_guests WHERE principal=?)
                    AND source_thread=? AND target_user=? AND state='committed' ''',
                    (binding['principal'], binding['thread_id'], actor['storage_id'])).fetchone()
                if moved: target = moved['target_thread']
        if binding and target and not binding['temporary']:
            user = self._principal_user()
            result['thread_id'] = target
            result['answer'] = None
            try:
                self._content_available(user, target)
                if binding['turn_id']:
                    snapshot = self.server.issues.load(user['user_id'], target, self.server.agent)
                    result['answer'] = next((x['content'] for x in snapshot['messages'] if x.get('role') == 'assistant' and x.get('turn_id') == binding['turn_id']), None)
            except IssueError as exc:
                if exc.status not in {404, 410}: raise
                result['deleted'] = True
        return self._public_charge(result)

    def _sensitive_admin(self, data):
        admin = self._admin_required()
        if not admin:
            return None
        if data.get('otp_code'):
            result = self.service.reauthenticate_admin(self._admin_token() or '', data['otp_code'])
            if not result.get('ok'):
                raise SecurityError('动态验证未通过', 403, 'reauth_required')
            admin = result['admin']
        recent = admin.get('recent_authenticated_at') or admin.get('mfa_verified_at')
        if not recent or time.time() - float(recent) > 300 or (self.server.require_admin_mfa and not admin.get('mfa_verified_at')):
            raise SecurityError('请重新登录或输入新的动态验证代码', 403, 'reauth_required')
        return admin

    def _credits_get(self, path, query):
        if path == '/api/security/appeals':
            result = self.server.security_appeals.list_own(self._security_actor(),
                limit=int(query.get('limit', ['20'])[0]), offset=int(query.get('offset', ['0'])[0]))
            self._json(200, result); return True
        if path == '/api/admin/security/appeals':
            if not self._admin_required(): return True
            result = self.server.security_appeals.list_admin(limit=int(query.get('limit', ['50'])[0]),
                offset=int(query.get('offset', ['0'])[0]), status=query.get('status', [None])[0])
            self._json(200, result); return True
        if path == '/api/admin/security/config':
            if not self._admin_required(): return True
            self._json(200, {'ok': True, 'config': self.server.security_config.public(self.server.security_config.load())}); return True
        if path == '/api/admin/billing/projection-audit':
            if not self._admin_required(): return True
            self._json(200, {'ok': True, 'audit': self.server.points.audit()}); return True
        if path == '/api/security/status':
            self._json(200, {'ok': True, **self._security_public()}); return True
        if path == '/api/auth/me':
            user = self._principal_user()
            if user:
                self._json(200, {'ok': True, 'user': {k: v for k, v in user.items() if k != 'actor'}, 'actor': user['actor']})
            else:
                self._json(401, {'ok': False, 'error': '请建立访客会话或登录'})
            return True
        if path == '/api/points/wallet':
            self._json(200, {'ok': True, 'wallet': self.server.points.wallet(self._actor())}); return True
        if path == '/api/points/ledger':
            result = self.server.points.ledger(self._actor(), limit=int(query.get('limit', ['50'])[0]), cursor=int(query.get('cursor', ['0'])[0]))
            self._json(200, {'ok': True, **result}); return True
        if path.startswith('/api/points/requests/'):
            result = self._request_recovery(self._actor(), path.rsplit('/', 1)[1])
            self._json(200, {'ok': True, **result, 'wallet': self.server.points.wallet(self._actor())}); return True
        if path.startswith('/api/account/guest-claim/'):
            account = self._account()
            if not account: raise PointsError('请先登录账户', 401)
            result = self.server.migration.status(path.rsplit('/', 1)[1], self.server.points.user_actor(account['user_id']))
            self._json(200, {'ok': True, **result}); return True
        if path == '/api/admin/billing':
            admin = self._admin_required()
            if admin:
                events = self.server.risk_audit.list(limit=30)
                risk_summary = self.server.risk_audit.summary()
                self._json(200, {'ok': True, 'prices': self.server.points.prices(), 'summary': self.server.points.summary(),
                                 'policy': self.server.points.policy(), 'security': self._security_public(),
                                 'risk': {'events': events, 'summary': risk_summary, 'rules_version': risk_summary['rules_version']},
                                 'finance_audit': self.server.billing_admin.finance_audit(limit=30),
                                 'pending_usage': self.server.billing_admin.pending(limit=30),
                                 'drafts': self.server.billing_admin.drafts(limit=30),
                                 'adjustments': self.server.billing_admin.adjustments(limit=30)})
            return True
        if path in {'/api/admin/security/events', '/api/admin/security/summary'}:
            if not self._admin_required(): return True
            common = {}
            for key in ('since', 'until'):
                if key in query: common[key] = float(query[key][0])
            if path.endswith('summary'):
                result = self.server.risk_audit.summary(**common)
            else:
                result = self.server.risk_audit.list(limit=int(query.get('limit', ['50'])[0]),
                    offset=int(query.get('offset', ['0'])[0]),
                    **{key: query[key][0] for key in ('action','code','outcome') if key in query}, **common)
                result['rules_version'] = self.server.risk_audit.summary()['rules_version']
            self._json(200, {'ok': True, **result}); return True
        listing = {'/api/admin/billing/finance-audit': 'finance_audit', '/api/admin/billing/pending-usage': 'pending',
                   '/api/admin/billing/adjustments': 'adjustments', '/api/admin/billing/drafts': 'drafts',
                   '/api/admin/billing/report': 'report'}
        if path in listing:
            if not self._admin_required(): return True
            arguments = {'limit': int(query.get('limit', ['50'])[0]), 'cursor': int(query.get('cursor', ['0'])[0])}
            if listing[path] == 'report':
                for key in ('start_at', 'end_at'):
                    if key in query: arguments[key] = float(query[key][0])
            result = getattr(self.server.billing_admin, listing[path])(**arguments)
            self._json(200, {'ok': True, **result}); return True
        return False

    def _credits_post(self, path, data):
        if path == '/api/security/appeals':
            if set(data) - {'operation_id','security_proof','risk_event_id','code','note'}:
                raise SecurityError('申诉字段无效', 400)
            principal = self._security_actor()
            self.server.security.require_rate('appeal-actor', principal, 10)
            self.server.security.require_rate('appeal-source', self._source(), 30)
            values = {key: data.get(key) for key in ('risk_event_id','code','note')}
            if values['note'] is None: values['note'] = ''
            self._require_proof(data, 'appeal', values)
            result = self.server.security_appeals.submit(principal, self._operation_id(data), **values)
            self._json(200, result); return True
        if path.startswith('/api/admin/security/appeals/'):
            match = re.fullmatch(r'/api/admin/security/appeals/([a-f0-9]{32})/review', path)
            if not match: raise SecurityError('申诉操作不存在', 404)
            admin = self._sensitive_admin(data)
            if not admin: return True
            if set(data) - {'operation_id','otp_code','decision','reason'}:
                raise SecurityError('申诉复核字段无效', 400)
            result = self.server.security_appeals.review(match.group(1), data.get('decision'), admin['admin_id'],
                self._operation_id(data), data.get('reason'))
            self._json(200, result); return True
        if path == '/api/admin/security/config':
            admin = self._sensitive_admin(data)
            if not admin: return True
            operation = self._operation_id(data)
            values = {key: value for key, value in data.items() if key not in {'operation_id','otp_code','reason'}}
            values = self.server.security_config.validate(values)
            hostname = values.get('hostname')
            if hostname and hostname != 'localhost':
                import ipaddress
                try: local = ipaddress.ip_address(hostname).is_loopback
                except ValueError: local = False
                if not local and not admin.get('mfa_verified_at'):
                    raise SecurityError('启用公网域名前请登记TOTP并使用动态码验证', 403, 'mfa_required')
            result = self.server.security_config.update(values, admin['admin_id'], operation, data.get('reason'))
            self.server.reload_security_settings()
            self._json(200, result); return True
        if path.startswith('/api/admin/billing/'):
            return self._billing_admin_post(path, data)
        if path == '/api/guest/bootstrap':
            self.server.security.require_rate('bootstrap-source', self._source(), 60)
            result, headers = self._bootstrap_guest()
            self._json(200, result, headers); return True
        if path == '/api/security/challenge':
            if data.get('action') not in {'guest_grant', 'register', 'login', 'admin_login', 'generate', 'benefit_verify', 'appeal'}:
                raise SecurityError('验证操作无效', 400)
            challenge = self.server.security.challenge(self._proof_actor(data.get('action')), data.get('action'), self._operation_id(data), data.get('digest'), self._source())
            self._json(200, {'ok': True, **challenge}); return True
        if path == '/api/security/verify':
            candidates = [self._security_actor()]
            guest = self._guest()
            if guest and guest['principal'] not in candidates: candidates.append(guest['principal'])
            for candidate in candidates:
                try:
                    proof = self.server.security.verify(data.get('challenge_id'), data.get('token'), candidate, self._source())
                    break
                except SecurityError as exc:
                    if candidate == candidates[-1] or exc.status != 403: raise
            self._json(200, {'ok': True, **proof}); return True
        if path == '/api/points/quote':
            self._quote(data); return True
        if path in {'/api/auth/register', '/api/auth/login'}:
            method = path.rsplit('/', 1)[1]
            self.server.security.require_rate('auth-source:' + method, self._source(), 30, 300)
            if set(data) - AUTH_FIELDS - CONTROL_FIELDS - {'math_challenge_id', 'math_answer'}:
                raise PointsError('账户请求字段无效')
            self._require_proof(data, method, {key: data.get(key) for key in AUTH_FIELDS})
            if method == 'register':
                operation = self._operation_id(data)
                actor = self._proof_actor(method)
                digest = canonical_digest(self.service.security_key, {key: data.get(key) for key in AUTH_FIELDS})
                with self._http_state() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    old = conn.execute('SELECT * FROM http_auth_operations WHERE principal=? AND action=? AND operation_id=?', (actor, method, operation)).fetchone()
                    if old:
                        if old['digest'] != digest:
                            raise PointsError('操作标识不能用于不同账户内容', 409, 'idempotency_conflict')
                        if old['response']:
                            self._json(200, json.loads(old['response'])); return True
                    else:
                        conn.execute('INSERT INTO http_auth_operations VALUES(?,?,?,?,NULL,?)', (actor, method, operation, digest, time.time()))
            extra = {}
            if method == 'register':
                operation_key = canonical_digest(self.service.security_key, [actor, method, operation])
                def pending_on_create(conn, uid):
                    count = conn.execute("SELECT COUNT(*) FROM users WHERE created_at>=datetime('now','-1 day')").fetchone()[0]
                    if count > self.server.registration_daily_cap:
                        raise SecurityError('当前注册请求较多，请稍后重试', 429, 'registration_capacity')
                    conn.execute('INSERT INTO points_principals(principal,benefit_pending,created) VALUES(?,1,?)', ('u:' + str(uid), time.time()))
                extra = {'operation_key': operation_key, 'on_created': pending_on_create}
            result = getattr(self.service, method)(data.get('username', ''), data.get('password', ''),
                                                   rate_key=self._source(), require_captcha=False, **extra)
            if result.get('ok') and method == 'register':
                browser = self._registration_subject()
                risk = self.server.security.record_registration(browser, self._source(), operation_id=operation_key)
                self.server.points.set_benefit_pending(self.server.points.user_actor(result['user_id']), risk['benefit_pending'])
                result['benefit_pending'] = risk['benefit_pending']
                if not risk['benefit_pending']:
                    guest = self._guest()
                    if guest:
                        self.server.points.grant_guest(guest)
                with self._http_state() as conn:
                    conn.execute('UPDATE http_auth_operations SET response=? WHERE principal=? AND action=? AND operation_id=?', (json.dumps(result, ensure_ascii=False), actor, method, operation))
            elif method == 'register':
                with self._http_state() as conn:
                    conn.execute('DELETE FROM http_auth_operations WHERE principal=? AND action=? AND operation_id=? AND response IS NULL', (actor, method, operation))
            if result.get('ok') and method == 'login':
                token = result.pop('session_token')
                guest = self._guest()
                if guest:
                    self.server.points.grant_guest(guest)
                self.server.points.wallet(self.server.points.user_actor(result['user']['user_id']))
                self._json(200, result, {'Set-Cookie': self._cookie_line('mao_session', token, self.service.session_ttl)})
            else:
                self._json(200 if result.get('ok') else 400, result)
            return True
        if path == '/api/auth/logout':
            result = self.service.logout(self._token() or '')
            # Logout revokes authentication and content capabilities; creation
            # of an empty replacement is exclusively the bounded bootstrap API.
            self._json(200, result, {'Set-Cookie': [self._cookie_line('mao_session', '', 0), self._cookie_line(GUEST_COOKIE, '', 0)]})
            return True
        if path == '/api/account/guest-claim':
            account = self._account()
            if not account: raise PointsError('请先登录账户', 401)
            source = self._guest()
            if not source:
                # The same task can be resumed after the content capability was
                # revoked; the operation ID and account determine its owner.
                result = self.server.migration.resume(data.get('claim_id'), self.server.points.user_actor(account['user_id']),
                                                       include_wallet=data.get('include_wallet', True), include_conversations=data.get('include_conversations', True))
            else:
                result = self.server.migration.claim(source, self.server.points.user_actor(account['user_id']), data.get('claim_id'),
                                                     include_wallet=data.get('include_wallet', True), include_conversations=data.get('include_conversations', True))
            self._json(200, {'ok': True, **result}); return True
        return False

    def _billing_admin_post(self, path, data):
        admin = self._sensitive_admin(data)
        if not admin: return True
        operation = self._operation_id(data)
        values = {k: v for k, v in data.items() if k not in {'otp_code', 'operation_id'}}
        store, aid = self.server.billing_admin, admin['admin_id']
        def fields(names):
            if set(values) - set(names): raise PointsError('管理操作字段无效')
        if path == '/api/admin/billing/drafts':
            fields({'price'})
            result = store.draft_price(values.get('price'), aid, operation)
        elif path == '/api/admin/billing/prices':
            # Compatibility endpoint still uses an audited draft and publish;
            # it cannot bypass validation or modify a historical price.
            draft = store.draft_price(values, aid, canonical_digest(self.service.security_key, [operation, 'draft']))
            result = store.publish_draft(draft['draft_id'], aid, operation)
        elif path == '/api/admin/billing/policy':
            result = store.configure_policy(values, aid, operation)
        elif path == '/api/admin/billing/rebuild-projection':
            fields({'reason'})
            result = store.rebuild_projection(aid, operation, values.get('reason'))
        elif path == '/api/admin/billing/refunds':
            fields({'request_id', 'reason'})
            result = store.refund(values.get('request_id'), aid, operation, values.get('reason'))
        elif path == '/api/admin/billing/adjustments':
            fields({'user_id', 'points', 'reason', 'expires_at'})
            uid = values.get('user_id')
            if type(uid) is not int or not 1 <= uid < 10**18:
                raise PointsError('目标账户ID无效')
            with self._http_state() as conn:
                if not conn.execute('SELECT 1 FROM users WHERE user_id=?', (uid,)).fetchone():
                    raise PointsError('目标账户不存在', 404)
            result = store.adjust(self.server.points.user_actor(uid), values.get('points'), aid, operation,
                                  values.get('reason'), values.get('expires_at'))
        elif path == '/api/admin/billing/reconcile':
            fields({'request_id', 'call_id', 'usage', 'provider_request_id', 'reason'})
            result = store.reconcile(values.get('request_id'), values.get('call_id'), values.get('usage'),
                                     values.get('provider_request_id'), aid, operation, values.get('reason'))
        elif path == '/api/admin/billing/reconcile-service':
            fields({'request_id', 'call_id', 'actual_cost_cny', 'provider_request_id', 'reason'})
            result = store.reconcile_service(values.get('request_id'), values.get('call_id'), values.get('actual_cost_cny'),
                                             aid, operation, values.get('reason'), provider_request_id=values.get('provider_request_id'))
        else:
            match = re.fullmatch(r'/api/admin/billing/(drafts|prices|adjustments)/([a-f0-9]{32})/(publish|disable|approve)', path)
            if not match:
                self._json(404, {'ok': False, 'error': '未找到管理操作'}); return True
            group, identity, action = match.groups()
            if (group, action) == ('drafts', 'publish'):
                fields(set())
                result = store.publish_draft(identity, aid, operation)
            elif (group, action) == ('prices', 'disable'):
                fields({'reason'})
                result = store.disable_price(identity, aid, operation, values.get('reason'))
            elif (group, action) == ('adjustments', 'approve'):
                fields(set())
                result = store.approve_adjustment(identity, aid, operation)
            else:
                self._json(404, {'ok': False, 'error': '未找到管理操作'}); return True
        self._json(200, {'ok': True, **result, 'result': result})
        return True


class _ChallengeSent(Exception):
    """The structured verification response has already been sent."""
