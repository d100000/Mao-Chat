import tempfile
from pathlib import Path
import unittest

from agent.db import connect
from agent.dialogue import CuratedMethodLibrary
from agent.principles import PRINCIPLES
from scripts.bootstrap_library import bootstrap


class CuratedBootstrapTests(unittest.TestCase):
    def test_curated_startup_needs_no_pdf_and_preserves_existing_private_tables(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / 'library.db'
            result = bootstrap(db)
            self.assertEqual(result['profile'], 'curated')
            conn = connect(db)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM evidence_cards').fetchone()[0], 37)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM pages').fetchone()[0], 0)
            conn.execute('CREATE TABLE private_marker(value TEXT)')
            conn.execute("INSERT INTO private_marker VALUES('keep')")
            conn.commit(); conn.close()
            with self.assertRaises(ValueError):
                bootstrap(db)
            bootstrap(db, refresh=True)
            conn = connect(db)
            self.assertEqual(conn.execute('SELECT value FROM private_marker').fetchone()[0], 'keep')
            conn.close()
            library = CuratedMethodLibrary(db)
            for principle in PRINCIPLES:
                traced = library.verify_citations([principle['principle_id']])
                self.assertEqual(traced['checks'][0]['status'], 'matched', principle['principle_id'])
                self.assertFalse(traced['principles'][0]['quote_eligible'])
            result = library.search('调查研究')
            self.assertTrue(result['results'])


if __name__ == '__main__':
    unittest.main()
