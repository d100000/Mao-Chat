"""Content-addressed versions for the curated library; never hash user records."""
import hashlib
import json
from pathlib import Path
import sqlite3
from .principles import PRINCIPLES


def content_version(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def knowledge_revision(db_path):
    path = Path(db_path)
    if not path.is_file(): return 'unavailable'
    conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('BEGIN')
        return revision_in_transaction(conn)
    finally: conn.close()


def revision_in_transaction(conn):
    digest = hashlib.sha256(content_version(PRINCIPLES).encode())
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, key in [('evidence_cards','evidence_id'), ('quotes','quote_id'), ('events','event_id'), ('philosophy_rules','rule_id'), ('quote_event_links','link_id')]:
        if table not in tables: continue
        # Sort by row content rather than assuming a particular relation PK.
        rows = [{k:r[k] for k in r.keys() if k != 'updated_at'} for r in conn.execute('SELECT * FROM '+table)]
        digest.update(table.encode())
        for row in sorted(rows, key=content_version): digest.update(content_version(row).encode())
    return digest.hexdigest()
