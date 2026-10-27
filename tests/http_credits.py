"""Real HTTP prerequisites for legacy transport tests after credit authorization.

Only fixture construction injects the random challenge verifier and fictional
prices. Every proof and quote still crosses the same HTTP routes as a browser.
"""
from __future__ import annotations

from http.cookiejar import CookieJar
import json
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener
import uuid


FIXTURE_ENV = {
    'MAO_AUTH_SECRET': 'isolated-legacy-http-test-secret-32-bytes',
    'MAO_PUBLIC_HOSTNAME': '', 'MAO_TRUSTED_PROXIES': '',
    'MAO_TURNSTILE_SITE_KEY': '', 'MAO_TURNSTILE_SECRET': '',
    'MAO_LLM_API_KEY': 'isolated-legacy-model-placeholder',
    'MAO_LLM_BASE_URL': 'https://provider.invalid/v1',
    'MAO_LLM_MODEL': 'legacy-http-fixture-model', 'MAO_LLM_MAX_TOKENS': '100',
}


def seed_credits(server):
    if not server.security.test_mode:
        raise AssertionError('credit HTTP fixtures require an explicitly isolated verifier')
    config = server.agent.config
    server.points.configure({'daily_cash_limit_cny': '1'})
    server.points.publish_price({
        'provider': config.llm.provider, 'gateway': config.llm.base_url, 'model': config.llm.model,
        'currency': 'CNY', 'rates': {key: '0.000001' for key in
                                  ('input_uncached', 'cache_read', 'cache_write', 'billable_output')},
        'adapter': 'openai', 'cache_read_supported': False, 'cache_write_supported': False,
    })
    # Search is also an isolated test double; its cash commitment remains finite.
    server.search_cash_bound = '0.000001'


class CreditsHTTPAdapter:
    def __init__(self, server, base):
        self.server, self.base = server, base
        self.jar = CookieJar()
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(self.jar))
        self.lock = threading.Lock()
        self.now = time.time()
        server.security.clock = lambda: self.now

    @staticmethod
    def decoded(response):
        try:
            return json.loads(response[2])
        except (ValueError, UnicodeDecodeError, TypeError):
            return {}

    def raw(self, path, method='GET', data=None, headers=None, response_hook=None):
        value = data if isinstance(data, bytes) else json.dumps(data).encode() if data is not None else None
        request = Request(self.base + path, data=value, method=method, headers=headers or {})
        try:
            with self.opener.open(request, timeout=5) as response:
                if response_hook:
                    response_hook(dict(response.headers))
                return response.status, dict(response.headers), response.read()
        except HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def prove(self, path, method, data, headers):
        response = self.raw(path, method, data, headers)
        value = self.decoded(response)
        if response[0] == 403 and value.get('code') == 'challenge_required':
            challenge = value['challenge']
            if challenge.get('provider') != 'test' or not challenge.get('test_token'):
                raise AssertionError('legacy tests must never contact a real verification provider')
            verified = self.raw('/api/security/verify', 'POST', {
                'challenge_id': challenge['challenge_id'], 'token': challenge['test_token']}, headers)
            if verified[0] != 200:
                return verified, data
            data = {**data, 'security_proof': self.decoded(verified)['proof']}
            response = self.raw(path, method, data, headers)
        return response, data

    def request(self, path, method='GET', data=None, headers=None, response_hook=None):
        headers = dict(headers or {})
        origin = headers.get('Origin')
        same_origin = not origin or urlsplit(origin).netloc == urlsplit(self.base).netloc
        if method == 'POST' and isinstance(data, dict) and same_origin:
            if path in {'/api/auth/register', '/api/auth/login'}:
                self.raw('/api/guest/bootstrap', 'POST', {}, headers)
                data = {**data, 'operation_id': data.get('operation_id') or uuid.uuid4().hex}
                return self.prove(path, method, data, headers)[0]
            if path == '/api/agent/stream':
                identity = self.raw('/api/auth/me', 'GET', headers=headers)
                user = self.decoded(identity).get('user') or {}
                # Preserve original anonymous-rejection tests; never bootstrap
                # a new visitor on their behalf or skip any authorization check.
                if identity[0] == 200 and not user.get('is_guest'):
                    with self.lock:
                        self.now += 6
                    data = {**data, 'client_request_id': data.get('client_request_id') or uuid.uuid4().hex}
                    quoted, data = self.prove('/api/points/quote', 'POST', data, headers)
                    if quoted[0] != 200:
                        return quoted
                    data = {**data, 'quote_id': self.decoded(quoted)['quote_id']}
        return self.raw(path, method, data, headers, response_hook)
