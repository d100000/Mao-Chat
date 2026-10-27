import json
from pathlib import Path
import tempfile
import unittest

from agent.benchmarks.run_benchmark import load_questions, run
from agent.benchmarks.run_llm_sample import select_questions
from scripts.bootstrap_library import bootstrap


class BenchmarkHarnessTests(unittest.TestCase):
    def test_load_questions_rejects_duplicate_ids(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "q.jsonl"
            path.write_text(json.dumps({"id": "x", "question": "一个问题", "mode": "research"}, ensure_ascii=False) + "\n" + json.dumps({"id": "x", "question": "另一个问题", "mode": "research"}, ensure_ascii=False) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_questions(path)

    def test_small_real_run_writes_results(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as td:
            questions = Path(td) / "q.jsonl"
            questions.write_text(json.dumps({"id": "q1", "question": "论持久战 速胜论", "mode": "research", "complexity": "simple", "category": "local"}, ensure_ascii=False) + "\n", encoding="utf-8")
            db = Path(td) / 'library.db'
            bootstrap(db)
            summary = run(questions, db, Path(td) / "out", max_questions=1)
            self.assertEqual(summary["question_count"], 1)
            self.assertTrue((Path(td) / "out/summary.json").is_file())
            self.assertTrue((Path(td) / "out/results.jsonl").is_file())

    def test_llm_sample_selection_covers_all_mode_complexity_cells(self):
        root = Path(__file__).resolve().parents[2]
        rows = load_questions(root / "agent/benchmarks/questions.jsonl")
        selected = select_questions(rows, per_cell=1)
        self.assertEqual(len(selected), 12)
        self.assertEqual(len({(row["mode"], row["complexity"]) for row in selected}), 12)


if __name__ == "__main__":
    unittest.main()
