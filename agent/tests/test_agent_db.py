#!/usr/bin/env python3
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agent.db import connect, ensure_schema
from agent.ingest import ingest, import_corpus, import_evidence, import_philosophy, import_relations
from agent.index import build_indexes, search, trace_page, trace_quote
from agent.vector import build_vectors, search_vectors


def full_corpus_available():
    """Optional integration assets are deliberately absent from public clones."""
    try:
        manifest = json.loads((ROOT / 'corpus/manifest.json').read_text())
        return len(manifest.get('books', [])) == 6 and all(
            (Path(book['source_file']) if Path(book['source_file']).is_absolute()
             else ROOT / book['source_file']).is_file()
            and (ROOT / 'corpus' / book['pages_file']).is_file()
            for book in manifest['books'])
    except (OSError, ValueError, KeyError, TypeError):
        return False


requires_full_corpus = unittest.skipUnless(full_corpus_available(), '可选完整PDF/OCR集成资产未安装；公开资料库测试仍执行。')


class AgentDatabaseTests(unittest.TestCase):
    @staticmethod
    def _tiny_corpus(root: Path, page_count: int):
        corpus = root / "corpus"
        corpus.mkdir(parents=True, exist_ok=True)
        rows = [
            {"source_id": "tiny", "pdf_page": page, "text": f"第{page}页"}
            for page in range(1, page_count + 1)
        ]
        (corpus / "tiny.pages.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        (corpus / "manifest.json").write_text(
            json.dumps({"books": [{"source_id": "tiny", "title": "tiny", "pages_file": "tiny.pages.jsonl"}]}, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_import_corpus_removes_deleted_pages(self):
        with tempfile.TemporaryDirectory(prefix="mao-agent-sync-") as td:
            root = Path(td)
            db = root / "agent.db"
            self._tiny_corpus(root, 2)
            conn = connect(db); ensure_schema(conn)
            import_corpus(conn, root); conn.commit()
            self._tiny_corpus(root, 1)
            import_corpus(conn, root); conn.commit()
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM passages").fetchone()[0], 1)
            conn.close()

    def test_import_evidence_and_relations_remove_deleted_records(self):
        with tempfile.TemporaryDirectory(prefix="mao-agent-evidence-sync-") as td:
            root = Path(td)
            evidence_dir = root / "skills/mao-primary-sources/references"
            evidence_dir.mkdir(parents=True)
            path = evidence_dir / "verified-evidence.jsonl"
            cards = [
                {"id": "Q1", "excerpt": "一条语录", "quote_verified": True},
                {"id": "E1", "source_role": "biographical_record", "topic": "一件事", "public_event_verified": True},
            ]
            path.write_text("".join(json.dumps(card, ensure_ascii=False) + "\n" for card in cards), encoding="utf-8")
            (root / "data").mkdir()
            relation_path = root / "data/quote_event_links.jsonl"
            relation_path.write_text(json.dumps({"quote_id": "quote:Q1", "event_id": "event:E1", "relation_type": "related", "verification_status": "reviewed"}) + "\n", encoding="utf-8")
            db = root / "agent.db"
            conn = connect(db); ensure_schema(conn)
            import_evidence(conn, root); import_relations(conn, root); conn.commit()
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM evidence_cards").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM quote_event_links").fetchone()[0], 1)
            path.write_text(json.dumps(cards[0], ensure_ascii=False) + "\n", encoding="utf-8")
            relation_path.write_text("", encoding="utf-8")
            import_evidence(conn, root); import_relations(conn, root); conn.commit()
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM evidence_cards").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM quote_event_links").fetchone()[0], 0)
            conn.close()

    def test_import_philosophy_removes_deleted_rules(self):
        with tempfile.TemporaryDirectory(prefix="mao-agent-rules-sync-") as td:
            root = Path(td)
            path = root / "skills/mao-primary-sources/references/worldview.md"
            path.parent.mkdir(parents=True)
            path.write_text("| 主题 | 规则 | 来源 | 条件 |\n| --- | --- | --- | --- |\n| 一 | 二 | 三 | 四 |\n| 五 | 六 | 七 | 八 |\n", encoding="utf-8")
            conn = connect(root / "agent.db"); ensure_schema(conn)
            import_philosophy(conn, root); conn.commit()
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM philosophy_rules").fetchone()[0], 2)
            path.write_text("| 主题 | 规则 | 来源 | 条件 |\n| --- | --- | --- | --- |\n| 一 | 二 | 三 | 四 |\n", encoding="utf-8")
            import_philosophy(conn, root); conn.commit()
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM philosophy_rules").fetchone()[0], 1)
            conn.close()

    @requires_full_corpus
    def test_schema_ingest_and_index_on_real_corpus(self):
        with tempfile.TemporaryDirectory(prefix="mao-agent-db-") as td:
            db = Path(td) / "agent.db"
            result = ingest(ROOT, db)
            self.assertEqual(result["sources"], 6)
            self.assertEqual(result["pages"], 2239)
            self.assertEqual(result["evidence_cards"], 37)
            conn = connect(db)
            indexes = build_indexes(conn)
            conn.close()
            self.assertEqual(indexes["pages"], 2239)
            self.assertGreater(indexes["passages"], 0)
            self.assertGreaterEqual(indexes["quotes"], 1)
            self.assertGreater(indexes["philosophy_rules"], 0)
            quote_hits = search(db, "矫枉必须过正", "quotes", 5)
            self.assertTrue(quote_hits)
            page = trace_page(db, "02_selected_works_v2:p0111", 1)
            self.assertEqual(len(page["pages"]), 3)

    @requires_full_corpus
    def test_ingest_is_idempotent(self):
        with tempfile.TemporaryDirectory(prefix="mao-agent-db-") as td:
            db = Path(td) / "agent.db"
            first = ingest(ROOT, db)
            second = ingest(ROOT, db)
            self.assertEqual(first["pages"], second["pages"])
            conn = connect(db)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0], 2239)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM evidence_cards").fetchone()[0], 37)
            conn.close()

    @requires_full_corpus
    def test_vector_index_and_search(self):
        with tempfile.TemporaryDirectory(prefix="mao-agent-vector-") as td:
            db = Path(td) / "agent.db"
            ingest(ROOT, db)
            conn = connect(db)
            build_indexes(conn)
            conn.close()
            result = build_vectors(db)
            self.assertEqual(result["total"], 2277)
            hits = search_vectors(db, "实践如何检验认识", "all", 5)
            self.assertEqual(len(hits), 5)
            self.assertIn(hits[0]["result_type"], {"passage", "quote", "event", "philosophy"})

    @requires_full_corpus
    def test_failed_ingest_validation_does_not_change_existing_db(self):
        with tempfile.TemporaryDirectory(prefix="mao-agent-validation-") as td:
            db = Path(td) / "agent.db"
            ingest(ROOT, db)
            before = db.read_bytes()
            with patch("agent.ingest.validate_manifest", side_effect=ValueError("manifest mismatch")):
                with self.assertRaises(ValueError):
                    ingest(ROOT, db)
            self.assertEqual(before, db.read_bytes())

    @requires_full_corpus
    def test_failed_index_rebuild_preserves_live_fts(self):
        with tempfile.TemporaryDirectory(prefix="mao-agent-fts-") as td:
            db = Path(td) / "agent.db"
            ingest(ROOT, db)
            conn = connect(db); build_indexes(conn); conn.close()
            before = search(db, "论持久战", "pages", 3)
            with patch("agent.index.indexed_text", side_effect=RuntimeError("tokenizer failed")):
                conn = connect(db)
                with self.assertRaises(RuntimeError):
                    build_indexes(conn)
                conn.close()
            self.assertEqual(len(search(db, "论持久战", "pages", 3)), len(before))

    @requires_full_corpus
    def test_search_closes_connection_for_empty_and_invalid_queries(self):
        with tempfile.TemporaryDirectory(prefix="mao-agent-index-close-") as td:
            db = Path(td) / "agent.db"
            ingest(ROOT, db)
            self.assertEqual(search(db, "", "pages", 3), [])
            with self.assertRaises(ValueError):
                search(db, "实践论", "not-a-kind", 3)

    @requires_full_corpus
    def test_reviewed_quote_event_relation_is_traceable(self):
        with tempfile.TemporaryDirectory(prefix="mao-agent-links-") as td:
            db = Path(td) / "agent.db"
            ingest(ROOT, db)
            conn = connect(db); build_indexes(conn); conn.close()
            temp_root = Path(td) / "root"; (temp_root / "data").mkdir(parents=True)
            row = {"quote_id": "quote:VL03", "event_id": "event:TL01", "relation_type": "directly_discusses", "confidence": 0.9, "verification_status": "reviewed", "evidence": {"note": "review test"}}
            (temp_root / "data/quote_event_links.jsonl").write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
            conn = connect(db); import_relations(conn, temp_root); conn.commit(); conn.close()
            related = trace_quote(db, "人民民主专政", 5)
            self.assertTrue(any(item.get("related_events") for item in related))


if __name__ == "__main__":
    unittest.main()
