#!/usr/bin/env python3
"""Create a portable curated library without private PDFs or application state.

Full OCR ingestion remains separate and retains its source/PDF validation.
This entry point only imports the checked-in, bounded evidence collection.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.db import connect, ensure_schema
from agent.ingest import import_evidence, import_philosophy, import_relations
from agent.index import build_indexes
from agent.vector import build_vectors


def bootstrap(db_path=None, *, root=ROOT, refresh=False):
    root = Path(root).resolve()
    db_path = Path(db_path or root / 'data/mao_agent.db').resolve()
    if db_path.exists() and not refresh:
        raise ValueError('数据库已存在；使用 --refresh-library 更新资料，或选择新的 --db。')
    evidence_script = root / 'skills/mao-primary-sources/scripts/persona.py'
    spec = importlib.util.spec_from_file_location('bootstrap_persona', evidence_script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cards = module.load_cards()
    if not cards:
        raise ValueError('核验资料为空，未创建资料库。')
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        ensure_schema(conn)
        with conn:
            for card in cards:
                source_id = card.get('source_id')
                if not source_id:
                    continue
                conn.execute('''INSERT INTO sources(source_id,title,sha256,metadata_json)
                    VALUES(?,?,?,?) ON CONFLICT(source_id) DO NOTHING''',
                    (source_id, source_id, card.get('pdf_sha256'),
                     json.dumps({'availability': 'metadata_only', 'notice': '原PDF与完整OCR未随公开仓库发布；可通过资料卡的公开出处核对。'}, ensure_ascii=False)))
            counts = import_evidence(conn, root)
            rules = import_philosophy(conn, root)
            relations = import_relations(conn, root)
            conn.execute("INSERT INTO schema_meta(key,value) VALUES('library_profile','curated') ON CONFLICT(key) DO UPDATE SET value='curated'")
        indexes = build_indexes(conn)
    finally:
        conn.close()
    os.chmod(db_path, 0o600)
    vectors = build_vectors(db_path)
    return {'db': str(db_path), 'profile': 'curated', **counts,
            'philosophy_rules': rules, 'relations': relations,
            'indexes': indexes, 'vectors': vectors,
            'notice': '提供已核摘要和有限片段；不声称已导入完整OCR或原PDF。'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path)
    parser.add_argument('--refresh-library', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = bootstrap(args.db, refresh=args.refresh_library)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
