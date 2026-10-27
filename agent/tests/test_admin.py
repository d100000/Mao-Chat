import json
import base64
import re
import os
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from agent.admin_config import AdminConfigStore
from agent.auth import AuthService
from agent.auth_server import make_server


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mao-admin-")
        self.root = Path(self.tmp.name)
        self.db = self.root / "auth.db"
        environment = patch.dict(os.environ, {"MAO_ADMIN_SECRET_FILE": str(self.root / "secrets.json"), "MAO_AGENT_DB": str(self.db)})
        environment.start()
        self.addCleanup(environment.stop)
        # These tests must never read, replace or delete real provider keys.
        for name, result in (("_keychain_get", ""), ("_keychain_set", False), ("_keychain_delete", None)):
            keychain = patch.object(AdminConfigStore, name, return_value=result)
            keychain.start()
            self.addCleanup(keychain.stop)
        self.auth = AuthService(self.db)
        self.auth.bootstrap_admin("rootadmin", "admin-password-123")

    def tearDown(self):
        self.tmp.cleanup()

    def captcha(self):
        math = self.auth.create_math_challenge()
        expression = re.search(r">(\d+ [+\-×] \d+ = \?)</text>", base64.b64decode(math["image"].split(",", 1)[1]).decode()).group(1)
        left, op, right = expression.split()[:3]; a, b = int(left), int(right); answer = a + b if op == "+" else a - b if op == "-" else a * b
        return {"math_challenge_id": math["challenge_id"], "math_answer": answer}

    def test_admin_login_session_and_user_controls(self):
        login = self.auth.admin_login("rootadmin", "admin-password-123", **self.captcha())
        self.assertTrue(login["ok"])
        self.assertTrue(self.auth.validate_admin_session(login["session_token"])["ok"])
        self.assertTrue(self.auth.admin_logout(login["session_token"])["ok"])
        self.assertFalse(self.auth.validate_admin_session(login["session_token"])["ok"])
        self.assertEqual(self.auth.admin_list_users()["admins"][0]["username"], "rootadmin")

    def test_config_redacts_secret_and_validates(self):
        store = AdminConfigStore(self.db)
        result = store.update({"llm": {"model": "test-model", "api_key": "secret-value", "temperature": 0.4}, "firecrawl": {"limit": 3}, "agent": {"max_steps": 9, "local_limit": 11, "web_limit": 4}}, 1)
        self.assertEqual(result["llm"]["model"], "test-model")
        self.assertTrue(result["llm"]["api_key_set"])
        self.assertEqual(result["agent"]["max_steps"], 9)
        self.assertNotIn("secret-value", json.dumps(result, ensure_ascii=False))
        loaded = store.load_agent_config()
        self.assertEqual(loaded.llm.model, "test-model")
        self.assertEqual(loaded.max_steps, 9)
        with self.assertRaises(ValueError):
            store.update({"llm": {"temperature": 3}}, 1)

    def test_keychain_update_removes_stale_sidecar_fallback(self):
        store = AdminConfigStore(self.db)
        store.secret_path.write_text(json.dumps({"llm_api_key": "old-key", "firecrawl_api_key": "keep-key"}), encoding="utf-8")
        with patch.object(store, "_keychain_set", return_value=True):
            store._set_secret("llm_api_key", "new-key", "llm-service")
        fallback = json.loads(store.secret_path.read_text(encoding="utf-8"))
        self.assertNotIn("llm_api_key", fallback)
        self.assertEqual(fallback["firecrawl_api_key"], "keep-key")

    def test_provider_keys_remain_in_temporary_store_without_system_calls(self):
        store = AdminConfigStore(self.db)
        with patch('agent.admin_config.subprocess.run', side_effect=AssertionError('test touched system Keychain')):
            store.update({'llm': {'api_key': 'isolated-test-placeholder'}})
        self.assertEqual(store._read_secrets()['llm_api_key'], 'isolated-test-placeholder')
        self.assertEqual(store.secret_path.parent, self.root)
        self.assertEqual(store.secret_path.stat().st_mode & 0o777, 0o600)

    def test_http_admin_routes(self):
        server = make_server("127.0.0.1", 0, self.db)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        def request(path, method="GET", data=None, headers=None):
            body = None if data is None else json.dumps(data).encode()
            req = Request(base + path, data=body, method=method, headers=headers or {})
            try:
                with urlopen(req, timeout=3) as response: return response.status, dict(response.headers), response.read()
            except HTTPError as exc: return exc.code, dict(exc.headers), exc.read()
        def json_request(path, method="GET", data=None, headers=None):
            status, response_headers, body = request(path, method, data, headers)
            return status, response_headers, json.loads(body.decode("utf-8"))
        try:
            status, _, value = json_request("/api/admin/me")
            self.assertEqual(status, 401)
            math = self.auth.create_math_challenge()
            expression = re.search(r">(\d+ [+\-×] \d+ = \?)</text>", base64.b64decode(math["image"].split(",", 1)[1]).decode()).group(1)
            left, op, right = expression.split()[:3]; a, b = int(left), int(right); answer = a + b if op == "+" else a - b if op == "-" else a * b
            login_payload = {"username": "rootadmin", "password": "admin-password-123", "math_challenge_id": math["challenge_id"], "math_answer": answer}
            status, headers, value = json_request("/api/admin/login", "POST", login_payload, {"Content-Type": "application/json"})
            self.assertEqual(status, 200); self.assertTrue(value["ok"])
            cookie = headers["Set-Cookie"].split(";", 1)[0]
            status, _, value = json_request("/api/admin/config", headers={"Cookie": cookie})
            self.assertEqual(status, 200); self.assertTrue(value["ok"])
            status, _, value = json_request("/api/admin/config/test", "POST", {"llm": {"temperature": 3}}, {"Content-Type": "application/json", "Cookie": cookie})
            self.assertEqual(status, 400)
            status, _, value = json_request("/api/admin/config/test", "POST", {"llm": {"model": "checked-model"}}, {"Content-Type": "application/json", "Cookie": cookie})
            self.assertEqual(status, 200); self.assertIn("未发起外部连接", value["message"])
            status, _, value = json_request("/api/admin/config", "PUT", {"llm": {"model": "admin-model", "api_key": "top-secret"}}, {"Content-Type": "application/json", "Cookie": cookie})
            self.assertEqual(status, 200); self.assertNotIn("top-secret", json.dumps(value, ensure_ascii=False))
            status, _, value = json_request("/api/admin/users", headers={"Cookie": cookie})
            self.assertEqual(status, 200); self.assertTrue(value["ok"])
            status, _, body = request("/admin-login")
            self.assertEqual(status, 200); self.assertIn("管理员登录".encode("utf-8"), body)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
