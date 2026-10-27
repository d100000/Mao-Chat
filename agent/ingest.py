#!/usr/bin/env python3
"""Import the existing OCR corpus and reviewed evidence into SQLite."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .db import connect, default_root, ensure_schema, json_text


def read_jsonl(path: Path):
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def validate_manifest(root: Path) -> dict:
    """Validate the immutable PDF/OCR manifest before opening the DB.

    This deliberately fails closed: a changed PDF, missing page, changed
    character count, or page marked as proofread is not silently imported.
    """
    manifest_path = root / "corpus" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "complete":
        raise ValueError("corpus manifest is not complete")
    total_pages = 0
    total_characters = 0
    books = manifest.get("books", [])
    if len(books) != 6:
        raise ValueError("expected six source books, got %s" % len(books))
    for book in books:
        source = Path(book.get("source_file", ""))
        if not source.is_absolute():
            source = root / source
        if not source.is_file():
            raise ValueError("source PDF missing: " + str(source))
        expected_hash = book.get("sha256")
        if expected_hash and sha256(source) != expected_hash:
            raise ValueError("source PDF SHA256 mismatch: " + book["source_id"])
        pages_path = root / "corpus" / book["pages_file"]
        rows = list(read_jsonl(pages_path))
        expected_pages = int(book["expected_pages"])
        if len(rows) != expected_pages:
            raise ValueError("page count mismatch: %s" % book["source_id"])
        expected_sequence = list(range(1, expected_pages + 1))
        actual_sequence = [int(row.get("pdf_page", -1)) for row in rows]
        if actual_sequence != expected_sequence:
            raise ValueError("page sequence/anchors are not contiguous: " + book["source_id"])
        characters = 0
        for row in rows:
            if row.get("source_id") not in (None, book["source_id"]):
                raise ValueError("page source_id mismatch: " + book["source_id"])
            if row.get("proofread") is not False:
                raise ValueError("OCR proofread flag changed: " + book["source_id"])
            characters += len(row.get("text", ""))
        if characters != int(book["characters"]):
            raise ValueError("OCR character count mismatch: " + book["source_id"])
        total_pages += expected_pages
        total_characters += characters
    expected_total = int(manifest.get("expected_pages", total_pages))
    if total_pages != expected_total:
        raise ValueError("total page count mismatch")
    return {"books": len(books), "pages": total_pages, "characters": total_characters,
            "manifest_sha256": sha256(manifest_path)}


def _upsert_source(conn, book, root: Path):
    source_id = book["source_id"]
    source_file = Path(book.get("source_file", ""))
    if not source_file.is_absolute():
        source_file = root / source_file
    basename = source_file.name if source_file.name else book.get("title", source_id)
    conn.execute(
        """INSERT INTO sources(source_id,title,source_file,source_basename,sha256,expected_pages,characters,metadata_json)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(source_id) DO UPDATE SET title=excluded.title,source_file=excluded.source_file,
           source_basename=excluded.source_basename,sha256=excluded.sha256,expected_pages=excluded.expected_pages,
           characters=excluded.characters,metadata_json=excluded.metadata_json,updated_at=CURRENT_TIMESTAMP""",
        (source_id, book.get("title", source_id), str(source_file), basename, book.get("sha256"),
         book.get("expected_pages"), book.get("characters"), json_text(book)),
    )
    conn.execute(
        """INSERT INTO documents(document_id,source_id,title,document_type,author,metadata_json)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(document_id) DO UPDATE SET source_id=excluded.source_id,title=excluded.title,
           document_type=excluded.document_type,metadata_json=excluded.metadata_json,updated_at=CURRENT_TIMESTAMP""",
        (source_id, source_id, book.get("title", source_id), "volume", "毛泽东", json_text(book)),
    )


def _split_passages(text: str):
    # Preserve OCR text while making stable paragraph-like chunks for retrieval.
    chunks = [x.strip() for x in re.split(r"\n\s*\n+", text or "") if x.strip()]
    if not chunks and text:
        chunks = [text.strip()]
    return chunks


def import_corpus(conn: sqlite3.Connection, root: Path) -> dict:
    manifest_path = root / "corpus" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    books = manifest.get("books", [])
    total_pages = total_passages = 0
    current_pages = {}
    for book in books:
        _upsert_source(conn, book, root)
        pages_path = root / "corpus" / book["pages_file"]
        source_id = book["source_id"]
        page_numbers = set()
        for row in read_jsonl(pages_path):
            source_id = row.get("source_id") or book["source_id"]
            page_no = int(row["pdf_page"])
            page_numbers.add(page_no)
            anchor = f"{source_id}:p{page_no:04d}"
            meta = {k: v for k, v in row.items() if k not in ("text", "lines")}
            conn.execute(
                """INSERT INTO pages(source_id,document_id,pdf_page,printed_page,anchor,text,ocr_status,metadata_json)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(source_id,pdf_page) DO UPDATE SET document_id=excluded.document_id,
                   printed_page=excluded.printed_page,anchor=excluded.anchor,text=excluded.text,
                   ocr_status=excluded.ocr_status,metadata_json=excluded.metadata_json""",
                (source_id, source_id, page_no, row.get("printed_page"), anchor, row.get("text", ""),
                 row.get("human_review_status") or row.get("status"), json_text(meta)),
            )
            page_id = conn.execute("SELECT page_id FROM pages WHERE source_id=? AND pdf_page=?", (source_id, page_no)).fetchone()[0]
            conn.execute("DELETE FROM passages WHERE page_id=?", (page_id,))
            for ordinal, chunk in enumerate(_split_passages(row.get("text", ""))):
                conn.execute("INSERT INTO passages(page_id,ordinal,article_title,text,start_offset,end_offset) VALUES(?,?,?,?,?,?)",
                             (page_id, ordinal, None, chunk, None, None))
            total_pages += 1
            total_passages += len(_split_passages(row.get("text", "")))
        current_pages[book["source_id"]] = page_numbers

    # The manifest is authoritative.  Remove pages and sources that were
    # deleted from the current corpus so corrections/revocations cannot leave
    # stale OCR searchable.  Foreign keys cascade page -> passage cleanup.
    source_ids = set(current_pages)
    if source_ids:
        marks = ",".join("?" for _ in source_ids)
        conn.execute(f"DELETE FROM pages WHERE source_id NOT IN ({marks})", tuple(source_ids))
        conn.execute(f"DELETE FROM sources WHERE source_id NOT IN ({marks})", tuple(source_ids))
        conn.execute(f"DELETE FROM documents WHERE document_type='volume' AND source_id NOT IN ({marks})", tuple(source_ids))
        conn.execute(f"DELETE FROM documents WHERE document_type='article' AND source_id NOT IN ({marks})", tuple(source_ids))
    else:
        conn.execute("DELETE FROM pages")
        conn.execute("DELETE FROM sources")
        conn.execute("DELETE FROM documents WHERE document_type IN ('volume','article')")
    for source_id, page_numbers in current_pages.items():
        if page_numbers:
            marks = ",".join("?" for _ in page_numbers)
            conn.execute(
                f"DELETE FROM pages WHERE source_id=? AND pdf_page NOT IN ({marks})",
                (source_id, *sorted(page_numbers)),
            )
        else:
            conn.execute("DELETE FROM pages WHERE source_id=?", (source_id,))
    return {"sources": len(books), "pages": total_pages, "passages": total_passages}


def import_articles(conn: sqlite3.Connection, root: Path) -> int:
    path = root / "corpus" / "article_navigation.json"
    articles = json.loads(path.read_text(encoding="utf-8")).get("articles", []) if path.exists() else []
    count = 0
    current_ids = set()
    for article in articles:
        source = article["source_id"]
        doc_id = f"article:{source}:{article['pdf_page_start']}:{article['pdf_page_end']}"
        current_ids.add(doc_id)
        conn.execute(
            """INSERT INTO documents(document_id,source_id,title,document_type,author,metadata_json)
               VALUES(?,?,?,?,?,?) ON CONFLICT(document_id) DO UPDATE SET title=excluded.title,metadata_json=excluded.metadata_json""",
            (doc_id, source, article.get("title", doc_id), "article", "毛泽东", json_text(article)),
        )
        conn.execute("UPDATE pages SET document_id=? WHERE source_id=? AND pdf_page BETWEEN ? AND ?",
                     (doc_id, source, article["pdf_page_start"], article["pdf_page_end"]))
        count += 1
    # Article navigation is also a generated view of the current corpus.
    # Remove old article documents, including ranges removed from the JSON.
    if current_ids:
        marks = ",".join("?" for _ in current_ids)
        conn.execute(
            f"DELETE FROM documents WHERE document_type='article' AND document_id NOT IN ({marks})",
            tuple(current_ids),
        )
    else:
        conn.execute("DELETE FROM documents WHERE document_type='article'")
    return count


def _upsert_evidence(conn, card):
    bool_int = lambda value: 1 if value is True else 0
    conn.execute(
        """INSERT INTO evidence_cards(evidence_id,topic,article_title,source_id,summary,excerpt,original_date_display,
           earliest_content_date,earliest_wording_date,known_to_character_from,content_qualification,source_role,speaker,
           actor,reviewed,quote_verified,source_urls_json,raw_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(evidence_id) DO UPDATE SET topic=excluded.topic,article_title=excluded.article_title,
           source_id=excluded.source_id,summary=excluded.summary,excerpt=excluded.excerpt,
           original_date_display=excluded.original_date_display,earliest_content_date=excluded.earliest_content_date,
           earliest_wording_date=excluded.earliest_wording_date,known_to_character_from=excluded.known_to_character_from,
           content_qualification=excluded.content_qualification,source_role=excluded.source_role,speaker=excluded.speaker,
           actor=excluded.actor,reviewed=excluded.reviewed,quote_verified=excluded.quote_verified,
           source_urls_json=excluded.source_urls_json,raw_json=excluded.raw_json,updated_at=CURRENT_TIMESTAMP""",
        (card.get("id"), card.get("topic"), card.get("article_title"), card.get("source_id"), card.get("summary"),
         card.get("excerpt"), card.get("original_date_display"), card.get("earliest_content_date"),
         card.get("earliest_wording_date"), card.get("known_to_character_from"), card.get("content_qualification"),
         card.get("source_role"), card.get("speaker"), card.get("actor"), bool_int(card.get("reviewed")),
         bool_int(card.get("quote_verified")), json_text(card.get("source_urls", [])), json_text(card)),
    )
    if card.get("excerpt") and card.get("id"):
        quote_id = f"quote:{card['id']}"
        status = "reviewed" if card.get("quote_verified") is True else "candidate"
        conn.execute(
            """INSERT INTO quotes(quote_id,evidence_id,text,speaker,article_title,source_id,pdf_pages_json,earliest_wording_date,verification_status,metadata_json)
               VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(quote_id) DO UPDATE SET evidence_id=excluded.evidence_id,text=excluded.text,
               speaker=excluded.speaker,article_title=excluded.article_title,source_id=excluded.source_id,pdf_pages_json=excluded.pdf_pages_json,
               earliest_wording_date=excluded.earliest_wording_date,verification_status=excluded.verification_status,metadata_json=excluded.metadata_json""",
            (quote_id, card.get("id"), card.get("excerpt"), card.get("speaker"), card.get("article_title"), card.get("source_id"),
             json_text(card.get("pdf_pages", [])), card.get("earliest_wording_date"), status, json_text(card)),
        )


def import_evidence(conn, root: Path) -> dict:
    path = root / "skills" / "mao-primary-sources" / "references" / "verified-evidence.jsonl"
    cards = list(read_jsonl(path)) if path.exists() else []
    evidence_ids = {str(card.get("id")) for card in cards if card.get("id")}
    quote_ids = {
        f"quote:{card['id']}" for card in cards if card.get("id") and card.get("excerpt")
    }
    event_ids = {
        f"event:{card['id']}" for card in cards
        if card.get("id") and card.get("source_role") == "biographical_record"
    }
    for card in cards:
        _upsert_evidence(conn, card)
    # Public-event cards provide cautious event records; no quote-event link is
    # invented without a reviewed relation.
    event_count = 0
    for card in cards:
        if card.get("source_role") != "biographical_record":
            continue
        event_id = f"event:{card['id']}"
        conn.execute(
            """INSERT INTO events(event_id,evidence_id,title,start_date,end_date,location,participants_json,description,verification_status,metadata_json)
               VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET title=excluded.title,start_date=excluded.start_date,
               end_date=excluded.end_date,description=excluded.description,verification_status=excluded.verification_status,metadata_json=excluded.metadata_json""",
            (event_id, card.get("id"), card.get("topic") or card.get("article_title") or event_id,
             card.get("event_date_lower"), card.get("event_date_upper"), card.get("location"), json_text([card.get("actor")]),
             card.get("summary"), "reviewed" if card.get("public_event_verified") is True else "candidate", json_text(card)),
        )
        event_count += 1
    # Evidence JSONL is the source of truth for generated cards, quotes and
    # public-event rows.  Delete records removed from it so historical queries
    # cannot resurrect revoked evidence.  Cascades remove stale relations.
    for table, key, ids in (
        ("quotes", "quote_id", quote_ids),
        ("events", "event_id", event_ids),
        ("evidence_cards", "evidence_id", evidence_ids),
    ):
        if ids:
            marks = ",".join("?" for _ in ids)
            conn.execute(
                f"DELETE FROM {table} WHERE {key} NOT IN ({marks})",
                tuple(ids),
            )
        else:
            conn.execute(f"DELETE FROM {table}")
    return {"evidence_cards": len(cards), "events": event_count, "quotes": conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]}


def import_philosophy(conn, root: Path) -> int:
    path = root / "skills" / "mao-primary-sources" / "references" / "worldview.md"
    if not path.exists():
        conn.execute("DELETE FROM philosophy_rules WHERE rule_id LIKE 'worldview:%'")
        return 0
    count = 0
    current_ids = set()
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.startswith("|") or line.startswith("| ---") or "主题" in line:
            continue
        cells = [x.strip() for x in line.strip().strip("|").split("|")]
        if len(cells) < 4 or not cells[0]:
            continue
        rule_id = f"worldview:{line_no}"
        current_ids.add(rule_id)
        conn.execute(
            """INSERT INTO philosophy_rules(rule_id,label,decision_rule,conditions,source_locator,verification_status,raw_json)
               VALUES(?,?,?,?,?,?,?) ON CONFLICT(rule_id) DO UPDATE SET label=excluded.label,decision_rule=excluded.decision_rule,
               conditions=excluded.conditions,source_locator=excluded.source_locator,raw_json=excluded.raw_json""",
            (rule_id, cells[0], cells[1], cells[3], cells[2], "candidate", json_text({"line": line_no, "cells": cells})),
        )
        count += 1
    if current_ids:
        marks = ",".join("?" for _ in current_ids)
        conn.execute(
            f"DELETE FROM philosophy_rules WHERE rule_id LIKE 'worldview:%' AND rule_id NOT IN ({marks})",
            tuple(current_ids),
        )
    else:
        conn.execute("DELETE FROM philosophy_rules WHERE rule_id LIKE 'worldview:%'")
    return count


def import_relations(conn: sqlite3.Connection, root: Path) -> int:
    """Import only explicitly supplied quote/event relations.

    No relation is inferred from keyword overlap.  The JSONL file is an
    editorial input and may be empty until a reviewer verifies a relation.
    """
    path = root / "data" / "quote_event_links.jsonl"
    # Relations are entirely generated from this editorial file.  Clear the
    # previous set first so deleting a line really removes the relation.
    conn.execute("DELETE FROM quote_event_links")
    if not path.exists():
        return 0
    count = 0
    for row in read_jsonl(path):
        quote_id = row.get("quote_id")
        event_id = row.get("event_id")
        relation_type = row.get("relation_type")
        if not all(isinstance(value, str) and value.strip() for value in (quote_id, event_id, relation_type)):
            raise ValueError("relation requires quote_id, event_id and relation_type")
        if conn.execute("SELECT 1 FROM quotes WHERE quote_id=?", (quote_id,)).fetchone() is None:
            raise ValueError("unknown quote_id: " + quote_id)
        if conn.execute("SELECT 1 FROM events WHERE event_id=?", (event_id,)).fetchone() is None:
            raise ValueError("unknown event_id: " + event_id)
        confidence = row.get("confidence")
        if confidence is not None and not 0 <= float(confidence) <= 1:
            raise ValueError("relation confidence must be between 0 and 1")
        status = row.get("verification_status", "candidate")
        if status not in {"candidate", "reviewed", "rejected"}:
            raise ValueError("invalid relation verification_status")
        conn.execute(
            """INSERT INTO quote_event_links(quote_id,event_id,relation_type,confidence,verification_status,evidence_json)
               VALUES(?,?,?,?,?,?) ON CONFLICT(quote_id,event_id,relation_type) DO UPDATE SET confidence=excluded.confidence,
               verification_status=excluded.verification_status,evidence_json=excluded.evidence_json""",
            (quote_id, event_id, relation_type, confidence, status, json_text(row.get("evidence", {}))),
        )
        count += 1
    return count


def ingest(root: Optional[Path] = None, db_path=None) -> dict:
    root = Path(root or default_root()).resolve()
    manifest_check = validate_manifest(root)
    conn = connect(db_path, root)
    ensure_schema(conn)
    try:
        with conn:
            result = import_corpus(conn, root)
            result["articles"] = import_articles(conn, root)
            result.update(import_evidence(conn, root))
            result["philosophy_rules"] = import_philosophy(conn, root)
            result["quote_event_links"] = import_relations(conn, root)
            result["manifest"] = manifest_check
            conn.execute("INSERT INTO schema_meta(key,value) VALUES('manifest_sha256',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (manifest_check["manifest_sha256"],))
            conn.execute("INSERT INTO schema_meta(key,value) VALUES('last_ingest_root',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(root),))
            conn.execute("INSERT INTO schema_meta(key,value) VALUES('last_ingest_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (datetime.now(timezone.utc).isoformat(),))
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
