import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from agent.security import SecurityService, SecurityError, client_ip


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='jiaoyuan-security-')
        self.db = Path(self.tmp.name)/'security.db'
        self.now = 1000.0
        self.service = SecurityService(self.db, b'isolated-test-key-material-32-bytes', test_mode=True, clock=lambda:self.now)

    def tearDown(self):
        self.tmp.cleanup()

    def challenge(self, actor='guest-1', action='guest_grant', operation='op-1', digest='a'*64, service=None):
        return (service or self.service).challenge(actor, action, operation, digest, '192.0.2.10')

    def proof(self, **kwargs):
        challenge = self.challenge(**kwargs)
        actor = kwargs.get('actor','guest-1')
        result = self.service.verify(challenge['challenge_id'], challenge['test_token'], actor)
        return result['proof']

    def test_untrusted_forwarding_is_ignored_and_addresses_are_normalized(self):
        self.assertEqual(client_ip('::ffff:192.0.2.10', {'X-Forwarded-For':'198.51.100.1'}), '192.0.2.10')
        self.assertEqual(client_ip('2001:0db8:0:0:0:0:0:1'), '2001:db8::1')

    def test_trusted_chain_walks_only_trusted_hops(self):
        self.assertEqual(client_ip('127.0.0.1', {'X-Forwarded-For':'198.51.100.99, 192.0.2.7'}, ['127.0.0.0/8']), '192.0.2.7')
        with self.assertRaises(SecurityError):
            client_ip('127.0.0.1', {'X-Forwarded-For':'not-an-address'}, ['127.0.0.0/8'])

    def test_sliding_rate_is_shared_across_instances_and_boundary(self):
        second = SecurityService(self.db, self.service.secret, test_mode=True, clock=lambda:self.now)
        self.assertTrue(self.service.check_rate('login','source',2,60))
        self.now += 59
        self.assertTrue(second.check_rate('login','source',2,60))
        self.now += 2
        self.assertTrue(self.service.check_rate('login','source',2,60))
        self.assertFalse(second.check_rate('login','source',2,60))
        self.assertTrue(second.check_rate('other','source',2,60))

    def test_concurrent_rate_limit_has_no_over_admission(self):
        barrier = threading.Barrier(8)
        results, errors = [], []
        def work():
            try:
                service = SecurityService(self.db, self.service.secret, test_mode=True, clock=lambda:self.now)
                barrier.wait(timeout=5)
                results.append(service.check_rate('request','same-wallet',3))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=work) for _ in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=10)
        self.assertFalse(errors)
        self.assertEqual(sum(results),3)

    def test_unconfigured_service_never_fakes_success(self):
        prod = SecurityService(self.db, self.service.secret)
        with self.assertRaises(SecurityError) as error:
            self.challenge(service=prod)
        self.assertEqual(error.exception.status,503)

    def test_test_challenge_is_unique_and_bound_to_operation(self):
        proof = self.proof()
        self.assertTrue(self.service.consume(proof,'guest-1','guest_grant','op-1','a'*64))
        # Safe retries refer to this exact immutable operation, not a new gift.
        self.assertTrue(self.service.consume(proof,'guest-1','guest_grant','op-1','a'*64))
        for args in [('guest-2','guest_grant','op-1','a'*64),('guest-1','register','op-1','a'*64),
                     ('guest-1','guest_grant','op-2','a'*64),('guest-1','guest_grant','op-1','b'*64)]:
            with self.assertRaises(SecurityError): self.service.consume(proof,*args)

    def test_proof_expiry_and_same_operation_outcome_retry(self):
        proof = self.proof()
        self.now += 121
        with self.assertRaises(SecurityError):
            self.service.consume(proof,'guest-1','guest_grant','op-1','a'*64)
        self.now += 60
        used = self.proof(actor='guest-2', operation='op-2')
        self.service.consume(used,'guest-2','guest_grant','op-2','a'*64)
        self.now += 121
        self.assertTrue(self.service.consume(used,'guest-2','guest_grant','op-2','a'*64))

    def test_challenge_actor_binding_and_attempt_limit(self):
        c = self.challenge()
        with self.assertRaises(SecurityError): self.service.verify(c['challenge_id'], c['test_token'], 'another')
        for _ in range(3):
            with self.assertRaises(SecurityError): self.service.verify(c['challenge_id'], 'wrong-token', 'guest-1')
        with self.assertRaises(SecurityError): self.service.verify(c['challenge_id'], c['test_token'], 'guest-1')

    def test_pending_challenges_are_bounded(self):
        self.challenge(operation='one'); self.challenge(operation='two')
        with self.assertRaises(SecurityError) as error: self.challenge(operation='three')
        self.assertEqual(error.exception.status,429)
        self.now += 121
        self.assertTrue(self.challenge(operation='three'))

    def test_verified_challenge_retry_does_not_need_a_new_provider_validation(self):
        c = self.challenge()
        first = self.service.verify(c['challenge_id'], c['test_token'], 'guest-1')
        second = self.service.verify(c['challenge_id'], c['test_token'], 'guest-1')
        self.assertEqual(first,second)
        with self.assertRaises(SecurityError): self.service.verify(c['challenge_id'],'other','guest-1')

    def test_provider_must_match_hostname_action_and_challenge_context(self):
        result = {'success':True,'hostname':'app.example','action':'register'}
        prod = SecurityService(self.db, self.service.secret, site_key='public-site', turnstile_secret='private-placeholder',
                               hostname='app.example', verifier=lambda token,c:dict(result), clock=lambda:self.now)
        c = self.challenge(actor='visitor',action='register',service=prod)
        for field, wrong in [('hostname','evil.example'),('action','guest_grant'),('cdata','another')]:
            result.update({'hostname':'app.example','action':'register','cdata':c['challenge_id']})
            result[field]=wrong
            with self.assertRaises(SecurityError): prod.verify(c['challenge_id'],'provider-token','visitor')
        self.now += 121
        c = self.challenge(actor='visitor',action='register',operation='second',service=prod)
        result.update({'hostname':'app.example','action':'register','cdata':c['challenge_id']})
        self.assertIn('proof',prod.verify(c['challenge_id'],'provider-token','visitor'))
        c2 = self.challenge(actor='other',action='register',operation='third',service=prod)
        result['cdata']=c2['challenge_id']
        with self.assertRaises(SecurityError): prod.verify(c2['challenge_id'],'provider-token','other')

    def test_provider_timeout_fails_closed_without_leaking_detail(self):
        def fail(*args): raise RuntimeError('private-placeholder')
        prod = SecurityService(self.db,self.service.secret,site_key='site',turnstile_secret='private-placeholder',hostname='app.example',verifier=fail,clock=lambda:self.now)
        c=self.challenge(actor='visitor',service=prod)
        with self.assertRaises(SecurityError) as error: prod.verify(c['challenge_id'],'token','visitor')
        self.assertEqual(error.exception.status,503)
        self.assertNotIn('private-placeholder',str(error.exception))

    def test_registration_risk_is_atomic_and_does_not_store_raw_identifiers(self):
        results=[self.service.record_registration('browser-private','192.0.2.99') for _ in range(4)]
        self.assertEqual([r['benefit_pending'] for r in results],[False,False,False,True])
        self.assertTrue(self.service.risk_eligibility('browser-private','192.0.2.99')['benefit_pending'])
        with sqlite3.connect(self.db) as conn:
            values=str(conn.execute('SELECT * FROM security_registrations').fetchall())
        self.assertNotIn('browser-private',values); self.assertNotIn('192.0.2.99',values)

    def test_external_verifier_concurrency_leases_are_shared_and_expire_after_crash(self):
        leases=[self.service._verification_slot() for _ in range(8)]
        restarted=SecurityService(self.db,self.service.secret,clock=lambda:self.now)
        with self.assertRaises(SecurityError) as error: restarted._verification_slot()
        self.assertEqual(error.exception.status,503)
        self.now+=16
        self.assertTrue(restarted._verification_slot())

    def test_short_window_events_expire_without_accumulating_a_day_of_requests(self):
        self.assertTrue(self.service.check_rate('short','actor',1,60))
        self.now+=61
        self.assertTrue(self.service.check_rate('short','actor',1,60))
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM security_rate_events').fetchone()[0],1)

    def test_registration_receipt_replays_original_risk_without_counting_again(self):
        first=self.service.record_registration('one-browser','192.0.2.1',operation_id='actor:register-0001')
        for index in range(3): self.service.record_registration('one-browser','192.0.2.1',operation_id='actor:extra-'+str(index))
        self.assertTrue(self.service.risk_eligibility('one-browser','192.0.2.1')['benefit_pending'])
        restarted=SecurityService(self.db,self.service.secret,clock=lambda:self.now)
        replay=restarted.record_registration('one-browser','198.51.100.2',operation_id='actor:register-0001')
        self.assertEqual(first,replay); self.assertFalse(replay['benefit_pending'])
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM security_registrations').fetchone()[0],4)
            receipts=str(conn.execute('SELECT * FROM security_registration_receipts').fetchall())
            self.assertNotIn('one-browser',receipts); self.assertNotIn('192.0.2.1',receipts)
        with self.assertRaises(SecurityError) as error:
            restarted.record_registration('another-browser','198.51.100.2',operation_id='actor:register-0001')
        self.assertEqual(error.exception.status,409)

    def test_concurrent_registration_receipt_records_one_success(self):
        barrier=threading.Barrier(6); results=[]; errors=[]
        def work():
            try:
                service=SecurityService(self.db,self.service.secret,clock=lambda:self.now)
                barrier.wait(timeout=5)
                results.append(service.record_registration('same-browser','192.0.2.1',operation_id='actor:same-operation'))
            except Exception as exc: errors.append(exc)
        threads=[threading.Thread(target=work) for _ in range(6)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=10)
        self.assertFalse(errors); self.assertEqual(len(results),6)
        self.assertTrue(all(result==results[0] for result in results))
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM security_registrations').fetchone()[0],1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM security_registration_receipts').fetchone()[0],1)


if __name__ == '__main__': unittest.main()
