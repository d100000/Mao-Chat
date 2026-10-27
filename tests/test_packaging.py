import importlib.util
import tempfile
import unittest
from pathlib import Path


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
            for name in ['config.json','.env.local','provider.provider-secrets.tmp']:
                path=root/name;path.write_text('private local value')
                self.assertFalse(self.portable.is_safe_file(path))


if __name__ == "__main__":
    unittest.main()
