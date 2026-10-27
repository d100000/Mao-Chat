#!/usr/bin/env python3
"""Deterministic CJK FTS5 indexes and lookup helpers."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from .db import connect, default_db_path, ensure_schema


def tokens(text: str) -> list[str]:
    out: list[str] = []
    for run in re.findall(r"[\u3400-\u9fff]+|[A-Za-z0-9_]+", (text or "").lower()):
        if re.fullmatch(r"[\u3400-\u9fff]+", run):
            out.extend(run[i : i + 2] for i in range(len(run) - 1))
            if len(run) == 1:
                out.append(run)
        else:
            out.append(run)
    return out


def indexed_text(*values) -> str:
    return " ".join(tokens(" ".join(str(v or "") for v in values)))


def build_indexes(conn: sqlite3.Connection, *, own_transaction=True) -> dict:
    if own_transaction:
        ensure_schema(conn)
    suffix = "__staging"
    backup_suffix = "__backup"
    names = ("pages_fts", "passages_fts", "quotes_fts", "events_fts", "philosophy_rules_fts")
    # Build into staging virtual tables first.  The live tables remain intact
    # if tokenization, an insert, or a source read fails.
    if own_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        for name in names:
            conn.execute(f"DROP TABLE IF EXISTS {name}{suffix}")
            conn.execute(f"DROP TABLE IF EXISTS {name}{backup_suffix}")
        conn.execute("CREATE VIRTUAL TABLE pages_fts__staging USING fts5(page_id UNINDEXED, content)")
        conn.execute("CREATE VIRTUAL TABLE passages_fts__staging USING fts5(passage_id UNINDEXED, content)")
        conn.execute("CREATE VIRTUAL TABLE quotes_fts__staging USING fts5(quote_id UNINDEXED, content)")
        conn.execute("CREATE VIRTUAL TABLE events_fts__staging USING fts5(event_id UNINDEXED, content)")
        conn.execute("CREATE VIRTUAL TABLE philosophy_rules_fts__staging USING fts5(rule_id UNINDEXED, content)")
        pages = conn.execute("SELECT page_id, source_id, anchor, text, metadata_json FROM pages")
        conn.executemany(
        "INSERT INTO pages_fts__staging(page_id,content) VALUES(?,?)",
        ((r["page_id"], indexed_text(r["source_id"], r["anchor"], r["text"], r["metadata_json"])) for r in pages),
        )
        passages = conn.execute("SELECT passage_id, article_title, text FROM passages")
        conn.executemany(
        "INSERT INTO passages_fts__staging(passage_id,content) VALUES(?,?)",
        ((r["passage_id"], indexed_text(r["article_title"], r["text"])) for r in passages),
        )
        quotes = conn.execute("SELECT quote_id, text, speaker, article_title FROM quotes")
        conn.executemany(
        "INSERT INTO quotes_fts__staging(quote_id,content) VALUES(?,?)",
        ((r["quote_id"], indexed_text(r["text"], r["speaker"], r["article_title"])) for r in quotes),
        )
        events = conn.execute("SELECT event_id, title, description, location FROM events")
        conn.executemany(
        "INSERT INTO events_fts__staging(event_id,content) VALUES(?,?)",
        ((r["event_id"], indexed_text(r["title"], r["description"], r["location"])) for r in events),
        )
        rules = conn.execute("SELECT rule_id, label, decision_rule, conditions, source_locator FROM philosophy_rules")
        conn.executemany(
        "INSERT INTO philosophy_rules_fts__staging(rule_id,content) VALUES(?,?)",
        ((r["rule_id"], indexed_text(r["label"], r["decision_rule"], r["conditions"], r["source_locator"])) for r in rules),
        )
        counts = {name: conn.execute(f"SELECT COUNT(*) FROM {name}{suffix}").fetchone()[0] for name in names}
        # Keep the live tables under backup names until every staging table is
        # in place.  A failed rename rolls the transaction back to the live
        # set; no caller observes a half-built index.
        live_tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for name in names:
            if name in live_tables:
                conn.execute(f"ALTER TABLE {name} RENAME TO {name}{backup_suffix}")
        for name in names:
            conn.execute(f"ALTER TABLE {name}{suffix} RENAME TO {name}")
        for name in names:
            conn.execute(f"DROP TABLE IF EXISTS {name}{backup_suffix}")
        if own_transaction:
            conn.commit()
        return {"pages": counts["pages_fts"], "passages": counts["passages_fts"], "quotes": counts["quotes_fts"], "events": counts["events_fts"], "philosophy_rules": counts["philosophy_rules_fts"]}
    except Exception:
        if own_transaction:
            conn.rollback()
        raise


def match_query(query: str) -> str:
    terms = list(dict.fromkeys(tokens(query)))[:80]
    if not terms:
        return ""
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)


def search(db_path, query: str, kind: str = "all", limit: int = 10) -> list[dict]:
    conn = connect(db_path)
    ensure_schema(conn)
    try:
        match = match_query(query)
        if not match:
            return []
        tables = {
            "pages": ("pages_fts", "page_id", "pages", "page_id"),
            "passages": ("passages_fts", "passage_id", "passages", "passage_id"),
            "quotes": ("quotes_fts", "quote_id", "quotes", "quote_id"),
            "events": ("events_fts", "event_id", "events", "event_id"),
            "philosophy": ("philosophy_rules_fts", "rule_id", "philosophy_rules", "rule_id"),
        }
        kinds = list(tables) if kind == "all" else [kind]
        output = []
        for selected in kinds:
            if selected not in tables:
                raise ValueError("kind must be pages, passages, quotes, events, philosophy, or all")
            fts, key, table, table_key = tables[selected]
            try:
                join = (f"t.{table_key}=CAST({fts}.{key} AS INTEGER)" if selected in {"pages", "passages"}
                        else f"t.{table_key}={fts}.{key}")
                rows = conn.execute(
                    f"SELECT t.*, bm25({fts}) AS rank FROM {fts} JOIN {table} t ON {join} "
                    f"WHERE {fts} MATCH ? ORDER BY rank LIMIT ?",
                    (match, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            for row in rows:
                item = dict(row)
                item["result_type"] = selected
                output.append(item)
        output.sort(key=lambda item: (item.get("rank", 0), item.get("result_type", "")))
        return output[:limit]
    finally:
        conn.close()


def trace_page(db_path, anchor: str, context: int = 1) -> dict:
    if context < 0 or context > 5:
        raise ValueError("context must be between 0 and 5")
    conn = connect(db_path)
    row = conn.execute("SELECT * FROM pages WHERE anchor=?", (anchor,)).fetchone()
    if row is None:
        conn.close()
        raise ValueError("page anchor not found: " + anchor)
    rows = conn.execute(
        "SELECT * FROM pages WHERE source_id=? AND pdf_page BETWEEN ? AND ? ORDER BY pdf_page",
        (row["source_id"], row["pdf_page"] - context, row["pdf_page"] + context),
    ).fetchall()
    result = {"anchor": anchor, "pages": [dict(r) for r in rows]}
    conn.close()
    return result


def trace_quote(db_path, query: str, limit: int = 10) -> list[dict]:
    rows = search(db_path, query, "quotes", limit)
    conn = connect(db_path)
    for row in rows:
        row["related_events"] = [dict(item) for item in conn.execute(
            """SELECT l.relation_type,l.confidence,l.verification_status,e.*
               FROM quote_event_links l JOIN events e ON e.event_id=l.event_id
               WHERE l.quote_id=? AND l.verification_status='reviewed'""", (row.get("quote_id"),)
        ).fetchall()]
    conn.close()
    return rows


def trace_event(db_path, query: str, limit: int = 10) -> list[dict]:
    rows = search(db_path, query, "events", limit)
    conn = connect(db_path)
    for row in rows:
        row["related_quotes"] = [dict(item) for item in conn.execute(
            """SELECT l.relation_type,l.confidence,l.verification_status,q.*
               FROM quote_event_links l JOIN quotes q ON q.quote_id=l.quote_id
               WHERE l.event_id=? AND l.verification_status='reviewed'""", (row.get("event_id"),)
        ).fetchall()]
    conn.close()
    return rows
