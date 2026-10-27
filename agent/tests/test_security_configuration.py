"""Operator-entered verification configuration stays private and recoverable."""
import json
import sqlite3
import stat
import unittest
import uuid
from unittest.mock import patch

from agent.points import PointsError
from agent.security_config import SecurityConfigStore
from agent.tests import test_billing_admin_http as fixture


class SecurityConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.BillingAdminHTTPTests(methodName='runTest')
        self.addCleanup(self.fx.doCleanups)
        self.fx.setUp()
        self.server, self.admin, self.db = self.fx.server, self.fx.admin, self.fx.db
        self.values = {'site_key': 'isolated-site-key', 'secret': 'isolated-verifier-secret',
                       'hostname': 'localhost', 'search_call_bound_cny': '0.02', 'reason': '隔离测试人工配置'}

    def save(self, values=None, operation=None, client=None):
        return self.fx.post('/api/admin/security/config', values or self.values,
                            operation=operation, client=client)

    def test_configuration_is_authenticated_audited_and_never_exposes_secret(self):
        status, result, _ = self.save()
        self.assertEqual(status, 200, result)
        self.assertTrue(result['config']['configured'])
        self.assertTrue(result['config']['secret_configured'])
        self.assertNotIn('isolated-verifier-secret', json.dumps(result))
        self.assertEqual(self.server.security.turnstile_secret, self.values['secret'])
        self.assertEqual(self.server.search_cash_bound, '0.02')
        self.assertIn('localhost', self.server.allowed_hosts)
        status, loaded, _ = self.admin.request('/api/admin/security/config')
        self.assertEqual(status, 200)
        self.assertEqual(loaded['config'], result['config'])
        self.assertNotIn('isolated-verifier-secret', json.dumps(loaded))
        with sqlite3.connect(self.db) as conn:
            rows = conn.execute('SELECT public_json,reason,admin_id FROM security_configuration').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], self.admin.admin_id)
        self.assertNotIn('isolated-verifier-secret', repr(rows))
        self.assertEqual(SecurityConfigStore(self.db, self.server.auth_service.security_key).load()['secret'], self.values['secret'])
        folder = self.server.security_config.directory
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        for item in folder.iterdir(): self.assertEqual(stat.S_IMODE(item.stat().st_mode), 0o600)

    def test_same_operation_is_idempotent_changed_values_conflict_empty_secret_retained(self):
        operation = uuid.uuid4().hex
        status, first, _ = self.save(operation=operation)
        self.assertEqual(status, 200, first)
        self.assertEqual(self.save(operation=operation)[1], first)
        self.assertEqual(self.save({**self.values, 'hostname': 'other.example'}, operation)[0], 409)
        status, result, _ = self.save({**self.values, 'site_key': 'updated-site', 'secret': ''})
        self.assertEqual(status, 200, result)
        self.assertEqual(self.server.security.turnstile_secret, self.values['secret'])
        self.assertEqual(self.server.security.site_key, 'updated-site')
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM security_configuration').fetchone()[0], 2)

    def test_public_hostname_enables_secure_cookie_and_mfa_but_cannot_skip_mfa_setup(self):
        no_mfa = self.fx.admin_client('local_admin_without_totp', mfa=False)
        status, result, _ = self.save({**self.values, 'hostname': 'chat.example'}, client=no_mfa)
        self.assertEqual(status, 403, result)
        self.assertEqual(result['code'], 'mfa_required')
        self.assertEqual(self.server.security.hostname, '')
        status, result, _ = self.save({**self.values, 'hostname': 'chat.example'})
        self.assertEqual(status, 200, result)
        self.assertTrue(self.server.secure_cookie)
        self.assertTrue(self.server.require_admin_mfa)
        self.assertFalse(self.server.security.test_mode)
        self.assertIn('chat.example', self.server.allowed_hosts)

    def test_guests_expired_admin_and_invalid_parameters_cannot_change_configuration(self):
        status, _, _ = self.save(client=self.fx.fx.client)
        self.assertEqual(status, 401)
        self.assertEqual(self.fx.fx.client.request('/api/admin/security/config')[0], 401)
        for changes in ({'hostname': 'https://chat.example/path'}, {'search_call_bound_cny': '-1'},
                        {'search_call_bound_cny': 'NaN'}, {'search_call_bound_cny': '0.0000000000001'},
                        {'secret': 'line\nbreak'}, {'unexpected': True}, {'reason': ''}):
            status, result, _ = self.save({**self.values, **changes})
            self.assertEqual(status, 400, result)
        with sqlite3.connect(self.db) as conn:
            conn.execute('UPDATE admin_sessions SET recent_authenticated_at=1,mfa_verified_at=1 WHERE session_id=?', (self.admin.session_id,))
        self.assertEqual(self.save()[0], 403)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM security_configuration').fetchone()[0], 0)

    def test_failed_file_publish_leaves_committed_version_and_no_orphan_temp(self):
        self.assertEqual(self.save()[0], 200)
        with patch('agent.security_config.os.replace', side_effect=OSError('isolated publish fault')):
            with self.assertRaises(OSError):
                self.server.security_config.update({'site_key': 'not-published'}, self.admin.admin_id, uuid.uuid4().hex, '模拟文件写入失败')
        self.assertEqual(self.server.security_config.load()['site_key'], self.values['site_key'])
        self.assertEqual(len(list(self.server.security_config.directory.iterdir())), 1)
        path = next(self.server.security_config.directory.iterdir())
        path.write_text('{}')
        with self.assertRaises(PointsError): self.server.security_config.load()
        status, result, _ = self.save()
        self.assertEqual(status, 200, result)
        self.assertTrue(result['config']['configured'])


if __name__ == '__main__': unittest.main()
