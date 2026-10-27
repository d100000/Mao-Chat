#!/usr/bin/env python3
"""Dependency-free local vector index.

This is a deterministic hashing embedding for offline retrieval.  It is a
lexical baseline, not a claim that a small hash vector understands history.
An embedding provider can be added later without changing the database
contract (`embeddings` stores model and dimension explicitly).
"""
from __future__ import annotations

from array import array
import hashlib
import math
import sqlite3
from typing import Dict, Iterable, List, Tuple

from .db import connect, ensure_schema
from .index import tokens


MODEL = "hashing-charbigram-v1"
DIMENSION = 384
MAX_SEARCH_LIMIT = 30


def _bounded_limit(value: int, default: int = 10) -> int:
    """Clamp untrusted retrieval limits before slicing result sets."""
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(1, min(parsed, MAX_SEARCH_LIMIT))


def vectorize(text: str, dimension: int = DIMENSION) -> List[float]:
    values = [0.0] * dimension
    items = tokens(text or "")
    for token in items:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        index = value % dimension
        sign = -1.0 if (value >> 63) & 1 else 1.0
        values[index] += sign
    norm = math.sqrt(sum(x * x for x in values))
    if norm:
        values = [x / norm for x in values]
    return values


def pack(values: List[float]) -> bytes:
    return array("f", values).tobytes()


def unpack(blob: bytes, dimension: int) -> List[float]:
    values = array("f")
    values.frombytes(blob)
    if len(values) != dimension:
        raise ValueError("embedding dimension mismatch")
    return list(values)


def cosine(left: List[float], right: List[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def _targets(conn: sqlite3.Connection, target_type: str = "all") -> Iterable[Tuple[str, str, str]]:
    sources = {
        "passage": ("SELECT passage_id, article_title, text FROM passages", "passage_id"),
        "quote": ("SELECT quote_id, article_title, text FROM quotes", "quote_id"),
        "event": ("SELECT event_id, title, description FROM events", "event_id"),
        "philosophy": ("SELECT rule_id, label, decision_rule || ' ' || conditions FROM philosophy_rules", "rule_id"),
    }
    selected = sources if target_type == "all" else {target_type: sources[target_type]}
    for kind, (sql, key) in selected.items():
        for row in conn.execute(sql):
            fields = list(row)
            yield kind, str(fields[0]), " ".join(str(x or "") for x in fields[1:])


def build_vectors(db_path=None, target_type="all", model=MODEL, dimension=DIMENSION) -> Dict[str, int]:
    conn = connect(db_path)
    ensure_schema(conn)
    counts: Dict[str, int] = {}
    with conn:
        # Ingest can replace AUTOINCREMENT page/passage IDs.  Remove rows
        # belonging to records that no longer exist before upserting the
        # current corpus, otherwise search returns orphan ``target_id`` hits
        # with no text or metadata.
        kinds = [target_type] if target_type != "all" else ["passage", "quote", "event", "philosophy"]
        for kind in kinds:
            # Rebuilding one target kind is small enough to do in one
            # transaction and avoids SQLite's variable-count limit for a
            # large ``NOT IN`` cleanup query.
            conn.execute("DELETE FROM embeddings WHERE target_type=? AND model=?", (kind, model))
        for kind, target_id, text in _targets(conn, target_type):
            values = vectorize(text, dimension)
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            conn.execute(
                """INSERT INTO embeddings(target_type,target_id,model,dimension,vector_blob,content_hash)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(target_type,target_id,model) DO UPDATE SET dimension=excluded.dimension,
                   vector_blob=excluded.vector_blob,content_hash=excluded.content_hash,created_at=CURRENT_TIMESTAMP""",
                (kind, target_id, model, dimension, pack(values), digest),
            )
            counts[kind] = counts.get(kind, 0) + 1
    conn.close()
    counts["total"] = sum(counts.values())
    return counts


def search_vectors(db_path, query: str, target_type="all", limit=10, model=MODEL) -> List[dict]:
    limit = _bounded_limit(limit)
    conn = connect(db_path)
    ensure_schema(conn)
    q = vectorize(query)
    rows = conn.execute(
        "SELECT target_type,target_id,dimension,vector_blob,content_hash FROM embeddings WHERE model=?" +
        (" AND target_type=?" if target_type != "all" else ""),
        (model, target_type) if target_type != "all" else (model,),
    ).fetchall()
    scores = []
    for row in rows:
        try:
            score = cosine(q, unpack(row["vector_blob"], row["dimension"]))
        except (ValueError, TypeError):
            continue
        scores.append((score, dict(row)))
    scores.sort(key=lambda item: (-item[0], item[1]["target_type"], item[1]["target_id"]))
    output = []
    for score, row in scores[:limit]:
        kind, target_id = row["target_type"], row["target_id"]
        table, key = {"passage": ("passages", "passage_id"), "quote": ("quotes", "quote_id"),
                      "event": ("events", "event_id"), "philosophy": ("philosophy_rules", "rule_id")}[kind]
        item = conn.execute("SELECT * FROM %s WHERE %s=?" % (table, key), (target_id,)).fetchone()
        # A manually edited database may still contain a malformed embedding;
        # do not expose an unresolvable vector hit as if it were source text.
        if item is None:
            continue
        record = dict(item)
        record.update({"result_type": kind, "vector_score": score, "embedding_model": model})
        output.append(record)
    conn.close()
    return output
