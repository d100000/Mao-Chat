import importlib.util
import contextlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class PackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.persona = load_script("package_persona_test", ROOT / "scripts/package_persona.py")
        cls.portable = load_script("build_portable_skill_test", ROOT / "scripts/build_portable_skill.py")

    def test_persona_file_collection_excludes_runtime_state_and_secrets(self):
        with tempfile.TemporaryDirectory(prefix="mao-package-") as td:
            root = Path(td)
            data = root / "data"
            data.mkdir()
            (data / "verified_early.jsonl").write_text("{}\n", encoding="utf-8")
            (data / "mao_agent.db").write_bytes(b"secret db")
            (data / "mao_agent.db.auth-secret").write_bytes(b"secret")
            (data / "sessions").mkdir()
            (data / "sessions/example.json").write_text("history", encoding="utf-8")
            (root/'tmp/dialogue_runs').mkdir(parents=True)
            (root/'tmp/dialogue_runs/session.json').write_text('private transcript')
            (root/'tests/runs').mkdir(parents=True)
            (root/'tests/runs/session.json').write_text('private test transcript')
            hidden = root / 'corpus/.agent-sessions/u1_main.json'
            hidden.parent.mkdir(parents=True)
            hidden.write_text('private HTTP transcript')
            self.assertFalse(self.portable.is_safe_file(hidden))
            files = self.persona.collect_files(root)
            relative = {path.relative_to(root).as_posix() for path in files if path.is_file()}
            self.assertIn("data/verified_early.jsonl", relative)
            self.assertNotIn("data/mao_agent.db", relative)
            self.assertNotIn("data/mao_agent.db.auth-secret", relative)
            self.assertNotIn("data/sessions/example.json", relative)
            self.assertNotIn('tmp/dialogue_runs/session.json', relative)
            self.assertNotIn('tests/runs/session.json', relative)
            self.assertNotIn('corpus/.agent-sessions/u1_main.json', relative)

    def test_portable_copy_filter_excludes_sensitive_files(self):
        with tempfile.TemporaryDirectory(prefix="mao-portable-") as td:
            root = Path(td)
            safe = root / "data/verified_late.jsonl"
            safe.parent.mkdir(parents=True)
            safe.write_text("{}\n", encoding="utf-8")
            self.assertTrue(self.portable.is_safe_file(safe, data_only=True))
            for name in ("mao_agent.db", "mao_agent.db-wal", "mao_agent.db.auth-secret", "x.provider-secrets"):
                path = safe.parent / name
                path.write_text("secret", encoding="utf-8")
                self.assertFalse(self.portable.is_safe_file(path, data_only=True))
            for name in ['config.json','.env.local','provider.provider-secrets.tmp','accounts.admin-totp','accounts.admin-totp.backup']:
                path=root/name;path.write_text('private local value')
                self.assertFalse(self.portable.is_safe_file(path))
                self.assertFalse(self.persona.is_safe_file(path, root))
            config=root/'accounts.db.security-config'/'version.json'
            config.parent.mkdir();config.write_text('private verifier configuration')
            self.assertFalse(self.portable.is_safe_file(config))
            self.assertFalse(self.persona.is_safe_file(config,root))

    def test_finalize_refilters_existing_bundle_for_manifest_checksums_archive_and_statistics(self):
        with tempfile.TemporaryDirectory(prefix='mao-finalize-package-') as td:
            root = Path(td)
            bundle = root / 'isolated-bundle'
            skill = bundle / 'mao-primary-sources'
            skill.mkdir(parents=True)
            safe = {
                skill / 'README.md': 'isolated public documentation',
                skill / 'references/evidence.jsonl': '{}\n',
                bundle / '使用说明.md': '隔离测试的公开说明',
            }
            for path, text in safe.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding='utf-8')
            private = [
                skill / 'data/accounts.db', skill / 'data/accounts.db-wal', skill / 'data/accounts.db-shm',
                skill / 'data/accounts.db.security-config/version.json',
                skill / 'data/accounts.admin-totp', skill / 'data/accounts.admin-totp.backup',
                skill / 'data/accounts.db.auth-secret', skill / 'data/provider.provider-secrets',
                skill / 'data/sessions/u1.json', skill / 'corpus/.agent-sessions/u2.json',
                skill / 'config.json', bundle / 'root.db.security-config/version.json',
                bundle / 'private.db', bundle / 'root.admin-totp', bundle / '.env.local',
            ]
            for index, path in enumerate(private):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('PRIVATE-FIXTURE-' + str(index), encoding='utf-8')
            # Simulate runtime files added after the initial safe copy, and
            # stale release metadata that the second finalize must replace.
            (skill / 'package-manifest.json').write_text('{"stale":"PRIVATE-FIXTURE"}')
            (bundle / 'SHA256SUMS.txt').write_text('stale PRIVATE-FIXTURE')
            captured = io.StringIO()
            with patch.object(self.portable, 'ROOT', root), patch.object(self.portable, 'BUNDLE', bundle), \
                 patch.object(self.portable, 'SKILL', skill), contextlib.redirect_stdout(captured):
                self.portable.finalize()
            result = json.loads(captured.getvalue())
            manifest_text = (skill / 'package-manifest.json').read_text(encoding='utf-8')
            manifest = json.loads(manifest_text)
            self.assertEqual({item['path'] for item in manifest['files']}, {'README.md', 'references/evidence.jsonl'})
            checksum_text = (bundle / 'SHA256SUMS.txt').read_text(encoding='utf-8')
            checksums = {line.split('  ', 1)[1] for line in checksum_text.splitlines()}
            expected = {path.relative_to(bundle).as_posix() for path in safe}
            expected.update({'mao-primary-sources/package-manifest.json', 'VERSION'})
            self.assertEqual(checksums, expected)
            with zipfile.ZipFile(root / 'isolated-bundle.zip') as archive:
                entries = archive.infolist()
                names = {str(Path(item.filename).relative_to(bundle.name)) for item in entries}
                self.assertEqual(names, expected | {'SHA256SUMS.txt'})
                self.assertEqual(result['files'], len(entries))
                self.assertEqual(result['uncompressed_bytes'], sum(item.file_size for item in entries))
                self.assertFalse(any(b'PRIVATE-FIXTURE' in archive.read(item.filename) for item in entries))
            self.assertNotIn('PRIVATE-FIXTURE', manifest_text)
            self.assertNotIn('PRIVATE-FIXTURE', checksum_text)
            # Packaging excludes local state without deleting the operator's
            # files or inspecting any real generated bundle.
            self.assertTrue(all(path.exists() for path in private))


if __name__ == "__main__":
    unittest.main()
