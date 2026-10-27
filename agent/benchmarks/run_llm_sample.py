#!/usr/bin/env python3
"""Run a bounded, stratified real-LLM sample from the large local benchmark.

The 1,200-question benchmark stays offline and deterministic.  This script is
the explicit remote-model layer: it calls the configured provider only for a
small reproducible sample and records complete answers plus tool traces.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys
import time
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.runtime import Agent, AgentConfig  # noqa: E402
from agent.benchmarks.run_benchmark import load_questions  # noqa: E402


def select_questions(rows: List[Dict], per_cell: int = 2) -> List[Dict]:
    cells = {}
    for row in rows:
        cells.setdefault((row.get("mode"), row.get("complexity")), []).append(row)
    selected = []
    for key in sorted(cells):
        # questions.jsonl was generated with a deterministic shuffle; taking
        # the first rows keeps this sample reproducible without extra randomness.
        selected.extend(cells[key][:per_cell])
    return selected


def future_years(text: str, cutoff: Optional[str]) -> List[int]:
    if not cutoff:
        return []
    cutoff_year = int(cutoff[:4])
    return sorted({int(y) for y in re.findall(r"(?<!\d)(1[89]\d{2}|20\d{2})(?!\d)", text) if int(y) > cutoff_year})


def run(question_path: Path, out_dir: Path, per_cell: int = 2) -> Dict:
    rows = select_questions(load_questions(question_path), per_cell)
    config = AgentConfig.load()
    agent = Agent(config)
    out_dir.mkdir(parents=True, exist_ok=True)
    result_path = out_dir / "llm_sample_results.jsonl"
    results = []
    started = time.monotonic()
    with result_path.open("w", encoding="utf-8") as stream:
        for index, row in enumerate(rows, 1):
            started_one = time.monotonic()
            try:
                # Keep the prompt itself evidence-focused and short; the
                # benchmark question remains unchanged in the record.
                prompt = row["question"] + "\n请尽量简洁，给出证据边界和出处；不要调用网页搜索。"
                response = agent.ask(prompt, row["mode"], row.get("as_of"), show_reasoning=False)
                content = response.get("content", "")
                tool_names = [item.get("name") for item in response.get("tools", [])]
                item = {
                    "id": row["id"], "mode": row["mode"], "complexity": row["complexity"],
                    "category": row.get("category"), "question": row["question"], "as_of": row.get("as_of"),
                    "ok": bool(response.get("ok")), "content": content,
                    "content_chars": len(content), "reasoning_chars": len(response.get("reasoning", "")),
                    "tool_names": tool_names, "tools_count": len(tool_names),
                    "citation_hint": bool(re.search(r"【[A-Za-z][A-Za-z0-9_-]+】|PDF|p\d{4}", content)),
                    "future_years_in_answer": future_years(content, row.get("as_of") if row.get("mode") == "historical" else None),
                    "elapsed_s": round(time.monotonic() - started_one, 3), "error": response.get("error"),
                }
            except Exception as exc:
                item = {"id": row["id"], "mode": row["mode"], "complexity": row["complexity"], "category": row.get("category"), "question": row["question"], "as_of": row.get("as_of"), "ok": False, "content": "", "content_chars": 0, "reasoning_chars": 0, "tool_names": [], "tools_count": 0, "citation_hint": False, "future_years_in_answer": [], "elapsed_s": round(time.monotonic() - started_one, 3), "error": str(exc)}
            stream.write(json.dumps(item, ensure_ascii=False) + "\n")
            stream.flush()
            print(json.dumps({"progress": index, "total": len(rows), "id": item["id"], "ok": item["ok"], "chars": item["content_chars"]}, ensure_ascii=False), flush=True)
    summary = {
        "question_count": len(results) if results else len(rows),
        "sample_cells": len({(r.get("mode"), r.get("complexity")) for r in rows}),
        "per_cell": per_cell,
        "result_file": str(result_path),
        "provider": config.llm.provider, "model": config.llm.model,
        "stream_requested": config.llm.stream, "reasoning_requested": config.llm.reasoning,
        "scope": "本文件是分层真实LLM抽样，不是1200题逐题远程调用；1200题完整结果在同目录的results.jsonl和summary.json。",
    }
    parsed = [json.loads(line) for line in result_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    summary.update({
        "ok_count": sum(1 for r in parsed if r.get("ok")),
        "nonempty_count": sum(1 for r in parsed if r.get("content_chars", 0) > 0),
        "citation_hint_count": sum(1 for r in parsed if r.get("citation_hint")),
        "future_year_warning_count": sum(1 for r in parsed if r.get("future_years_in_answer")),
        "error_count": sum(1 for r in parsed if r.get("error")),
        "tool_usage": Counter(name for r in parsed for name in r.get("tool_names", [])),
        "elapsed_s": round(time.monotonic() - started, 3),
    })
    (out_dir / "llm_sample_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=dict) + "\n", encoding="utf-8")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, default=ROOT / "agent/benchmarks/questions.jsonl")
    parser.add_argument("--out", type=Path, default=ROOT / "data/benchmarks")
    parser.add_argument("--per-cell", type=int, default=2)
    args = parser.parse_args(argv)
    print(json.dumps(run(args.questions, args.out, args.per_cell), ensure_ascii=False, indent=2, default=dict))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
