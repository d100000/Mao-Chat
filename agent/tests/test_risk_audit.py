import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from agent.risk_audit import RiskAuditStore, RULES_VERSION


class RiskAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='jiaoyuan-risk-')
        self.db = Path(self.tmp.name) / 'risk.db'
        self.now = 200000.0
        self.secret = b'isolated-risk-audit-secret-32-bytes'
        self.store = RiskAuditStore(self.db, self.secret, clock=lambda: self.now)

    def tearDown(self):
        self.tmp.cleanup()

    def test_records_opaque_ids_and_no_raw_sensitive_identifiers(self):
        identity = self.store.record('auth.login', 'rate_limited',
            principal='u:private-account-17', source='192.0.2.99', operation_id='private-operation-id')
        self.assertRegex(identity, r'^risk_[a-f0-9]{32}$')
        event = self.store.list()['items'][0]
        self.assertEqual(event['risk_event_id'], identity)
        self.assertEqual(event['rules_version'], RULES_VERSION)
        for field in ('principal_hash', 'source_hash', 'operation_hash'):
            self.assertRegex(event[field], r'^[a-f0-9]{64}$')
        with sqlite3.connect(self.db) as conn:
            serialized = json.dumps(conn.execute('SELECT * FROM risk_audit_events').fetchall())
        for raw in ('u:private-account-17', '192.0.2.99', 'private-operation-id'):
            self.assertNotIn(raw, serialized)
            self.assertNotIn(raw.encode(), self.db.read_bytes())
        self.assertEqual(self.db.stat().st_mode & 0o777, 0o600)

    def test_ids_are_purpose_separated_and_normalize_source(self):
        self.store.record('generate', 'rate_limited', principal='192.0.2.1', source='::ffff:192.0.2.1')
        self.store.record('quote', 'rate_limited', source='192.0.2.1')
        rows = self.store.list()['items']
        self.assertEqual(rows[0]['source_hash'], rows[1]['source_hash'])
        self.assertNotEqual(rows[1]['source_hash'], rows[1]['principal_hash'])

    def test_operation_retry_after_restart_does_not_double_count(self):
        first = self.store.record('register', 'benefit_pending', principal='u:17',
            source='192.0.2.1', operation_id='register-1', outcome='pending')
        other = RiskAuditStore(self.db, self.secret, clock=lambda: self.now)
        self.assertEqual(first, other.record('register', 'benefit_pending', principal='u:17',
            source='198.51.100.2', operation_id='register-1', outcome='pending'))
        self.assertEqual(other.summary()['total'], 1)
        second = other.record('register', 'verified', principal='u:17',
            operation_id='register-1', outcome='allowed')
        self.assertNotEqual(first, second)
        third = other.record('register', 'benefit_pending', principal='u:18',
            operation_id='register-1', outcome='pending')
        self.assertNotEqual(first, third)
        self.assertEqual(other.summary()['total'], 3)

    def test_no_operation_means_each_decision_is_counted(self):
        one = self.store.record('login', 'credentials_rejected')
        two = self.store.record('login', 'credentials_rejected')
        self.assertNotEqual(one, two)
        self.assertEqual(self.store.summary()['total'], 2)

    def test_concurrent_workers_record_one_operation_once(self):
        barrier = threading.Barrier(8)
        results, errors = [], []
        def work():
            try:
                store = RiskAuditStore(self.db, self.secret, clock=lambda: self.now)
                barrier.wait(timeout=5)
                results.append(store.record('verify', 'verification_required',
                    principal='g:actor', operation_id='operation-123'))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=work) for _ in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=10)
        self.assertFalse(errors)
        self.assertEqual(len(results), 8)
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(self.store.summary()['total'], 1)

    def test_event_capacity_does_not_lose_rule_counts(self):
        store = RiskAuditStore(self.db, self.secret, max_events=3, clock=lambda: self.now)
        ids = []
        for index in range(7):
            ids.append(store.record('verify', 'verification_required'))
            self.now += 1
        page, summary = store.list(), store.summary()
        self.assertEqual(page['total'], 3)
        self.assertEqual({r['risk_event_id'] for r in page['items']}, set(ids[-3:]))
        self.assertEqual(summary['total'], 7)
        self.assertEqual(summary['by_rule'][0]['event_count'], 7)
        self.assertEqual(summary['retained_events'], 3)
        self.assertTrue(summary['capacity_warning'])
        self.assertTrue(summary['detail_capacity_reached'])

    def test_rule_cardinality_and_summary_response_are_bounded(self):
        store = RiskAuditStore(self.db, self.secret, max_counters=3, clock=lambda: self.now)
        for index in range(8):
            store.record('action' + str(index), 'rule' + str(index))
            self.now += 3600
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM risk_audit_counts').fetchone()[0], 3)
        summary = store.summary(limit=2)
        self.assertEqual(summary['total'], 3)
        self.assertEqual(len(summary['by_rule']), 2)
        self.assertEqual(len(summary['by_action']), 2)
        self.assertTrue(summary['has_more_rules'])
        self.assertTrue(summary['has_more_actions'])
        self.assertTrue(summary['counter_capacity_reached'])

    def test_list_pagination_filters_and_export_do_not_expose_dedupe_key(self):
        for index in range(5):
            self.store.record('login' if index % 2 else 'register', 'rate_limited',
                              outcome='blocked' if index < 3 else 'pending')
            self.now += 1
        first = self.store.list(limit=2)
        second = self.store.list(limit=2, offset=first['next_offset'])
        final = self.store.list(limit=2, offset=second['next_offset'])
        all_rows = first['items'] + second['items'] + final['items']
        self.assertEqual(len({r['risk_event_id'] for r in all_rows}), 5)
        self.assertFalse(final['has_more'])
        self.assertIsNone(final['next_offset'])
        self.assertEqual(self.store.list(action='login')['total'], 2)
        self.assertEqual(self.store.list(outcome='pending')['total'], 2)
        self.assertEqual(self.store.list(since=self.now - 2)['total'], 2)
        self.assertNotIn('dedupe_hash', all_rows[0])

    def test_expired_events_and_counters_are_hidden_before_cleanup(self):
        store = RiskAuditStore(self.db, self.secret, retention_days=1, clock=lambda: self.now)
        store.record('verify', 'verification_required', operation_id='old-operation')
        self.now += 86401
        self.assertEqual(store.list()['total'], 0)
        self.assertEqual(store.summary()['total'], 0)
        self.assertEqual(store.cleanup(), 1)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM risk_audit_counts').fetchone()[0], 0)
        self.assertEqual(store.cleanup(), 0)

    def test_summary_declares_hour_window_and_contains_no_subjects(self):
        self.store.record('generate', 'cash_budget_exhausted', principal='u:17',
                          source='192.0.2.1', operation_id='request-1')
        report = self.store.summary(since=self.now - 15, until=self.now + 1)
        self.assertEqual(report['total'], 1)
        self.assertEqual(report['bucket_seconds'], 3600)
        self.assertLessEqual(report['since'], self.now - 15)
        self.assertGreaterEqual(report['until'], self.now + 1)
        self.assertEqual(report['by_action'], {'generate': 1})
        self.assertEqual(report['by_outcome'], {'blocked': 1})
        self.assertNotIn('principal_hash', json.dumps(report))
        self.assertNotIn('source_hash', json.dumps(report))
        self.assertEqual(self.store.summary(since=self.now, until=self.now)['total'], 0)

    def test_invalid_input_never_stores_free_text_or_unbounded_queries(self):
        for kwargs in ({'action':'/api/login'}, {'action':'login\nsecret'},
                       {'code':'password is private'}, {'code':'x'*81},
                       {'outcome':'exception stack'}, {'source':'not-an-ip'},
                       {'principal':'x'*257}, {'operation_id':'x'*257}):
            values = {'action':'login','code':'rate_limited', **kwargs}
            with self.assertRaises(ValueError): self.store.record(**values)
        for kwargs in ({'limit':0}, {'limit':True}, {'limit':201}, {'offset':-1},
                       {'offset':50001}, {'since':float('nan')}, {'until':float('inf')},
                       {'since':20,'until':10}, {'since':0,'until':20000000},
                       {'since':1e300}):
            with self.assertRaises(ValueError): self.store.list(**kwargs)
        with self.assertRaises(ValueError): self.store.summary(limit=201)
        self.assertEqual(self.store.list()['total'], 0)


if __name__ == '__main__':
    unittest.main()
