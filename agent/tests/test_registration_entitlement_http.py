"""A client-controlled cache cookie cannot reset registration risk identity."""
import sqlite3
import unittest
import uuid

from agent.tests import test_credits_http as fixture


class RegistrationEntitlementHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixture.CreditsHTTPTests(methodName='runTest')
        self.addCleanup(self.fx.doCleanups)
        self.fx.setUp()

    def test_rotating_raw_trial_cookie_does_not_reset_server_entitlement_counter(self):
        self.fx.bootstrap()
        original_guest = self.fx.client.cookies['jiaoyuan_guest']
        replies = []
        for index in range(4):
            self.fx.client.cookies['jiaoyuan_trial'] = 'untrusted-' + uuid.uuid4().hex
            payload, _ = self.fx.prove('/api/auth/register',
                {'username': 'entitlement_user_' + str(index), 'password': 'isolated-long-password-123'})
            status, result, _ = self.fx.post('/api/auth/register', payload)
            self.assertEqual(status, 200, result)
            replies.append(result)
            self.assertEqual(self.fx.client.cookies['jiaoyuan_guest'], original_guest)
            # Replaying the same accepted operation returns its receipt and
            # cannot count a second registration or issue a second benefit.
            status, again, _ = self.fx.post('/api/auth/register', payload)
            self.assertEqual(status, 200, again)
            self.assertEqual(again, result)
        self.assertEqual([r['benefit_pending'] for r in replies], [False, False, False, True])
        actor = self.fx.server.points.user_actor(replies[-1]['user_id'])
        wallet = self.fx.server.points.wallet(actor)
        self.assertTrue(wallet['benefit_pending'])
        self.assertEqual(wallet['available'], '0.000000')
        with sqlite3.connect(self.fx.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(DISTINCT browser_hash),COUNT(*) FROM security_registrations').fetchone(), (1, 4))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM points_lots WHERE source='guest_trial'").fetchone()[0], 1)


if __name__ == '__main__': unittest.main()
