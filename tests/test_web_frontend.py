import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "web" / "index.html"


class WebFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = PAGE.read_text(encoding="utf-8")
        match = re.search(r"<script>([\s\S]*)</script>", cls.html)
        cls.assertion_script = match.group(1) if match else ""

    def test_page_contains_workbench_contract(self):
        self.assertIn("<title>教员 · 对话分析助手</title>", self.html)
        for element_id in ("composer", "question", "messages", "history", "evidence", "modeSelect", "sendButton"):
            self.assertIn('id="%s"' % element_id, self.html)
        for endpoint in ("/api/auth/me", "/api/auth/logout", "/api/agent/stream", "/api/agent/session"):
            self.assertIn(endpoint, self.html)

    def test_page_has_responsive_and_reduced_motion_rules(self):
        self.assertIn("@media (max-width: 960px)", self.html)
        self.assertIn("@media (max-width: 680px)", self.html)
        self.assertIn("prefers-reduced-motion", self.html)
        self.assertIn("evidence.open", self.html)
        self.assertIn("evidence-hidden", self.html)

    def test_stream_and_history_guards_are_present(self):
        self.assertIn("AbortController", self.assertion_script)
        self.assertIn("response.body", self.assertion_script)
        self.assertIn("历史模式需要先选择截止日期", self.assertion_script)
        self.assertIn("readConversation", self.assertion_script)
        self.assertIn("/api/agent/session?thread_id=", self.assertion_script)
        self.assertIn("aria-pressed", self.html)
        self.assertIn("Boolean(row.untrusted)", self.assertion_script)
        self.assertIn("candidate?'候选':'已审核'", self.assertion_script)

    @unittest.skipUnless(shutil.which("node"), "Node.js is unavailable")
    def test_sse_parser_flushes_tail_and_propagates_handler_errors(self):
        start = self.assertion_script.index("function parseSSE")
        end = self.assertion_script.index("async function send", start)
        parser = self.assertion_script[start:end].strip()
        code = parser + "\n" + r'''
let seen=[];
parseSSE('data: {"type":"content","text":"尾帧"}', e => seen.push(e), true);
if (seen.length !== 1 || seen[0].text !== '尾帧') process.exit(1);
let propagated=false;
try { parseSSE('data: {"type":"error","error":"x"}', () => { throw new Error('handler'); }, true); }
catch (e) { propagated=e.message === 'handler'; }
if (!propagated) process.exit(2);
'''
        result = subprocess.run(["node", "-e", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js is unavailable")
    def test_embedded_javascript_compiles(self):
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8") as handle:
            handle.write(self.assertion_script)
            handle.flush()
            result = subprocess.run(["node", "--check", handle.name], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
