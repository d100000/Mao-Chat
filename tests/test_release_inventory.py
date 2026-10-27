from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts.check_release import contains_secret, contains_private_path, forbidden, inspect


class ReleaseInventoryTests(unittest.TestCase):
    def test_runtime_state_is_not_publishable(self):
        for value in ['tmp/report.json','tests/runs/a/session.json','sources/book.pdf','data/sessions/a.json','data/.agent-sessions/u1_main.json','data/real_runs/a.json','agent/config.json','data/x.db','data/x.db.auth-secret','x.provider-secrets','x.zip','.env.local']:
            self.assertTrue(forbidden(Path(value)), value)
        for value in ['agent/config.example.json','.env.example','data/verified_early.jsonl','docs/教员产品优化方案.docx']:
            self.assertFalse(forbidden(Path(value)), value)

    def test_detects_credential_shapes_without_logging_values(self):
        self.assertTrue(contains_secret(('sk-' + 'x' * 48).encode()))
        self.assertTrue(contains_secret(('fc-' + 'a' * 32).encode()))
        self.assertTrue(contains_secret(b'-----BEGIN ' + b'OPENSSH PRIVATE KEY-----'))
        self.assertFalse(contains_secret(b'MAO_LLM_API_KEY=replace-me'))

    def test_detects_local_paths_in_text_and_document_xml(self):
        local = b'/' + b'Users/' + b'test-machine/' + b'docs/plan.docx'
        self.assertTrue(contains_private_path(b'<w:t>' + local + b'</w:t>'))
        self.assertTrue(contains_private_path(b'/' + b'home/' + b'ci-runner/file'))
        self.assertTrue(contains_private_path(b'C:' + bytes([92]) + b'Users' + bytes([92]) + b'test-machine' + bytes([92]) + b'doc'))
        self.assertFalse(contains_private_path(b'docs/plan.docx'))

    def test_scans_the_staged_blob_even_if_the_working_file_was_cleaned(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            subprocess.run(['git','init','-q'],cwd=root,check=True)
            path = root/'README.md'
            path.write_text('sk-' + 'x' * 48)
            subprocess.run(['git','add','README.md'],cwd=root,check=True)
            path.write_text('safe replacement')
            findings, count = inspect(root)
            self.assertEqual(count, 1)
            self.assertTrue(findings)
            self.assertNotIn('x' * 48, str(findings))
            subprocess.run(['git','add','README.md'],cwd=root,check=True)
            self.assertEqual(inspect(root), ([], 1))


if __name__ == '__main__': unittest.main()
