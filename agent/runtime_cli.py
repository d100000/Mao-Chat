#!/usr/bin/env python3
"""CLI for the local evidence-first Agent."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path
import sys

from .runtime import Agent, AgentConfig


def _print_event(event, show_reasoning=False):
    kind = event.get("type")
    if kind == "reasoning" and show_reasoning:
        print("\n[思考] " + event.get("text", ""), end="", flush=True)
    elif kind == "content":
        print(event.get("text", ""), end="", flush=True)
    elif kind == "tool":
        print("\n[工具] " + event.get("name", ""), file=sys.stderr)
    elif kind == "error":
        print("\n[错误] " + event.get("error", ""), file=sys.stderr)
    elif kind == "done":
        print()


def _config_path(args):
    return Path(args.config).expanduser() if getattr(args, "config", None) else None


def main(argv=None):
    parser = argparse.ArgumentParser(description="本地毛泽东史料问答 Agent")
    parser.add_argument("--config", help="JSON 配置文件路径")
    sub = parser.add_subparsers(dest="command", required=True)
    ask = sub.add_parser("ask", help="对话解惑与史料问答")
    ask.add_argument("question")
    ask.add_argument("--mode", choices=["consult", "historical", "research", "modern"], default="consult")
    ask.add_argument("--as-of", help="historical模式截止日期 YYYY-MM-DD")
    ask.add_argument("--show-reasoning", action="store_true")
    ask.add_argument("--json", action="store_true", help="结束后输出结构化JSON而不是直接文本")
    ask.add_argument("--session", help="本地多轮会话ID；只保存用户问题和最终回答")
    ask.add_argument("--unsafe-stream", action="store_true", help="直接显示模型原始增量；会跳过最终证据门，仅用于调试")
    search = sub.add_parser("search", help="本地搜索")
    search.add_argument("query")
    search.add_argument("--mode", default="research")
    search.add_argument("--as-of")
    search.add_argument("--limit", type=int, default=8)
    search.add_argument("--hybrid", dest="hybrid", action="store_true", default=True)
    search.add_argument("--no-hybrid", dest="hybrid", action="store_false")
    quote = sub.add_parser("trace-quote", help="追溯语录")
    quote.add_argument("text")
    quote.add_argument("--limit", type=int, default=8)
    event = sub.add_parser("trace-event", help="追溯事件")
    event.add_argument("text")
    event.add_argument("--limit", type=int, default=8)
    web = sub.add_parser("web-search", help="使用 Firecrawl 搜索公开网页（结果标为不可信候选）")
    web.add_argument("query")
    web.add_argument("--limit", type=int, default=5)
    web.add_argument("--full", action="store_true", help="输出完整网页Markdown；默认只输出摘要")
    cfg = sub.add_parser("config", help="查看或复制配置模板")
    cfg.add_argument("action", choices=["show", "init"])
    doctor = sub.add_parser("doctor", help="运行本地能力检查")
    doctor.add_argument("--no-llm", action="store_true")
    args = parser.parse_args(argv)
    config_path = _config_path(args)
    if args.command == "config":
        target = config_path or Path(__file__).resolve().parent / "config.json"
        if args.action == "init":
            example = Path(__file__).resolve().parent / "config.example.json"
            if target.exists():
                raise SystemExit("配置已存在，未覆盖：" + str(target))
            target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
            print(json.dumps({"created": str(target), "notice": "密钥只从环境变量读取"}, ensure_ascii=False, indent=2))
        else:
            loaded = AgentConfig.load(target)
            try:
                llm_set = bool(loaded.llm.api_key())
            except Exception:
                llm_set = False
            try:
                firecrawl_set = bool(loaded.firecrawl.api_key())
            except Exception:
                firecrawl_set = False
            print(json.dumps({"config_path": str(target), "exists": target.exists(), "credentials": {"llm": "<set>" if llm_set else "<missing>", "firecrawl": "<set>" if firecrawl_set else "<missing>"}, "note": "环境变量优先，macOS Keychain可作为回退；密钥值不显示"}, ensure_ascii=False, indent=2))
        return 0
    config = AgentConfig.load(config_path)
    agent = Agent(config)
    if args.command == "ask":
        if args.mode == "historical" and not args.as_of:
            parser.error("historical模式需要 --as-of YYYY-MM-DD")
        history = agent.load_session(args.session) if args.session else []
        if args.session:
            previous = agent.session_metadata(args.session)
            if previous and (previous.get("mode") != args.mode or previous.get("as_of") != args.as_of):
                parser.error("会话模式或历史截止日期已固定；请换新的 --session。")
        if args.json:
            result = agent.ask(args.question, args.mode, args.as_of, history=history, show_reasoning=args.show_reasoning, session_id=args.session)
            if args.session:
                messages = history + [{"role": "user", "content": args.question}, {"role": "assistant", "content": result.get("content", "")}]
                agent.save_session(args.session, messages, {"mode": args.mode, "as_of": args.as_of, "reasoning_enabled": config.llm.reasoning, "analysis": result.get('analysis')})
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        answer_parts = []
        analysis = None
        if not args.unsafe_stream:
            # The safe default waits for the local evidence gate, then emits
            # the approved answer in small chunks.  Raw token streaming is
            # available only as an explicit debugging escape hatch.
            result = agent.ask(args.question, args.mode, args.as_of, history=history, show_reasoning=args.show_reasoning, session_id=args.session)
            if args.show_reasoning and result.get("reasoning"):
                print("\n[思考] " + result["reasoning"], end="", flush=True)
            answer = result.get("content", "")
            analysis = result.get('analysis')
            answer_parts.append(answer)
            for offset in range(0, len(answer), 96):
                print(answer[offset:offset + 96], end="", flush=True)
            print()
        else:
            for event_data in agent.stream(args.question, args.mode, args.as_of, history=history, show_reasoning=args.show_reasoning):
                _print_event(event_data, args.show_reasoning)
                if event_data.get("type") == "content":
                    answer_parts.append(event_data.get("text", ""))
                elif event_data.get('type') == 'analysis':
                    analysis = event_data.get('analysis')
        if args.session:
            messages = history + [{"role": "user", "content": args.question}, {"role": "assistant", "content": "".join(answer_parts)}]
            agent.save_session(args.session, messages, {"mode": args.mode, "as_of": args.as_of, "reasoning_enabled": config.llm.reasoning, "analysis": analysis})
        return 0
    if args.command == "search":
        print(json.dumps(agent.knowledge.search(args.query, args.mode, args.as_of, args.limit, args.hybrid), ensure_ascii=False, indent=2))
    elif args.command == "trace-quote":
        print(json.dumps(agent.knowledge.trace_quote(args.text, args.limit), ensure_ascii=False, indent=2))
    elif args.command == "trace-event":
        print(json.dumps(agent.knowledge.trace_event(args.text, args.limit), ensure_ascii=False, indent=2))
    elif args.command == "web-search":
        result = agent._firecrawl_search(args.query, args.limit)
        if not args.full:
            result = dict(result)
            compact = []
            for item in result.get("results", []):
                row = dict(item)
                if isinstance(row.get("markdown"), str) and len(row["markdown"]) > 1200:
                    row["markdown"] = row["markdown"][:1200] + "…[已截断；完整网页内容已保存到本地web_sources]"
                compact.append(row)
            result["results"] = compact
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "doctor":
        if args.no_llm:
            llm_key_present = "skipped"
        else:
            try:
                llm_key_present = bool(config.llm.api_key())
            except Exception:
                llm_key_present = False
        try:
            firecrawl_key_present = bool(config.firecrawl.api_key())
        except Exception:
            firecrawl_key_present = False
        counts = {}
        if config.db_path.exists():
            try:
                with sqlite3.connect(str(config.db_path)) as conn:
                    for table in ("sources", "pages", "passages", "evidence_cards", "quotes", "events", "philosophy_rules", "embeddings", "web_sources"):
                        try:
                            counts[table] = conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                        except sqlite3.Error:
                            counts[table] = None
            except sqlite3.Error:
                counts = {"error": "无法读取SQLite数据库"}
        checks = {"db_path": str(config.db_path), "db_exists": config.db_path.exists(), "db_counts": counts, "corpus_db": str(config.corpus_db_path), "corpus_exists": config.corpus_db_path.exists(), "llm_key_present": llm_key_present, "firecrawl_key_present": firecrawl_key_present, "model": config.llm.model, "stream": config.llm.stream, "reasoning": config.llm.reasoning, "key_source": "environment or macOS Keychain; value never printed", "vector_note": "hashing-charbigram-v1是离线向量基线，不等同语义embedding模型"}
        print(json.dumps(checks, ensure_ascii=False, indent=2))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
