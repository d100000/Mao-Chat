"""Conversation snapshots survive interrupted publication without crossing owners."""
import json
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

from agent.runtime import Agent, AgentConfig
from agent.llm import LLMConfig
from agent.firecrawl import FirecrawlConfig


class SessionDurabilityTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='session-durability-')
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.agent = Agent(AgentConfig(llm=LLMConfig(), firecrawl=FirecrawlConfig(),
                                      db_path=self.root / 'test.db', session_dir=self.root / 'sessions'))
        self.sid = 'u7_private'
        self.turn = 'a' * 32
        self.messages = [{'role': 'user', 'content': '原问题', 'turn_id': self.turn, 'evidence_ids': []},
                         {'role': 'assistant', 'content': '已保存回答', 'turn_id': self.turn, 'evidence_ids': ['E01']}]
        self.metadata = {'mode': 'historical', 'as_of': '1938-06-30', 'latest_turn_id': self.turn}
        self.path = self.agent.save_session(self.sid, self.messages, self.metadata)

    def test_replace_failure_retains_last_complete_file_and_database(self):
        before = self.path.read_bytes()
        with patch('agent.runtime.os.replace', side_effect=OSError('isolated interrupted publish')):
            with self.assertRaises(OSError):
                self.agent.save_session(self.sid, [{'role': 'user', 'content': '未提交内容'}])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.agent.load_session(self.sid), self.messages)
        self.path.unlink()
        self.assertEqual(self.agent.load_session(self.sid), self.messages)
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_fsync_failure_never_publishes_partial_snapshot(self):
        before = self.path.read_bytes()
        with patch('agent.runtime.os.fsync', side_effect=OSError('isolated disk fault')):
            with self.assertRaises(OSError): self.agent.save_session(self.sid, [])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_missing_corrupt_and_non_object_snapshots_recover_exact_turn(self):
        for value in (None, '{truncated', 'null', '{"messages": null}'):
            with self.subTest(value=value):
                if value is None: self.path.unlink(missing_ok=True)
                else: self.path.write_text(value)
                self.assertEqual(self.agent.load_session(self.sid), self.messages)
                self.assertEqual(self.agent.session_metadata(self.sid), self.metadata)
        self.assertEqual(self.agent.load_session('u8_private'), [])
        self.assertEqual(self.agent.session_metadata('u8_private'), {})
        with self.assertRaises(ValueError): self.agent.load_session('../u7_private')

    def test_atomic_snapshot_and_existing_directory_are_private(self):
        self.path.chmod(0o644)
        self.path.parent.chmod(0o755)
        self.agent.save_session(self.sid, self.messages, self.metadata)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(json.loads(self.path.read_text())['messages'], self.messages)

    def test_bounded_database_recovery_keeps_latest_delivery(self):
        values = [{'role': 'user', 'content': str(index)} for index in range(1001)]
        values.append(self.messages[-1])
        self.agent.save_session(self.sid, values, self.metadata)
        self.path.write_text('{interrupted snapshot')
        recovered = self.agent.load_session(self.sid)
        self.assertEqual(len(recovered), 1000)
        self.assertEqual(recovered[0]['content'], '2')
        self.assertEqual(recovered[-1], self.messages[-1])
        self.assertEqual(self.agent.session_metadata(self.sid)['latest_turn_id'], self.turn)

    def test_deleted_database_copy_cannot_resurrect_and_legacy_metadata_is_bounded(self):
        with sqlite3.connect(self.agent.config.db_path) as conn:
            conn.execute('UPDATE messages SET metadata_json=? WHERE session_id=?', ('null', self.sid))
        self.path.unlink()
        loaded = self.agent.load_session(self.sid)
        self.assertEqual([m['content'] for m in loaded], ['原问题', '已保存回答'])
        self.assertTrue(all('turn_id' not in m for m in loaded))
        with sqlite3.connect(self.agent.config.db_path) as conn:
            conn.execute('DELETE FROM messages WHERE session_id=?', (self.sid,))
            conn.execute('DELETE FROM sessions WHERE session_id=?', (self.sid,))
        self.assertEqual(self.agent.load_session(self.sid), [])
        self.assertEqual(self.agent.session_metadata(self.sid), {})


if __name__ == '__main__': unittest.main()
