#!/usr/bin/env python3
"""CLI for the local database and deterministic retrieval foundation."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

try:
    from .db import connect, default_db_path, default_root, ensure_schema
    from .ingest import ingest, import_relations
    from .index import build_indexes, search, trace_event, trace_page, trace_quote
    from .vector import build_vectors, search_vectors
except ImportError:  # python agent/cli.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from agent.db import connect, default_db_path, default_root, ensure_schema
    from agent.ingest import ingest
    from agent.ingest import import_relations
    from agent.index import build_indexes, search, trace_event, trace_page, trace_quote
    from agent.vector import build_vectors, search_vectors


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def db_arg(parser):
    parser.add_argument("--db", type=Path, default=None, help="SQLite database path")


def doctor(db_path):
    conn = connect(db_path)
    ensure_schema(conn)
    tables = ["sources", "documents", "pages", "passages", "evidence_cards", "quotes", "events", "philosophy_rules"]
    counts = {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in tables}
    fts = {}
    for name in ("pages_fts", "passages_fts", "quotes_fts", "events_fts", "philosophy_rules_fts"):
        try:
            fts[name] = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        except sqlite3.OperationalError:
            fts[name] = None
    errors = []
    if counts["pages"] and fts["pages_fts"] != counts["pages"]:
        errors.append("pages FTS count differs")
    if counts["passages"] and fts["passages_fts"] != counts["passages"]:
        errors.append("passages FTS count differs")
    conn.close()
    return {"passed": not errors, "database": str(db_path or default_db_path()), "counts": counts, "fts": fts, "errors": errors}


def main(argv=None):
    parser = argparse.ArgumentParser(description="毛泽东史料本地数据库与索引工具")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init-db"); db_arg(p)
    p = sub.add_parser("ingest"); db_arg(p); p.add_argument("--root", type=Path, default=default_root())
    p = sub.add_parser("index"); db_arg(p)
    p = sub.add_parser("search"); db_arg(p); p.add_argument("query"); p.add_argument("--kind", default="all"); p.add_argument("--limit", type=int, default=10)
    p = sub.add_parser("trace-quote"); db_arg(p); p.add_argument("query"); p.add_argument("--limit", type=int, default=10)
    p = sub.add_parser("trace-event"); db_arg(p); p.add_argument("query"); p.add_argument("--limit", type=int, default=10)
    p = sub.add_parser("trace-page"); db_arg(p); p.add_argument("anchor"); p.add_argument("--context", type=int, default=1)
    p = sub.add_parser("vector-search"); db_arg(p); p.add_argument("query"); p.add_argument("--kind", default="all", choices=["all", "passage", "quote", "event", "philosophy"]); p.add_argument("--limit", type=int, default=10)
    p = sub.add_parser("doctor"); db_arg(p)
    p = sub.add_parser("vector-index", aliases=["vector-build"]); db_arg(p); p.add_argument("--kind", default="all", choices=["all", "passage", "quote", "event", "philosophy"])
    p = sub.add_parser("relations-import"); db_arg(p); p.add_argument("--root", type=Path, default=default_root())
    p = sub.add_parser("relations-review"); db_arg(p); p.add_argument("link_id", type=int); p.add_argument("--status", choices=["candidate", "reviewed", "rejected"], required=True)
    args = parser.parse_args(argv)
    db_path = args.db or default_db_path()
    try:
        if args.command == "init-db":
            conn = connect(db_path); ensure_schema(conn); conn.close(); result = {"passed": True, "database": str(db_path)}
        elif args.command == "ingest":
            result = ingest(args.root, db_path)
        elif args.command == "index":
            conn = connect(db_path); result = build_indexes(conn); conn.close()
        elif args.command == "search":
            result = search(db_path, args.query, args.kind, args.limit)
        elif args.command == "trace-quote":
            result = trace_quote(db_path, args.query, args.limit)
        elif args.command == "trace-event":
            result = trace_event(db_path, args.query, args.limit)
        elif args.command == "trace-page":
            result = trace_page(db_path, args.anchor, args.context)
        elif args.command in ("vector-index", "vector-build"):
            result = build_vectors(db_path, args.kind)
        elif args.command == "relations-import":
            conn = connect(db_path); ensure_schema(conn)
            with conn:
                result = {"quote_event_links": import_relations(conn, args.root)}
            conn.close()
        elif args.command == "relations-review":
            conn = connect(db_path); ensure_schema(conn)
            with conn:
                changed = conn.execute("UPDATE quote_event_links SET verification_status=? WHERE link_id=?", (args.status, args.link_id)).rowcount
            conn.close()
            if not changed:
                raise ValueError("unknown relation link_id: %s" % args.link_id)
            result = {"link_id": args.link_id, "verification_status": args.status}
        elif args.command == "vector-search":
            result = search_vectors(db_path, args.query, args.kind, args.limit)
        else:
            result = doctor(db_path)
        emit(result)
        return 0 if not isinstance(result, dict) or result.get("passed", True) else 1
    except Exception as exc:
        emit({"passed": False, "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
