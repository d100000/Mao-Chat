import importlib.util
import contextlib
import io
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ToolingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ocr = load_module("run_ocr_test", ROOT / "scripts/run_ocr.py")

    def test_ocr_source_directory_and_positive_limits_are_configurable(self):
        args = self.ocr.build_parser().parse_args(["--source-dir", "/tmp/pdf-source", "--workers", "2", "--edge", "1200", "--sample-only"])
        self.assertEqual(args.source_dir, Path("/tmp/pdf-source"))
        self.assertEqual(args.workers, 2)
        self.assertEqual(args.edge, 1200)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.ocr.build_parser().parse_args(["--workers", "0"])

    def test_verify_corpus_resolves_relative_source_files(self):
        source = (ROOT / "scripts/verify_corpus.py").read_text(encoding="utf-8")
        self.assertIn("if not source_path.is_absolute(): source_path=root/source_path", source)



class LiveAcceptanceReportTests(unittest.TestCase):
    def test_failed_startup_replaces_a_previous_success_report(self):
        import json,sys,tempfile,io
        from contextlib import redirect_stderr
        from unittest.mock import patch
        from scripts import live_plan_check
        with tempfile.TemporaryDirectory() as td:
            report=Path(td)/'report.json'
            report.write_text(json.dumps({'passed':5,'failed':0,'answers':['stale success']}))
            with redirect_stderr(io.StringIO()),patch.object(sys,'argv',['live_plan_check.py','--run','--output',str(report)]),patch.object(live_plan_check,'make_live_server',side_effect=ValueError('fixture startup failure')):
                self.assertEqual(live_plan_check.main(),1)
            result=json.loads(report.read_text())
            self.assertFalse(result['running'])
            self.assertEqual((result['passed'],result['failed']),(0,1))
            self.assertEqual(result['answers'],[])

if __name__ == "__main__":
    unittest.main()
