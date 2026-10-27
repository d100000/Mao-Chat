import base64
import json
import os
import re
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent.auth_server import make_server


class AuthHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mao-auth-http-")
        db = Path(self.tmp.name) / 'auth.db'
        environment = patch.dict(os.environ, {'MAO_AGENT_DB': str(db), 'MAO_ADMIN_SECRET_FILE': str(db) + '.provider-secrets'})
        environment.start(); self.addCleanup(environment.stop)
        for name, value in (('_keychain_get', ''), ('_keychain_set', False), ('_keychain_delete', None)):
            isolated = patch('agent.admin_config.AdminConfigStore.' + name, return_value=value)
            isolated.start(); self.addCleanup(isolated.stop)
        self.server = make_server("127.0.0.1", 0, Path(self.tmp.name) / "auth.db")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.tmp.cleanup()

    def request(self, path, method="GET", data=None, headers=None):
        body = None if data is None else (data if isinstance(data, bytes) else json.dumps(data).encode())
        req = Request(self.base + path, data=body, method=method, headers=headers or {})
        try:
            with urlopen(req, timeout=3) as response:
                return response.status, dict(response.headers), response.read()
        except HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def json_request(self, path, method="POST", data=None, headers=None):
        status, response_headers, body = self.request(path, method, data, {"Content-Type": "application/json", **(headers or {})})
        return status, response_headers, json.loads(body.decode("utf-8"))

    def challenge(self):
        _, _, math = self.json_request("/api/auth/captcha")
        svg = base64.b64decode(math["image"].split(",", 1)[1]).decode("utf-8")
        expression = re.search(r">(\d+ [+\-×] \d+ = \?)</text>", svg).group(1)
        left, op, right = expression.split()[:3]
        a, b = int(left), int(right)
        answer = a + b if op == "+" else a - b if op == "-" else a * b
        return math, answer

    def test_page_and_svg_routes(self):
        status, headers, body = self.request("/auth")
        self.assertEqual(status, 200)
        self.assertIn("<title>登录工作台 · 教员</title>".encode("utf-8"), body)
        self.assertNotIn(b"slider", body)
        self.assertNotIn(b"session_token", body)
        self.assertIn(b"safeNext", body)
        self.assertIn(b"value.includes('\\\\')", body)
        status, headers, body = self.request("/auth/captcha.svg")
        self.assertEqual(status, 200)
        self.assertTrue(body.lstrip().startswith(b"<svg"))
        self.assertEqual(headers.get("Content-Type"), "image/svg+xml; charset=utf-8")
        status, headers, body = self.request("/app")
        self.assertEqual(status, 200)
        self.assertIn("<title>教员 · 对话分析助手</title>".encode("utf-8"), body)
        self.assertIn(b"/api/agent/stream", body)

    def test_register_login_cookie_me_logout(self):
        math, answer = self.challenge()
        payload = {"username": "web_user", "password": "correct horse battery staple", "math_challenge_id": math["challenge_id"], "math_answer": answer}
        status, _, result = self.json_request("/api/auth/register", data=payload)
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])

        math, answer = self.challenge()
        payload.update({"math_challenge_id": math["challenge_id"], "math_answer": answer})
        status, headers, result = self.json_request("/api/auth/login", data=payload)
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertNotIn("session_token", result)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Lax", headers["Set-Cookie"])

        status, _, result = self.json_request("/api/auth/me", method="GET", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertEqual(result["user"]["username"], "web_user")
        status, _, result = self.json_request("/api/auth/logout", data={}, headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        status, _, result = self.json_request("/api/auth/me", method="GET", headers={"Cookie": cookie})
        self.assertEqual(status, 401)
        self.assertFalse(result["ok"])

    def test_slider_endpoints_removed_and_origin_and_bad_json(self):
        for path in ("/api/auth/slider/start", "/api/auth/slider/complete"):
            status, _, result = self.json_request(path, data={})
            self.assertEqual(status, 404)
            self.assertFalse(result["ok"])

        status, _, result = self.json_request("/api/auth/captcha", headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertFalse(result["ok"])
        status, _, result = self.json_request("/api/auth/register", data=b"not-json")
        self.assertEqual(status, 400)
        self.assertFalse(result["ok"])

    def test_challenge_rate_limit(self):
        for _ in range(30):
            status, _, result = self.json_request("/api/auth/captcha")
            self.assertEqual(status, 200)
            self.assertIn("challenge_id", result)
        status, _, result = self.json_request("/api/auth/captcha")
        self.assertEqual(status, 429)
        self.assertFalse(result["ok"])

    def test_server_periodically_runs_auth_cleanup(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        calls = []
        self.server.auth_service.cleanup = lambda: calls.append(True)
        self.server._last_auth_cleanup = -1e9
        self.server.service_actions()
        self.server.service_actions()
        self.assertEqual(calls, [True])

    def test_session_restores_scope_and_evidence_and_rejects_scope_change(self):
        self.server.auth_service.validate_session = lambda token: {"ok": True, "user": {"user_id": 7, "username": "tester"}}
        agent = self.server.agent
        tools = [{"name": "local_search", "result": {"results": [{"evidence_id": "VE04", "reviewed": 1}]}}]
        agent.save_session("u7_scope", [{"role": "user", "content": "实践论"}], {"mode": "historical", "as_of": "1938-06-30", "tools": tools})
        status, _, result = self.json_request('/api/agent/session?thread_id=scope', method='GET')
        self.assertEqual(status, 200)
        self.assertEqual(result['metadata']['mode'], 'historical')
        self.assertEqual(result['metadata']['as_of'], '1938-06-30')
        self.assertEqual(result['metadata']['actions'], [])
        self.assertEqual(result['tools'], tools)
        for mode, cutoff in [('research', None), ('historical', '1957-01-01')]:
            status, _, result = self.json_request('/api/agent/stream', data={"question": "继续", "mode": mode, "as_of": cutoff, "thread_id": "scope"})
            self.assertEqual(status, 409)

    def test_admin_reload_preserves_custom_storage_paths(self):
        from agent.auth_server import AuthHandler
        from types import SimpleNamespace
        before = self.server.agent.config
        paths = (before.db_path, before.session_dir, before.corpus_db_path)
        AuthHandler._reload_agent(SimpleNamespace(server=self.server))
        config = self.server.agent.config
        self.assertEqual((config.db_path, config.session_dir, config.corpus_db_path), paths)

    def test_agent_stream_requires_session_and_emits_events(self):
        status, _, result = self.json_request("/api/agent/stream", data={"question": "原文是什么？", "mode": "research", "thread_id": "demo"})
        self.assertEqual(status, 401)
        self.assertFalse(result["ok"])

        class FakeAgent:
            def __init__(self):
                self.saved = None
                self.persisted = None
                self.network_enabled = None
            def load_session(self, session_id):
                return []
            def stream(self, question, mode, as_of, history=None, show_reasoning=False, network_enabled=True):
                self.network_enabled = network_enabled
                yield {"type": "tool", "name": "local_search", "result": {"results": [{"source_id": "v1", "anchor": "v1:p1"}]}}
                yield {"type": "content", "text": "已找到原文。"}
                yield {"type": "done", "steps": 1}
            def save_session(self, session_id, messages, metadata):
                self.saved = (session_id, messages, metadata)
            def _persist_run(self, *args):
                self.persisted = args

        self.server.auth_service.validate_session = lambda token: {"ok": True, "user": {"user_id": 7, "username": "tester"}}
        fake = FakeAgent()
        self.server.agent = fake
        status, headers, body = self.request("/api/agent/stream", method="POST", data={"question": "原文是什么？", "mode": "research", "thread_id": "demo"}, headers={"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn('"type":"content"', text)
        self.assertIn("已找到原文", text)
        self.assertIn('"type":"done"', text)
        self.assertEqual(fake.saved[0], "u7_demo")
        self.assertFalse(fake.network_enabled)

        status, _, result = self.json_request("/api/agent/session?thread_id=demo", method="GET")
        self.assertEqual(status, 200)
        self.assertEqual(result["thread_id"], "demo")
        self.assertEqual(result["messages"], [])

        self.server.stream_slots = threading.BoundedSemaphore(0)
        status, _, result = self.json_request("/api/agent/stream", data={"question": "原文是什么？", "mode": "research", "thread_id": "demo"})
        self.assertEqual(status, 429)
        self.assertFalse(result["ok"])

        self.server.stream_slots = threading.BoundedSemaphore(4)
        self.server.auth_service.validate_session = lambda token: {"ok": True, "user": {"user_id": 7, "username": "tester"}}
        status, _, result = self.json_request("/api/agent/stream", data={"question": "原文是什么？", "mode": "historical", "as_of": "2024-99-01", "thread_id": "demo"})
        self.assertEqual(status, 400)
        self.assertFalse(result["ok"])

        status, _, _ = self.request("/api/agent/stream", method="POST", data={"question": "原文是什么？", "mode": "historical", "as_of": "1949-10-01", "thread_id": "demo-historical", "search": True}, headers={"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        self.assertFalse(fake.network_enabled)


if __name__ == "__main__":
    unittest.main()
