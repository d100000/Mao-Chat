#!/usr/bin/env python3
"""Run a large deterministic local retrieval benchmark.

This harness deliberately does not call a language model or Firecrawl.  It
measures the evidence/search layer on every generated question; a separate
stratified sample can be sent to an LLM after this inexpensive gate passes.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.runtime import AgentConfig, LocalKnowledge  # noqa: E402
from agent.index import search as fts_search, trace_event as fts_trace_event, trace_quote as fts_trace_quote  # noqa: E402
from agent.vector import cosine, unpack, vectorize  # noqa: E402


class CachedVectorIndex:
    """Load the small local index once; 1,000 questions must stay cheap."""
    def __init__(self, db_path: Path):
        self.rows = []
        self.records = {}
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            for row in conn.execute("SELECT target_type,target_id,dimension,vector_blob,model FROM embeddings"):
                item = dict(row)
                values = unpack(item["vector_blob"], item["dimension"])
                item["sparse"] = {index: value for index, value in enumerate(values) if value}
                self.rows.append(item)
            for kind, table, key in (("passage", "passages", "passage_id"), ("quote", "quotes", "quote_id"),
                                     ("event", "events", "event_id"), ("philosophy", "philosophy_rules", "rule_id")):
                for record in conn.execute("SELECT * FROM %s" % table):
                    self.records[(kind, str(record[key]))] = dict(record)

    def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        q_values = vectorize(query)
        q = {index: value for index, value in enumerate(q_values) if value}
        scored = []
        for row in self.rows:
            score = sum(value * row["sparse"].get(index, 0.0) for index, value in q.items())
            scored.append((score, row))
        scored.sort(key=lambda pair: (-pair[0], pair[1]["target_type"], pair[1]["target_id"]))
        output = []
        for score, row in scored[:limit]:
            record = dict(self.records.get((row["target_type"], str(row["target_id"])), {}))
            record.update({"result_type": row["target_type"], "vector_score": score, "embedding_model": row["model"]})
            output.append(record)
        return output


class BenchmarkKnowledge:
    """Use FTS directly for the batch; avoid reopening/scanning vectors per row."""
    def __init__(self, db_path: Path, corpus_db_path: Path):
        self.db_path = db_path
        self.corpus_db_path = corpus_db_path
        self.curated = LocalKnowledge(db_path, corpus_db_path)

    def search(self, query, mode="research", as_of=None, limit=5):
        if mode == "historical" and as_of:
            return self.curated.search(query, mode, as_of, limit)
        return {"query": query, "mode": mode, "as_of": as_of,
                "results": fts_search(self.db_path, query, "all", limit), "untrusted": False}

    def trace_quote(self, text, limit=5):
        return {"query": text, "results": fts_trace_quote(self.db_path, text, limit), "status": "fts"}

    def trace_event(self, text, limit=5):
        return {"query": text, "results": fts_trace_event(self.db_path, text, limit), "status": "fts"}

    def trace_quote_historical(self, text, as_of, limit=5):
        return self.curated.trace_quote_historical(text, as_of, limit)

    def trace_event_historical(self, text, as_of, limit=5):
        return self.curated.trace_event_historical(text, as_of, limit)


def _read_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                yield value


def load_questions(path: Path) -> List[Dict[str, Any]]:
    rows = list(_read_jsonl(path))
    seen = set()
    errors = []
    for row in rows:
        qid = str(row.get("id", ""))
        text = row.get("question") or row.get("prompt")
        mode = row.get("mode", "research")
        if not qid or qid in seen:
            errors.append("重复或缺少问题ID：" + qid)
        if not isinstance(text, str) or not text.strip():
            errors.append("问题文本为空：" + qid)
        if mode not in {"historical", "research", "modern"}:
            errors.append("模式无效：" + qid)
        if mode == "historical" and not row.get("as_of"):
            errors.append("历史题缺少as_of：" + qid)
        seen.add(qid)
    if errors:
        raise ValueError("; ".join(errors[:10]))
    return rows


def _compact(value: Any, max_text: int = 280) -> Any:
    if isinstance(value, dict):
        output = {}
        for key, item in value.items():
            if key in {"page_text", "text", "markdown", "content"} and isinstance(item, str) and len(item) > max_text:
                output[key] = item[:max_text] + "…"
            else:
                output[key] = _compact(item, max_text)
        return output
    if isinstance(value, list):
        return [_compact(item, max_text) for item in value[:8]]
    return value


def _result_ids(results: List[Dict[str, Any]]) -> List[str]:
    ids = []
    for result in results:
        for key in ("evidence_id", "id", "quote_id", "event_id", "rule_id"):
            if result.get(key):
                ids.append(str(result[key]))
                break
    return ids


def _future_violation(results: List[Dict[str, Any]], cutoff: Optional[str]) -> List[str]:
    if not cutoff:
        return []
    violations = []
    for result in results:
        record_type = result.get("record_type") or result.get("result_type")
        if record_type == "evidence_card":
            keys = ("earliest_content_date", "known_to_character_from")
        elif record_type == "quote" or result.get("quote_id"):
            keys = ("earliest_wording_date",)
        elif record_type == "event" or result.get("event_id"):
            keys = ("start_date", "end_date")
        else:
            keys = ("earliest_content_date", "known_to_character_from", "start_date", "end_date")
        for key in keys:
            value = result.get(key)
            if isinstance(value, str) and len(value) >= 10 and value[:10] > cutoff:
                violations.append(str(result.get("evidence_id") or result.get("quote_id") or result.get("event_id") or key))
                break
    return violations


def _expected_empty(row: Dict[str, Any]) -> bool:
    return bool(row.get("expected_empty") or row.get("expect_no_result") or row.get("expect_no_evidence"))


def evaluate_question(knowledge: LocalKnowledge, db_path: Path, row: Dict[str, Any], limit: int = 5, vector_index: Optional[CachedVectorIndex] = None) -> Dict[str, Any]:
    qid = str(row["id"])
    question = str(row.get("question") or row.get("prompt"))
    mode = row.get("mode", "research")
    as_of = row.get("as_of")
    search_query = str(row.get("search_query") or row.get("retrieval_query") or question)
    start = time.monotonic()
    local = knowledge.search(search_query, mode=mode, as_of=as_of, limit=limit)
    local_results = local.get("results") if isinstance(local, dict) else []
    if not isinstance(local_results, list):
        local_results = []
    vector_results = []
    vector_error = None
    try:
        # Vector retrieval is independently measured even for historical
        # questions, but its hits are never treated as history-safe evidence.
        vector_results = vector_index.search(search_query, limit) if vector_index else []
    except Exception as exc:
        vector_error = str(exc)
    quote_results = []
    event_results = []
    if row.get("quote_query") or row.get("category") in {"quote", "quote_trace", "adversarial_quote"}:
        quote_text = str(row.get("quote_query") or search_query)
        if mode == "historical" and as_of and hasattr(knowledge, "trace_quote_historical"):
            quote = knowledge.trace_quote_historical(quote_text, as_of, limit)
        else:
            quote = knowledge.trace_quote(quote_text, limit)
        quote_results = quote.get("results", []) if isinstance(quote, dict) else []
    if row.get("event_query") or row.get("category") in {"event", "event_trace", "historical_event"}:
        event_text = str(row.get("event_query") or search_query)
        if mode == "historical" and as_of and hasattr(knowledge, "trace_event_historical"):
            event = knowledge.trace_event_historical(event_text, as_of, limit)
        else:
            event = knowledge.trace_event(event_text, limit)
        event_results = event.get("results", []) if isinstance(event, dict) else []
    all_results = list(local_results) + list(vector_results) + list(quote_results) + list(event_results)
    future = _future_violation(local_results + quote_results + event_results, as_of if mode == "historical" else None)
    expected_empty = _expected_empty(row)
    authorized_results = (local_results + quote_results + event_results) if mode == "historical" else all_results
    retrieval_ok = bool(authorized_results) if not expected_empty else not bool(authorized_results)
    # A non-empty result is not enough for historical success: the source must
    # be an allowed, dated curated result, or the answer layer must label it as
    # an unreviewed candidate.
    history_safe = not future
    if mode == "historical":
        history_safe = history_safe and all(result.get("source") in {"curated_local", "local"} for result in local_results)
    passed = retrieval_ok and history_safe and vector_error is None
    elapsed = time.monotonic() - start
    return {
        "id": qid,
        "question": question,
        "mode": mode,
        "as_of": as_of,
        "category": row.get("category"),
        "complexity": row.get("complexity"),
        "expected_empty": expected_empty,
        "retrieval_ok": retrieval_ok,
        "history_safe": history_safe,
        "passed": passed,
        "future_violations": future,
        "vector_error": vector_error,
        "result_counts": {
            "local": len(local_results),
            "vector": len(vector_results),
            "quotes": len(quote_results),
            "events": len(event_results),
        },
        "result_ids": {
            "local": _result_ids(local_results),
            "vector": _result_ids(vector_results),
            "quotes": _result_ids(quote_results),
            "events": _result_ids(event_results),
        },
        "retrieval_ms": round(elapsed * 1000, 3),
        "samples": {
            "local": _compact(local_results[:2]),
            "vector": _compact(vector_results[:2]),
            "quotes": _compact(quote_results[:2]),
            "events": _compact(event_results[:2]),
        },
    }


def summarize(rows: List[Dict[str, Any]], question_sha256: str, elapsed_s: float) -> Dict[str, Any]:
    def rate(key: str) -> float:
        return round(sum(1 for row in rows if row.get(key)) / len(rows), 4) if rows else 0.0
    grouped = defaultdict(list)
    for row in rows:
        grouped["mode:" + str(row.get("mode"))].append(row)
        grouped["complexity:" + str(row.get("complexity"))].append(row)
        grouped["category:" + str(row.get("category"))].append(row)
    groups = {}
    for name, values in sorted(grouped.items()):
        groups[name] = {
            "total": len(values),
            "passed": sum(1 for value in values if value.get("passed")),
            "pass_rate": round(sum(1 for value in values if value.get("passed")) / len(values), 4),
            "retrieval_rate": round(sum(1 for value in values if value.get("retrieval_ok")) / len(values), 4),
            "history_safe_rate": round(sum(1 for value in values if value.get("history_safe")) / len(values), 4),
        }
    return {
        "schema_version": 1,
        "question_count": len(rows),
        "question_sha256": question_sha256,
        "elapsed_s": round(elapsed_s, 3),
        "passed": sum(1 for row in rows if row.get("passed")),
        "failed": sum(1 for row in rows if not row.get("passed")),
        "pass_rate": rate("passed"),
        "retrieval_rate": rate("retrieval_ok"),
        "history_safe_rate": rate("history_safe"),
        "vector_error_count": sum(1 for row in rows if row.get("vector_error")),
        "future_violation_count": sum(1 for row in rows if row.get("future_violations")),
        "median_retrieval_ms": sorted(row["retrieval_ms"] for row in rows)[len(rows) // 2] if rows else None,
        "groups": groups,
        "scope": "本报告只验证本地检索、向量索引、语录/事件追溯、历史截止点与结果结构；不代表LLM语言回答已经逐题生成或历史真值已被自动证明。",
    }


def write_markdown(path: Path, summary: Dict[str, Any], failures: List[Dict[str, Any]]) -> None:
    lines = ["# 本地 Agent 1000 题离线检索评测", "", f"- 问题数：{summary['question_count']}", f"- 通过：{summary['passed']}", f"- 失败：{summary['failed']}", f"- 检索命中率：{summary['retrieval_rate']:.2%}", f"- 历史安全率：{summary['history_safe_rate']:.2%}", f"- 总耗时：{summary['elapsed_s']} 秒", "", "## 范围", "", summary["scope"], "", "## 分组统计", "", "| 分组 | 总数 | 通过率 | 检索率 | 历史安全率 |", "|---|---:|---:|---:|---:|"]
    for name, value in summary.get("groups", {}).items():
        lines.append(f"| {name} | {value['total']} | {value['pass_rate']:.2%} | {value['retrieval_rate']:.2%} | {value['history_safe_rate']:.2%} |")
    lines += ["", "## 失败样本（最多50条）", ""]
    for row in failures[:50]:
        lines.append(f"- `{row['id']}` [{row['mode']}/{row.get('complexity')}/{row.get('category')}] {row['question']}；future={row['future_violations']}；counts={row['result_counts']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(question_path: Path, db_path: Path, out_dir: Path, limit: int = 5, max_questions: Optional[int] = None) -> Dict[str, Any]:
    questions = load_questions(question_path)
    if max_questions is not None:
        questions = questions[:max_questions]
    config = AgentConfig.load()
    knowledge = BenchmarkKnowledge(db_path, config.corpus_db_path)
    vector_index = CachedVectorIndex(db_path)
    digest = hashlib.sha256(question_path.read_bytes()).hexdigest()
    started = time.monotonic()
    rows = [evaluate_question(knowledge, db_path, row, limit, vector_index) for row in questions]
    summary = summarize(rows, digest, time.monotonic() - started)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_markdown(out_dir / "report.md", summary, [row for row in rows if not row.get("passed")])
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, default=ROOT / "agent/benchmarks/questions.jsonl")
    parser.add_argument("--db", type=Path, default=ROOT / "data/mao_agent.db")
    parser.add_argument("--out", type=Path, default=ROOT / "data/benchmarks")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--max-questions", type=int)
    args = parser.parse_args(argv)
    summary = run(args.questions, args.db, args.out, args.limit, args.max_questions)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
