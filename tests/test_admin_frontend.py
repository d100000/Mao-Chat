import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "web" / "admin.html"
LOGIN_PAGE = ROOT / "web" / "admin-login.html"


class AdminFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = PAGE.read_text(encoding="utf-8")
        match = re.search(r"<script>([\s\S]*)</script>", cls.html)
        cls.script = match.group(1) if match else ""

    def test_admin_contract_and_sensitive_field_rules(self):
        for element_id in ("llmBaseUrl", "llmModel", "llmKey", "fireBaseUrl", "fireKey", "maxSteps", "saveConfig", "testConfig", "usersBody"):
            self.assertIn('id="%s"' % element_id, self.html)
        for endpoint in ("/api/admin/config", "/api/admin/users", "/api/admin/config/test", "/api/admin/logout"):
            self.assertIn(endpoint, self.html)
        self.assertIn('type="password"', self.html)
        self.assertIn("已配置的密钥不会回显", self.html)
        self.assertNotIn("sk-", self.html)
        self.assertNotIn("fc-", self.html)

    def test_admin_responsive_and_accessibility_rules(self):
        self.assertIn("@media(max-width:900px)", self.html)
        self.assertIn("prefers-reduced-motion", self.html)
        self.assertIn('role="status"', self.html)
        self.assertIn('aria-label="管理导航"', self.html)
        self.assertIn("fireConfigured", self.script)
        for label in ("开启模型思考", "开启流式输出", "历史模式证据门"):
            self.assertIn('aria-label="%s"' % label, self.html)

    @unittest.skipUnless(shutil.which("node"), "Node.js is unavailable")
    def test_embedded_javascript_compiles(self):
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8") as handle:
            handle.write(self.script)
            handle.flush()
            result = subprocess.run(["node", "--check", handle.name], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_admin_login_page_contract_and_javascript(self):
        html = LOGIN_PAGE.read_text(encoding="utf-8")
        for element_id in ("form", "username", "password", "math", "submit"):
            self.assertIn('id="%s"' % element_id, html)
        for endpoint in ("/api/auth/captcha", "/api/admin/login"):
            self.assertIn(endpoint, html)
        self.assertIn('type="password"', html)
        self.assertIn('aria-label="验证码答案"', html)
        self.assertIn("safeNext", html)
        self.assertIn("value.includes('\\\\')", html)
        match = re.search(r"<script>([\s\S]*)</script>", html)
        self.assertIsNotNone(match)
        if shutil.which("node"):
            with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8") as handle:
                handle.write(match.group(1))
                handle.flush()
                result = subprocess.run(["node", "--check", handle.name], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
