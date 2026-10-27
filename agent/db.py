#!/usr/bin/env python3
"""SQLite storage for the local source-grounded agent."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Optional, Union


SCHEMA = r'''
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
  source_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  source_file TEXT,
  source_basename TEXT,
  sha256 TEXT,
  expected_pages INTEGER,
  characters INTEGER,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS documents (
  document_id TEXT PRIMARY KEY,
  source_id TEXT REFERENCES sources(source_id) ON DELETE SET NULL,
  title TEXT NOT NULL,
  document_type TEXT NOT NULL DEFAULT 'volume',
  author TEXT,
  original_date_lower TEXT,
  original_date_upper TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS pages (
  page_id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
  document_id TEXT REFERENCES documents(document_id) ON DELETE SET NULL,
  pdf_page INTEGER NOT NULL,
  printed_page TEXT,
  anchor TEXT NOT NULL UNIQUE,
  text TEXT NOT NULL,
  ocr_status TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(source_id, pdf_page)
);
CREATE TABLE IF NOT EXISTS passages (
  passage_id INTEGER PRIMARY KEY AUTOINCREMENT,
  page_id INTEGER NOT NULL REFERENCES pages(page_id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  article_title TEXT,
  text TEXT NOT NULL,
  start_offset INTEGER,
  end_offset INTEGER,
  UNIQUE(page_id, ordinal)
);
CREATE TABLE IF NOT EXISTS evidence_cards (
  evidence_id TEXT PRIMARY KEY,
  topic TEXT,
  article_title TEXT,
  source_id TEXT REFERENCES sources(source_id) ON DELETE SET NULL,
  summary TEXT,
  excerpt TEXT,
  original_date_display TEXT,
  earliest_content_date TEXT,
  earliest_wording_date TEXT,
  known_to_character_from TEXT,
  content_qualification TEXT,
  source_role TEXT,
  speaker TEXT,
  actor TEXT,
  reviewed INTEGER NOT NULL DEFAULT 0,
  quote_verified INTEGER NOT NULL DEFAULT 0,
  source_urls_json TEXT NOT NULL DEFAULT '[]',
  raw_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS quotes (
  quote_id TEXT PRIMARY KEY,
  evidence_id TEXT REFERENCES evidence_cards(evidence_id) ON DELETE SET NULL,
  text TEXT NOT NULL,
  speaker TEXT,
  article_title TEXT,
  source_id TEXT REFERENCES sources(source_id) ON DELETE SET NULL,
  pdf_pages_json TEXT NOT NULL DEFAULT '[]',
  earliest_wording_date TEXT,
  verification_status TEXT NOT NULL DEFAULT 'candidate',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  evidence_id TEXT REFERENCES evidence_cards(evidence_id) ON DELETE SET NULL,
  title TEXT NOT NULL,
  start_date TEXT,
  end_date TEXT,
  location TEXT,
  participants_json TEXT NOT NULL DEFAULT '[]',
  description TEXT,
  verification_status TEXT NOT NULL DEFAULT 'candidate',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS quote_event_links (
  link_id INTEGER PRIMARY KEY AUTOINCREMENT,
  quote_id TEXT NOT NULL REFERENCES quotes(quote_id) ON DELETE CASCADE,
  event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
  relation_type TEXT NOT NULL,
  confidence REAL,
  verification_status TEXT NOT NULL DEFAULT 'candidate',
  evidence_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(quote_id, event_id, relation_type)
);
CREATE TABLE IF NOT EXISTS philosophy_rules (
  rule_id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  decision_rule TEXT,
  conditions TEXT,
  source_locator TEXT,
  evidence_ids_json TEXT NOT NULL DEFAULT '[]',
  counter_evidence_ids_json TEXT NOT NULL DEFAULT '[]',
  verification_status TEXT NOT NULL DEFAULT 'candidate',
  raw_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS web_sources (
  web_source_id INTEGER PRIMARY KEY AUTOINCREMENT,
  query TEXT NOT NULL,
  url TEXT,
  title TEXT,
  description TEXT,
  content TEXT,
  source_provider TEXT,
  fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  sha256 TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(query, url)
);
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  mode TEXT NOT NULL,
  as_of TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS messages (
  message_id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  evidence_ids_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS citations (
  citation_id INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id INTEGER NOT NULL REFERENCES messages(message_id) ON DELETE CASCADE,
  evidence_id TEXT,
  source_id TEXT,
  pdf_page INTEGER,
  url TEXT,
  citation_text TEXT,
  verification_status TEXT NOT NULL DEFAULT 'candidate'
);
CREATE TABLE IF NOT EXISTS llm_runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT REFERENCES sessions(session_id) ON DELETE SET NULL,
  provider TEXT,
  model TEXT,
  mode TEXT,
  prompt_hash TEXT,
  started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  elapsed_ms INTEGER,
  input_tokens INTEGER,
  output_tokens INTEGER,
  status TEXT,
  error TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS embeddings (
  embedding_id INTEGER PRIMARY KEY AUTOINCREMENT,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  model TEXT NOT NULL,
  dimension INTEGER NOT NULL,
  vector_blob BLOB NOT NULL,
  content_hash TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(target_type, target_id, model)
);
CREATE INDEX IF NOT EXISTS idx_embeddings_target ON embeddings(target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_pages_source_page ON pages(source_id, pdf_page);
CREATE INDEX IF NOT EXISTS idx_passages_page ON passages(page_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_evidence_source ON evidence_cards(source_id);
CREATE INDEX IF NOT EXISTS idx_events_dates ON events(start_date, end_date);
CREATE INDEX IF NOT EXISTS idx_quotes_evidence ON quotes(evidence_id);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, created_at);
'''


def default_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_db_path(root: Optional[Path] = None) -> Path:
    env = os.environ.get("MAO_AGENT_DB")
    return Path(env).expanduser() if env else (root or default_root()) / "data" / "mao_agent.db"


def connect(path: Optional[Union[str, Path]] = None, root: Optional[Path] = None) -> sqlite3.Connection:
    db_path = Path(path).expanduser() if path else default_db_path(root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO schema_meta(key,value) VALUES('schema_version','1') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )
    conn.commit()


def json_text(value) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def row_dict(row):
    return dict(row) if row is not None else None


def record_session(conn: sqlite3.Connection, session_id: str, mode: str, as_of=None, metadata=None) -> None:
    """Persist a session header; callers must validate the identifier first."""
    conn.execute(
        """INSERT INTO sessions(session_id,mode,as_of,metadata_json)
           VALUES(?,?,?,?)
           ON CONFLICT(session_id) DO UPDATE SET mode=excluded.mode,as_of=excluded.as_of,
           updated_at=CURRENT_TIMESTAMP,metadata_json=excluded.metadata_json""",
        (session_id, mode, as_of, json_text(metadata or {})),
    )


def record_message(conn: sqlite3.Connection, session_id: str, role: str, content: str,
                   evidence_ids=None, metadata=None) -> int:
    cur = conn.execute(
        "INSERT INTO messages(session_id,role,content,evidence_ids_json,metadata_json) VALUES(?,?,?,?,?)",
        (session_id, role, content, json_text(evidence_ids or []), json_text(metadata or {})),
    )
    conn.execute("UPDATE sessions SET updated_at=CURRENT_TIMESTAMP WHERE session_id=?", (session_id,))
    return int(cur.lastrowid)


def record_citations(conn: sqlite3.Connection, message_id: int, citations) -> None:
    for item in citations or []:
        conn.execute(
            """INSERT INTO citations(message_id,evidence_id,source_id,pdf_page,url,citation_text,verification_status)
               VALUES(?,?,?,?,?,?,?)""",
            (message_id, item.get("evidence_id"), item.get("source_id"), item.get("pdf_page"),
             item.get("url"), item.get("citation_text"), item.get("verification_status", "candidate")),
        )


def record_llm_run(conn: sqlite3.Connection, session_id: Optional[str], provider: str, model: str,
                   mode: str, prompt_hash: str, elapsed_ms: int, status: str,
                   input_tokens=None, output_tokens=None, error=None, metadata=None) -> int:
    cur = conn.execute(
        """INSERT INTO llm_runs(session_id,provider,model,mode,prompt_hash,elapsed_ms,input_tokens,
           output_tokens,status,error,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (session_id, provider, model, mode, prompt_hash, elapsed_ms, input_tokens, output_tokens,
         status, error, json_text(metadata or {})),
    )
    return int(cur.lastrowid)
