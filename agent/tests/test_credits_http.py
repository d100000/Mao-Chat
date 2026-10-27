"""Isolated real-HTTP credit flows; all prices and model usage are test data."""
from __future__ import annotations

import http.client
from http import cookies
import json
import os
from pathlib import Path
import sqlite3
import socket
import struct
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from agent import auth_server
from agent.admin_config import AdminConfigStore
from agent.billing_runtime import before_model_request, record_billed_call
from agent.runtime import Agent
from agent.telemetry import record_model_call
from agent.llm import OpenAICompatibleClient
from agent.firecrawl import FirecrawlClient


class CookieClient:
    def __init__(self, server):
        self.server=server
        self.cookies={}

    def request(self,path,method='GET',data=None,*,headers=None,raw=None,on_headers=None):
        body=raw if raw is not None else (json.dumps(data,ensure_ascii=False).encode() if data is not None else None)
        values={'Content-Type':'application/json'}
        if self.cookies:
            values['Cookie']='; '.join(k+'='+v for k,v in self.cookies.items())
        values.update(headers or {})
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_address[1],timeout=10)
        try:
            conn.request(method,path,body=body,headers=values)
            response=conn.getresponse();status=response.status
            response_headers=response.getheaders();content_type=response.getheader('Content-Type','')
            if on_headers:on_headers(status,response_headers)
            payload=response.read()
            for key,value in response_headers:
                if key.lower()=='set-cookie':
                    jar=cookies.SimpleCookie();jar.load(value)
                    for name,morsel in jar.items():
                        if morsel['max-age']=='0':self.cookies.pop(name,None)
                        else:self.cookies[name]=morsel.value
            if 'text/event-stream' in content_type:
                value=[json.loads(line[6:]) for line in payload.decode().splitlines() if line.startswith('data: ')]
            elif 'application/json' in content_type:
                value=json.loads(payload)
            else:
                value=payload.decode(errors='replace')
            return status,value,response_headers
        finally:
            conn.close()


class BilledHTTPAgent(Agent):
    """Persist real sessions while faking the remote model at its billing seam."""
    def __init__(self,config):
        super().__init__(config)
        self.executions=0
        self.usage_complete=True
        self.pause=False
        self.started=threading.Event()
        self.finish_gate=threading.Event()
        self.fail_after_bill=False

    def stream(self,question,mode='research',as_of=None,history=None,show_reasoning=False,network_enabled=True):
        self.executions+=1
        payload={'model':self.config.llm.model,'messages':[{'role':'user','content':question}],
                 'max_tokens':self.config.llm.max_tokens,'stream':True}
        before_model_request(payload,self.config.llm)
        usage={'prompt_tokens':123,'completion_tokens':45} if self.usage_complete else None
        record_billed_call(usage,
                           provider_request_id='isolated-http-'+str(self.executions))
        record_model_call(usage,1)
        if self.pause:
            self.started.set()
            if not self.finish_gate.wait(timeout=5):
                raise RuntimeError('isolated test worker timed out')
        if self.fail_after_bill:
            yield {'type':'error','error':'隔离HTTP测试供应商故障'}
            return
        yield {'type':'content','text':'这是隔离HTTP测试的回答，不是模型实测。'}
        yield {'type':'done','steps':1}


class WireResponse:
    def __init__(self,body):self.body=body
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def read(self,n=-1):return self.body if n<0 else self.body[:n]
    def __iter__(self):return iter(self.body.splitlines(True))


class WireHTTPAgent(Agent):
    """The actual LLM client and SSE parser with an in-process HTTP opener."""
    def __init__(self,config,*,cached=False,missing_usage=False,extra_system=''):
        super().__init__(config)
        self.executions=0;self.payloads=[]
        self.cached,self.missing_usage,self.extra_system=cached,missing_usage,extra_system
        self.client=OpenAICompatibleClient(config.llm,opener=self._open)

    def _open(self,req,timeout=None):
        self.executions+=1
        payload=json.loads(req.data);self.payloads.append(payload)
        identity='isolated-wire-'+uuid.uuid4().hex
        usage={'prompt_tokens':123,'completion_tokens':45,'total_tokens':168}
        if self.cached:
            usage.update(prompt_cache_hit_tokens=23,prompt_cache_miss_tokens=100,
                         prompt_tokens_details={'cached_tokens':23},completion_tokens_details={'reasoning_tokens':12})
        answer='这是通过实际协议解析器的隔离测试回答。'
        if payload['stream']:
            frames=[{'id':identity,'choices':[{'delta':{'content':answer}}]}]
            if not self.missing_usage:frames.append({'id':identity,'choices':[],'usage':usage})
            raw=''.join('data: '+json.dumps(x,ensure_ascii=False)+'\n\n' for x in frames)+'data: [DONE]\n\n'
        else:
            result={'id':identity,'choices':[{'message':{'content':answer}}]}
            if not self.missing_usage:result['usage']=usage
            raw=json.dumps(result,ensure_ascii=False)
        return WireResponse(raw.encode())

    def stream(self,question,mode='research',as_of=None,history=None,show_reasoning=False,network_enabled=True):
        messages=[{'role':'system','content':'合成HTTP测试 '+self.extra_system},*(history or []),{'role':'user','content':question}]
        response=self.client.chat(messages)
        yield {'type':'content','text':response.content}
        yield {'type':'done','steps':1}


class SearchWireHTTPAgent(WireHTTPAgent):
    def __init__(self,config):
        super().__init__(config)
        self.searches=0
        self.firecrawl=FirecrawlClient(config.firecrawl,opener=self._search_open)

    def _search_open(self,req,timeout=None):
        self.searches+=1
        return WireResponse(json.dumps({'data':[{'title':'隔离搜索候选','url':'https://evidence.invalid/test',
            'markdown':'合成网页候选，不是核验史料'}]}).encode())

    def stream(self,question,mode='research',as_of=None,history=None,show_reasoning=False,network_enabled=True):
        if network_enabled:
            result=self._firecrawl_search(question,limit=1,network_enabled=True)
            yield {'type':'tool','name':'firecrawl_search','result':result}
        yield from super().stream(question,mode,as_of,history,show_reasoning,network_enabled)


class CreditsHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='jiaoyuan-credits-http-')
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name);self.db=self.path/'test.db'
        env={'MAO_AGENT_DB':str(self.db),'MAO_SESSION_DIR':str(self.path/'sessions'),
             'MAO_ADMIN_SECRET_FILE':str(self.path/'provider-secrets'),
             'MAO_AUTH_SECRET':'isolated-http-credit-test-only-secret-32-bytes',
             'MAO_LLM_BASE_URL':'https://provider.invalid/v1','MAO_LLM_MODEL':'billing-http-test',
             'MAO_LLM_API_KEY':'isolated-test-placeholder','MAO_LLM_MAX_TOKENS':'100',
             'FIRECRAWL_API_KEY':'isolated-search-test-placeholder','FIRECRAWL_API_KEY_ENV':'FIRECRAWL_API_KEY',
             'MAO_AGENT_MAX_STEPS':'3','MAO_PUBLIC_HOSTNAME':'','MAO_TRUSTED_PROXIES':'',
             'MAO_TURNSTILE_SITE_KEY':'','MAO_TURNSTILE_SECRET':'',
             'MAO_ADMIN_USERNAME':'','MAO_ADMIN_PASSWORD':''}
        environment=patch.dict(os.environ,env);environment.start();self.addCleanup(environment.stop)
        for name,value in [('_keychain_get',''),('_keychain_set',False),('_keychain_delete',None)]:
            change=patch.object(AdminConfigStore,name,return_value=value);change.start();self.addCleanup(change.stop)
        self.server=auth_server.make_server('127.0.0.1',0,self.db,security_test_mode=True)
        config=self.server.agent.config
        self.server.agent=BilledHTTPAgent(config)
        self.addCleanup(self.server.agent.finish_gate.set)
        self.server.migration.agent=self.server.agent
        self.server.points.configure({'daily_cash_limit_cny':'1'})
        # Deliberately fictional low rates for deterministic tests. They are
        # never copied to any production database or treated as gateway fees.
        self.price=self.server.points.publish_price({
            'provider':config.llm.provider,'gateway':config.llm.base_url,'model':config.llm.model,
            'rates':{'input_uncached':'0.001','cache_read':'0.001','cache_write':'0.001','billable_output':'0.002'},
            'adapter':'openai','cache_read_supported':False,'cache_write_supported':False})
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={'poll_interval':.01},daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.client=CookieClient(self.server)

    def stop_server(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=5)

    def post(self,path,data,client=None,**kw):
        return (client or self.client).request(path,'POST',data,**kw)

    def bootstrap(self,client=None):
        status,data,headers=self.post('/api/guest/bootstrap',{},client)
        self.assertEqual(status,200,data)
        return data

    def prove(self,path,payload,client=None):
        client=client or self.client
        value={**payload,'operation_id':payload.get('operation_id',uuid.uuid4().hex)}
        status,response,_=self.post(path,value,client)
        self.assertEqual(status,403,response)
        self.assertEqual(response.get('code'),'challenge_required',response)
        challenge=response['challenge']
        self.assertEqual(challenge['provider'],'test')
        status,verified,_=self.post('/api/security/verify',{'challenge_id':challenge['challenge_id'],'token':challenge['test_token']},client)
        self.assertEqual(status,200,verified)
        value['security_proof']=verified['proof']
        return value,challenge

    def quoted(self,*,client=None,question='我想讨论一件具体事情',thread_id='http-topic',**extra):
        client=client or self.client
        payload={'question':question,'mode':'research','thread_id':thread_id,'temporary':False,'search':False,
                 'client_request_id':uuid.uuid4().hex,**extra}
        status,result,_=self.post('/api/points/quote',payload,client)
        if status==403 and result.get('code')=='challenge_required':
            challenge=result['challenge']
            status,verified,_=self.post('/api/security/verify',{'challenge_id':challenge['challenge_id'],'token':challenge['test_token']},client)
            self.assertEqual(status,200,verified)
            payload['security_proof']=verified['proof']
            status,result,_=self.post('/api/points/quote',payload,client)
        self.assertEqual(status,200,result)
        return payload,result

    def authenticated(self,client=None,username='http_user'):
        client=client or self.client
        self.bootstrap(client)
        credentials={'username':username,'password':'isolated-long-password-123'}
        request,_=self.prove('/api/auth/register',credentials,client)
        status,registered,_=self.post('/api/auth/register',request,client)
        self.assertEqual(status,200,registered)
        request,_=self.prove('/api/auth/login',credentials,client)
        status,logged,_=self.post('/api/auth/login',request,client)
        self.assertEqual(status,200,logged)
        self.assertNotIn('session_token',logged)
        return logged['user']

    def fixture_topic(self,actor,tid='private-topic',text='这是一条私有测试记录'):
        self.server.agent.save_session(self.server.issues.session_id(actor['storage_id'],tid),
            [{'role':'user','content':text},{'role':'assistant','content':'隔离测试回答'}],
            {'mode':'research','as_of':None,'title':'私有测试议题'})

    def test_public_bootstrap_separates_capabilities_without_grant(self):
        result=self.bootstrap()
        self.assertEqual(result['wallet']['available'],'0.000000')
        self.assertEqual(result['actor']['kind'],'guest')
        self.assertLess(result['actor']['storage_id'],0)
        self.assertEqual(set(self.client.cookies),{'jiaoyuan_guest','jiaoyuan_trial','jiaoyuan_wallet'})
        self.assertEqual(len(set(self.client.cookies.values())),3)
        again=self.bootstrap()
        self.assertEqual(again['actor']['principal'],result['actor']['principal'])
        self.assertEqual(again['wallet']['available'],'0.000000')

    def test_valid_challenge_and_quote_activate_trial_only_once(self):
        self.bootstrap()
        payload,quote=self.quoted()
        self.assertEqual(quote['wallet']['available'],'100.000000')
        self.assertGreater(quote['max_micro'],0)
        status,again,_=self.post('/api/points/quote',payload)
        self.assertEqual(status,200,again)
        self.assertEqual(again['wallet']['available'],'100.000000')
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM points_lots WHERE source='guest_trial'").fetchone()[0],1)

    def test_proof_cannot_be_rebound_to_changed_content_or_another_guest(self):
        self.bootstrap()
        payload,_=self.prove('/api/points/quote',{'question':'原来的问题','mode':'research','thread_id':'proof-topic','client_request_id':uuid.uuid4().hex})
        status,result,_=self.post('/api/points/quote',{**payload,'question':'篡改过的问题'})
        self.assertEqual(status,403,result)
        other=CookieClient(self.server);self.bootstrap(other)
        status,result,_=self.post('/api/points/quote',payload,other)
        self.assertEqual(status,403,result)
        self.assertEqual(self.client.request('/api/points/wallet')[1]['wallet']['available'],'0.000000')

    def test_client_verifier_success_cannot_award_credits(self):
        self.bootstrap()
        status,result,_=self.post('/api/security/verify',{'success':True,'challenge_id':'fake-challenge','token':'made-up-token'})
        self.assertEqual(status,403,result)
        self.assertEqual(self.client.request('/api/points/wallet')[1]['wallet']['available'],'0.000000')

    def test_unbootstrapped_and_admin_routes_require_identity(self):
        for path in ['/api/points/wallet','/api/points/ledger','/api/agent/sessions','/api/admin/billing']:
            status,result,_=self.client.request(path)
            self.assertEqual(status,401,(path,result))
        for path in ['/api/admin/billing/prices','/api/admin/billing/policy']:
            status,result,_=self.post(path,{'daily_cash_limit_cny':'100'})
            self.assertEqual(status,401,(path,result))

    def test_two_guests_on_same_source_cannot_read_or_modify_each_other(self):
        first=self.bootstrap()['actor']
        self.fixture_topic(first)
        other=CookieClient(self.server);second=self.bootstrap(other)['actor']
        self.assertNotEqual(first['storage_id'],second['storage_id'])
        status,own,_=self.client.request('/api/agent/session?thread_id=private-topic')
        self.assertEqual(status,200,own)
        self.assertTrue(own['messages'])
        status,foreign,_=other.request('/api/agent/session?thread_id=private-topic&user_id='+str(first['storage_id']))
        self.assertEqual(status,200,foreign)
        self.assertEqual(foreign['messages'],[])
        self.assertEqual(other.request('/api/agent/sessions')[1]['sessions'],[])
        status,_,_=other.request('/api/agent/session?thread_id=private-topic','DELETE')
        self.assertGreaterEqual(status,400)
        self.assertTrue(self.client.request('/api/agent/session?thread_id=private-topic')[1]['messages'])

    def test_unknown_prices_and_cash_budget_are_explicit_fail_closed(self):
        self.bootstrap()
        self.server.points.disable_price(self.price['id'])
        payload,_=self.prove('/api/points/quote',{'question':'价格未配置','mode':'research','client_request_id':uuid.uuid4().hex})
        status,result,_=self.post('/api/points/quote',payload)
        self.assertEqual(status,422,result)
        self.assertEqual(result['code'],'price_unavailable')
        self.assertEqual(self.server.agent.executions,0)
        self.assertEqual(self.server.points.summary()['cash_committed_cny'],'0')

    def test_no_production_verifier_cannot_silently_accept_math_or_test_token(self):
        self.bootstrap()
        self.server.security.test_mode=False
        status,result,_=self.post('/api/points/quote',{'question':'真实验证器缺失','mode':'research','client_request_id':uuid.uuid4().hex,'math_answer':'1','math_challenge_id':'fake'})
        self.assertEqual(status,400,result)
        status,result,_=self.post('/api/points/quote',{'question':'真实验证器缺失','mode':'research','client_request_id':uuid.uuid4().hex})
        self.assertEqual(status,503,result)
        self.assertEqual(result['code'],'verification_unavailable')

    def test_extra_money_fields_duplicate_json_and_nonfinite_numbers_are_rejected(self):
        self.bootstrap()
        base={'question':'不能相信本机余额','mode':'research','client_request_id':uuid.uuid4().hex}
        for key in ['available','points','usage','price','benefit_date','user_id']:
            status,result,_=self.post('/api/points/quote',{**base,key:999999})
            self.assertEqual(status,400,(key,result))
        for raw in [b'{"question":"a","question":"b"}',b'{"question":NaN}',b'{"question":Infinity}']:
            status,result,_=self.post('/api/points/quote',None,raw=raw)
            self.assertEqual(status,400,result)
        self.assertEqual(self.client.request('/api/points/wallet')[1]['wallet']['available'],'0.000000')

    def test_host_and_origin_spoofing_are_rejected_before_state_changes(self):
        for headers in [{'Host':'attacker.invalid'},{'Origin':'https://attacker.invalid'},{'Host':'attacker.invalid','Origin':'http://attacker.invalid'}]:
            status,result,_=self.post('/api/guest/bootstrap',{},headers=headers)
            self.assertEqual(status,403,result)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM points_guests').fetchone()[0],0)

    def test_spoofed_forwarding_headers_cannot_reset_source_limits(self):
        for i in range(60):
            status,result,_=self.post('/api/guest/bootstrap',{},headers={'X-Forwarded-For':f'192.0.2.{i+1}'})
            self.assertEqual(status,200,result)
        status,result,headers=self.post('/api/guest/bootstrap',{},headers={'X-Forwarded-For':'203.0.113.99'})
        self.assertEqual(status,429,result)
        self.assertIn('Retry-After',dict(headers))

    def test_register_login_daily_grant_and_explicit_guest_claim(self):
        self.bootstrap()
        guest_actor=self.client.request('/api/auth/me')[1]['actor']
        self.fixture_topic(guest_actor)
        account=self.authenticated()
        status,me,_=self.client.request('/api/auth/me')
        self.assertEqual(status,200,me)
        self.assertFalse(me['user']['is_guest'])
        status,wallet,_=self.client.request('/api/points/wallet')
        self.assertEqual(wallet['wallet']['available'],'100.000000')
        claim_id=uuid.uuid4().hex
        status,claimed,_=self.post('/api/account/guest-claim',{'claim_id':claim_id,'include_wallet':True,'include_conversations':True})
        self.assertEqual(status,200,claimed)
        self.assertEqual(claimed['state'],'committed')
        status,wallet,_=self.client.request('/api/points/wallet')
        self.assertEqual(wallet['wallet']['available'],'200.000000')
        status,topics,_=self.client.request('/api/agent/sessions')
        self.assertEqual(topics['total'],1)
        self.assertEqual(topics['sessions'][0]['thread_id'],'private-topic')
        self.assertEqual(self.server.points.content_owner(guest_actor['storage_id']),account['user_id'])
        status,replayed,_=self.post('/api/account/guest-claim',{'claim_id':claim_id,'include_wallet':True,'include_conversations':True})
        self.assertEqual(status,200,replayed)
        self.assertEqual(self.client.request('/api/points/wallet')[1]['wallet']['available'],'200.000000')

    def test_logout_revokes_account_cache_scope_without_new_trial(self):
        self.authenticated()
        account_actor=self.client.request('/api/auth/me')[1]['actor']
        self.fixture_topic(account_actor,tid='account-only-topic')
        account_cookie=self.client.cookies['mao_session']
        status,result,_=self.post('/api/auth/logout',{})
        self.assertEqual(status,200,result)
        self.assertNotIn('mao_session',self.client.cookies)
        self.assertEqual(self.client.request('/api/auth/me')[0],401)
        self.bootstrap()
        status,me,_=self.client.request('/api/auth/me')
        self.assertEqual(status,200,me)
        self.assertTrue(me['user']['is_guest'])
        self.assertEqual(self.client.request('/api/agent/sessions')[1]['sessions'],[])
        # The original anonymous trial is retained until claimed; logout does
        # not issue a fresh trial grant and cannot restore the account cookie.
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM points_lots WHERE source='guest_trial'").fetchone()[0],1)
        revoked=CookieClient(self.server);revoked.cookies['mao_session']=account_cookie
        self.assertEqual(revoked.request('/api/auth/me')[0],401)

    def test_unauthenticated_logout_cannot_bypass_bounded_bootstrap_creation(self):
        with sqlite3.connect(self.db) as conn:
            before=conn.execute('SELECT COUNT(*) FROM points_guests').fetchone()[0]
        for _ in range(70):
            status,result,_=self.post('/api/auth/logout',{})
            self.assertIn(status,{200,429},result)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM points_guests').fetchone()[0],before)
        self.assertEqual(self.client.request('/api/auth/me')[0],401)
        for _ in range(60):
            status,result,_=self.post('/api/guest/bootstrap',{})
            self.assertEqual(status,200,result)
        status,result,_=self.post('/api/guest/bootstrap',{})
        self.assertEqual(status,429,result)

    def test_explicit_test_verifier_cannot_bind_wildcard_public_listener(self):
        created=[]
        original=auth_server.AuthHTTPServer.__init__
        def capture(instance,*args,**kwargs):
            created.append(instance)
            return original(instance,*args,**kwargs)
        try:
            with patch.object(auth_server.AuthHTTPServer,'__init__',capture):
                with self.assertRaises(ValueError):
                    auth_server.make_server('0.0.0.0',0,self.path/'wildcard.db',security_test_mode=True)
        finally:
            for server in created:
                try:server.server_close()
                except AttributeError:pass

    def test_limit_bounds_apply_to_public_ledger(self):
        self.bootstrap()
        for value in ['1000000','-1','NaN']:
            status,result,_=self.client.request('/api/points/ledger?limit='+value)
            self.assertEqual(status,400,result)

    def test_authentication_response_retry_returns_original_registration(self):
        self.bootstrap()
        payload,_=self.prove('/api/auth/register',{'username':'retry_user','password':'isolated-long-password-123'})
        status,first,_=self.post('/api/auth/register',payload)
        self.assertEqual(status,200,first)
        status,second,_=self.post('/api/auth/register',payload)
        self.assertEqual(status,200,second)
        self.assertEqual(second['user_id'],first['user_id'])
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM users').fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM points_lots WHERE source='guest_trial'").fetchone()[0],1)

    def test_deduplication_marker_cannot_authenticate_content_or_recover_wallet(self):
        actor=self.bootstrap()['actor']
        self.fixture_topic(actor)
        self.quoted()
        attacker=CookieClient(self.server)
        attacker.cookies['jiaoyuan_guest']=self.client.cookies['jiaoyuan_trial']
        attacker.cookies['jiaoyuan_trial']=self.client.cookies['jiaoyuan_trial']
        self.assertEqual(attacker.request('/api/points/wallet')[0],401)
        self.assertEqual(attacker.request('/api/agent/session?thread_id=private-topic')[0],401)
        new=self.bootstrap(attacker)
        self.assertEqual(new['wallet']['available'],'0.000000')
        self.assertNotEqual(new['actor']['storage_id'],actor['storage_id'])
        self.assertEqual(attacker.request('/api/agent/sessions')[1]['sessions'],[])

    def test_client_parameter_cannot_enable_production_test_verifier(self):
        self.server.security.test_mode=False
        status,result,_=self.post('/api/guest/bootstrap',{'security_test_mode':True,'test_mode':True})
        self.assertEqual(status,200,result)
        self.assertEqual(result['security']['provider'],'turnstile')
        self.assertFalse(result['security']['configured'])
        status,result,_=self.post('/api/points/quote',{'question':'不能从HTTP启用测试验证器','mode':'research','client_request_id':uuid.uuid4().hex})
        self.assertEqual(status,503,result)
        self.assertNotIn('test_token',json.dumps(result))

    def test_stream_requires_quote_and_does_not_start_model_without_authorization(self):
        self.bootstrap()
        status,result,_=self.post('/api/agent/stream',{'question':'没有报价不能调用','mode':'research','thread_id':'no-quote','client_request_id':uuid.uuid4().hex})
        self.assertIn(status,{400,402,422},result)
        self.assertEqual(self.server.agent.executions,0)
        self.assertEqual(self.client.request('/api/points/wallet')[1]['wallet']['held_micro'],0)

    def test_stream_bills_verified_usage_once_and_recovery_replays_saved_answer(self):
        self.bootstrap()
        payload,quote=self.quoted()
        submitted={**payload,'quote_id':quote['quote_id']}
        status,events,_=self.post('/api/agent/stream',submitted)
        self.assertEqual(status,200,events)
        kinds=[x['type'] for x in events]
        self.assertIn('accepted',kinds,events)
        self.assertIn('settled',kinds,events)
        self.assertIn('done',kinds,events)
        request_id=next(x['request_id'] for x in events if x['type']=='accepted')
        status,result,_=self.client.request('/api/points/requests/'+request_id)
        self.assertEqual(status,200,result)
        self.assertEqual(result['state'],'settled')
        self.assertEqual(result['charged_points'],'0.000213')
        self.assertIn('隔离HTTP测试',result['answer'])
        self.assertEqual(result['wallet']['held_micro'],0)
        status,replayed,_=self.post('/api/agent/stream',submitted)
        self.assertEqual(status,200,replayed)
        self.assertEqual(self.server.agent.executions,1)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM points_ledger WHERE event='settle'").fetchone()[0],1)
        stranger=CookieClient(self.server);self.bootstrap(stranger)
        self.assertEqual(stranger.request('/api/points/requests/'+request_id)[0],404)

    def test_quoted_content_or_snapshot_changes_reject_before_model(self):
        actor=self.bootstrap()['actor']
        payload,quote=self.quoted()
        status,result,_=self.post('/api/agent/stream',{**payload,'quote_id':quote['quote_id'],'question':'换成另一问题'})
        self.assertIn(status,{409,422},result)
        self.fixture_topic(actor,tid=payload['thread_id'],text='报价后改变了真实上下文')
        status,result,_=self.post('/api/agent/stream',{**payload,'quote_id':quote['quote_id']})
        self.assertEqual(status,422,result)
        self.assertEqual(self.server.agent.executions,0)

    def test_expired_quote_rejects_without_spending_or_model_call(self):
        self.bootstrap()
        payload,quote=self.quoted()
        original=self.server.points.clock
        moment=original()
        self.server.points.clock=lambda:moment+301
        status,result,_=self.post('/api/agent/stream',{**payload,'quote_id':quote['quote_id']})
        self.assertEqual(status,422,result)
        self.assertEqual(self.server.agent.executions,0)
        self.assertEqual(self.client.request('/api/points/wallet')[1]['wallet']['available'],'100.000000')

    def test_missing_usage_stream_is_pending_instead_of_silently_free(self):
        self.bootstrap()
        self.server.agent.usage_complete=False
        payload,quote=self.quoted()
        status,events,_=self.post('/api/agent/stream',{**payload,'quote_id':quote['quote_id']})
        self.assertEqual(status,200,events)
        self.assertIn('usage_pending',[x['type'] for x in events],events)
        request_id=next(x['request_id'] for x in events if x['type']=='accepted')
        status,result,_=self.client.request('/api/points/requests/'+request_id)
        self.assertEqual(status,200,result)
        self.assertEqual(result['state'],'usage_pending')
        self.assertGreater(result['wallet']['held_micro'],0)
        self.assertEqual(result['charged_micro'],0)
        self.assertIn('隔离HTTP测试',result['answer'])

    def test_missing_cash_budget_rejects_stream_before_provider_dispatch(self):
        self.bootstrap()
        payload,quote=self.quoted()
        with sqlite3.connect(self.db) as conn:
            conn.execute('DELETE FROM points_policy')
            conn.execute('DELETE FROM points_cash_days')
        status,result,_=self.post('/api/agent/stream',{**payload,'quote_id':quote['quote_id']})
        self.assertEqual(status,503,result)
        self.assertEqual(result['code'],'budget_unavailable')
        self.assertEqual(self.server.agent.executions,0)
        self.assertEqual(self.client.request('/api/points/wallet')[1]['wallet']['held_micro'],0)

    def test_provider_failure_releases_user_credits_but_retains_actual_supplier_cost(self):
        self.bootstrap()
        self.server.agent.fail_after_bill=True
        payload,quote=self.quoted()
        status,events,_=self.post('/api/agent/stream',{**payload,'quote_id':quote['quote_id']})
        self.assertEqual(status,200,events)
        self.assertIn('error',[x['type'] for x in events])
        request_id=next(x['request_id'] for x in events if x['type']=='accepted')
        status,result,_=self.client.request('/api/points/requests/'+request_id)
        self.assertEqual(status,200,result)
        self.assertEqual(result['state'],'platform_absorbed')
        self.assertEqual(result['charged_micro'],0)
        self.assertEqual(result['wallet']['available'],'100.000000')
        self.assertNotIn('supplier_cost_cny',result)
        self.assertNotIn('platform_cost_cny',result)
        self.assertGreater(float(self.server.points.summary()['supplier_cost_cny']),0)
        self.assertIsNone(result.get('answer'))

    def test_temporary_stream_has_billing_but_no_transcript_or_recoverable_answer(self):
        actor=self.bootstrap()['actor']
        payload,quote=self.quoted(thread_id='temporary-topic',temporary=True,question='这段临时文字不应持久化')
        submitted={**payload,'quote_id':quote['quote_id']}
        status,events,_=self.post('/api/agent/stream',submitted)
        self.assertEqual(status,200,events)
        request_id=next(x['request_id'] for x in events if x['type']=='accepted')
        status,result,_=self.client.request('/api/points/requests/'+request_id)
        self.assertEqual(status,200,result)
        self.assertEqual(result['charged_points'],'0.000213')
        self.assertIsNone(result.get('answer'))
        self.assertEqual(self.client.request('/api/agent/sessions')[1]['total'],0)
        sid=self.server.issues.session_id(actor['storage_id'],'temporary-topic')
        self.assertFalse((self.server.agent.config.session_dir/(sid+'.json')).exists())
        with sqlite3.connect(self.db) as conn:
            for table in ['sessions','messages','llm_runs']:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM '+table+' WHERE session_id=?',(sid,)).fetchone()[0],0)
        status,replayed,_=self.post('/api/agent/stream',submitted)
        self.assertEqual(status,200,replayed)
        self.assertNotIn('content',[x['type'] for x in replayed])
        self.assertEqual(self.server.agent.executions,1)

    def test_pending_usage_deadline_releases_user_hold_without_provider_reexecution(self):
        self.bootstrap()
        self.server.agent.usage_complete=False
        payload,quote=self.quoted()
        status,events,_=self.post('/api/agent/stream',{**payload,'quote_id':quote['quote_id']})
        self.assertEqual(status,200,events)
        request_id=next(x['request_id'] for x in events if x['type']=='accepted')
        moment=self.server.points.clock()
        self.server.points.clock=lambda:moment+86401
        status,result,_=self.client.request('/api/points/requests/'+request_id)
        self.assertEqual(status,200,result)
        self.assertEqual(result['state'],'usage_released')
        self.assertEqual(result['wallet']['available'],'100.000000')
        self.assertEqual(result['wallet']['held_micro'],0)
        self.assertEqual(self.server.agent.executions,1)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM points_calls WHERE state='unknown'").fetchone()[0],1)

    def _paused_stream(self,payload,quote):
        self.server.agent.pause=True
        result={};ready=threading.Event()
        def headers(status,values):
            result['headers']=dict(values);result['status']=status;ready.set()
        def run():
            try:
                result['response']=self.post('/api/agent/stream',{**payload,'quote_id':quote['quote_id']},on_headers=headers)
            except Exception as exc:
                result['exception']=exc
        worker=threading.Thread(target=run,daemon=True);worker.start()
        self.addCleanup(self.server.agent.finish_gate.set)
        self.assertTrue(ready.wait(timeout=5),result)
        self.assertTrue(self.server.agent.started.wait(timeout=5),result)
        return result,worker

    def test_user_stop_charges_only_verified_incurred_usage(self):
        self.bootstrap()
        payload,quote=self.quoted(thread_id='stop-topic')
        running,worker=self._paused_stream(payload,quote)
        token=running['headers']['X-Stream-Token']
        status,result,_=self.post('/api/agent/session/stop',{'thread_id':'stop-topic','stream_token':token})
        self.assertEqual(status,200,result)
        self.assertTrue(result['stopped'])
        self.server.agent.finish_gate.set();worker.join(timeout=5)
        self.assertFalse(worker.is_alive(),running)
        self.assertNotIn('exception',running)
        events=running['response'][1]
        request_id=next(x['request_id'] for x in events if x['type']=='accepted')
        status,result,_=self.client.request('/api/points/requests/'+request_id)
        self.assertEqual(status,200,result)
        self.assertEqual(result['charged_points'],'0.000213')
        self.assertEqual(result['wallet']['held_micro'],0)
        self.assertEqual(self.server.agent.executions,1)

    def test_same_wallet_cannot_start_second_request_while_first_running(self):
        self.bootstrap()
        first,quote1=self.quoted(thread_id='first-topic')
        second,quote2=self.quoted(thread_id='second-topic')
        running,worker=self._paused_stream(first,quote1)
        status,result,_=self.post('/api/agent/stream',{**second,'quote_id':quote2['quote_id']})
        self.assertEqual(status,409,result)
        self.assertEqual(result['code'],'wallet_busy')
        self.assertEqual(self.server.agent.executions,1)
        self.server.agent.finish_gate.set();worker.join(timeout=5)
        self.assertFalse(worker.is_alive(),running)
        self.assertNotIn('exception',running)

    def test_disconnected_normal_stream_finishes_and_recovers_without_rebilling(self):
        self.bootstrap()
        payload,quote=self.quoted(thread_id='disconnected-topic')
        self.server.agent.pause=True
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_address[1],timeout=10)
        body=json.dumps({**payload,'quote_id':quote['quote_id']}).encode()
        conn.request('POST','/api/agent/stream',body,{'Content-Type':'application/json','Cookie':'; '.join(k+'='+v for k,v in self.client.cookies.items())})
        sock=conn.sock
        response=conn.getresponse()
        self.assertEqual(response.status,200)
        self.assertTrue(self.server.agent.started.wait(timeout=5))
        request_id=None
        while request_id is None:
            line=response.readline()
            self.assertTrue(line)
            if line.startswith(b'data: '):
                event=json.loads(line[6:])
                if event.get('type')=='accepted':request_id=event['request_id']
        sock.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack('ii',1,0))
        response.close();conn.close()
        self.server.agent.finish_gate.set()
        result={}
        for _ in range(100):
            status,result,_=self.client.request('/api/points/requests/'+request_id)
            self.assertEqual(status,200,result)
            if result['state'] not in {'reserved','running'}:break
            time.sleep(.01)
        self.assertEqual(result['state'],'settled',result)
        self.assertEqual(result['charged_points'],'0.000213')
        self.assertIn('隔离HTTP测试',result.get('answer',''))
        self.assertEqual(self.server.agent.executions,1)

    def test_failed_turn_never_recovers_a_legacy_previous_answer(self):
        actor=self.bootstrap()['actor']
        self.fixture_topic(actor,tid='legacy-topic')
        self.server.agent.fail_after_bill=True
        payload,quote=self.quoted(thread_id='legacy-topic')
        status,events,_=self.post('/api/agent/stream',{**payload,'quote_id':quote['quote_id']})
        self.assertEqual(status,200,events)
        request_id=next(x['request_id'] for x in events if x['type']=='accepted')
        status,result,_=self.client.request('/api/points/requests/'+request_id)
        self.assertEqual(status,200,result)
        self.assertEqual(result['state'],'platform_absorbed')
        self.assertIsNone(result.get('answer'),result)

    def test_deleted_transcript_does_not_delete_minimal_financial_request_details(self):
        self.bootstrap()
        payload,quote=self.quoted()
        status,events,_=self.post('/api/agent/stream',{**payload,'quote_id':quote['quote_id']})
        self.assertEqual(status,200,events)
        request_id=next(x['request_id'] for x in events if x['type']=='accepted')
        status,deleted,_=self.client.request('/api/agent/session?thread_id='+payload['thread_id'],'DELETE')
        self.assertEqual(status,200,deleted)
        status,result,_=self.client.request('/api/points/requests/'+request_id)
        self.assertEqual(status,200,result)
        self.assertEqual(result['state'],'settled')
        self.assertEqual(result['charged_points'],'0.000213')
        self.assertIsNone(result.get('answer'),result)

    def test_claimed_guest_answer_remains_recoverable_by_the_account(self):
        self.bootstrap()
        payload,quote=self.quoted(thread_id='claimed-topic')
        status,events,_=self.post('/api/agent/stream',{**payload,'quote_id':quote['quote_id']})
        self.assertEqual(status,200,events)
        request_id=next(x['request_id'] for x in events if x['type']=='accepted')
        self.authenticated()
        status,claimed,_=self.post('/api/account/guest-claim',{'claim_id':uuid.uuid4().hex})
        self.assertEqual(status,200,claimed)
        self.assertEqual(claimed['state'],'committed')
        status,result,_=self.client.request('/api/points/requests/'+request_id)
        self.assertEqual(status,200,result)
        self.assertEqual(result['charged_points'],'0.000213')
        self.assertIn('隔离HTTP测试',result.get('answer',''))
        self.assertEqual(self.server.agent.executions,1)

    def test_inflight_claim_finishes_old_worker_and_commits_account_delivery(self):
        self.bootstrap()
        payload,quote=self.quoted(thread_id='inflight-claim')
        running,worker=self._paused_stream(payload,quote)
        self.authenticated()
        status,claim,_=self.post('/api/account/guest-claim',{'claim_id':uuid.uuid4().hex})
        self.assertEqual(status,200,claim)
        self.assertEqual(claim['state'],'pending')
        self.server.agent.finish_gate.set();worker.join(timeout=5)
        self.assertFalse(worker.is_alive(),running)
        self.assertNotIn('exception',running)
        migrated={}
        for _ in range(100):
            status,migrated,_=self.client.request('/api/account/guest-claim/'+claim['job_id'])
            self.assertEqual(status,200,migrated)
            if migrated['state']=='committed':break
            time.sleep(.01)
        self.assertEqual(migrated['state'],'committed',migrated)
        self.assertFalse(self.server.issues.active_streams)
        events=running['response'][1]
        request_id=next(x['request_id'] for x in events if x['type']=='accepted')
        status,result,_=self.client.request('/api/points/requests/'+request_id)
        self.assertEqual(status,200,result)
        self.assertEqual(result['charged_points'],'0.000213')
        self.assertIn('隔离HTTP测试',result.get('answer',''))
        self.assertEqual(self.server.agent.executions,1)

    def test_transient_settlement_failure_never_leaks_worker_or_reexecutes_model(self):
        self.bootstrap()
        payload,quote=self.quoted(thread_id='settlement-retry')
        original=self.server.points.settle
        calls=[]
        def transient(*args,**kwargs):
            calls.append(args[0])
            if len(calls)==1:
                raise sqlite3.OperationalError('isolated transient settlement failure')
            return original(*args,**kwargs)
        with patch.object(self.server.points,'settle',side_effect=transient):
            status,events,_=self.post('/api/agent/stream',{**payload,'quote_id':quote['quote_id']})
        self.assertEqual(status,200,events)
        self.assertGreaterEqual(len(calls),2)
        self.assertFalse(self.server.issues.active_streams)
        request_id=next(x['request_id'] for x in events if x['type']=='accepted')
        status,result,_=self.client.request('/api/points/requests/'+request_id)
        self.assertEqual(status,200,result)
        self.assertEqual(result['state'],'settled')
        self.assertEqual(result['charged_points'],'0.000213')
        self.assertIn('隔离HTTP测试',result.get('answer',''))
        self.assertEqual(self.server.agent.executions,1)

    def test_durable_delivery_recovers_settlement_after_storage_outage(self):
        self.bootstrap()
        payload,quote=self.quoted(thread_id='delivery-recover')
        with patch.object(self.server.points,'settle',side_effect=sqlite3.OperationalError('isolated settlement storage outage')):
            status,events,_=self.post('/api/agent/stream',{**payload,'quote_id':quote['quote_id']})
        self.assertEqual(status,200,events)
        self.assertFalse(self.server.issues.active_streams)
        request_id=next(x['request_id'] for x in events if x['type']=='accepted')
        status,result,_=self.client.request('/api/points/requests/'+request_id)
        self.assertEqual(status,200,result)
        self.assertEqual(result['state'],'settled')
        self.assertEqual(result['charged_points'],'0.000213')
        self.assertIn('隔离HTTP测试',result.get('answer',''))
        status,replayed,_=self.post('/api/agent/stream',{**payload,'quote_id':quote['quote_id']})
        self.assertEqual(status,200,replayed)
        self.assertEqual(self.server.agent.executions,1)

    def wire_agent(self,**options):
        agent=WireHTTPAgent(self.server.agent.config,**options)
        self.server.agent=agent;self.server.migration.agent=agent
        return agent

    def test_actual_stream_client_bills_usage_tail_with_disjoint_cache_buckets(self):
        self.bootstrap()
        agent=self.wire_agent(cached=True)
        config=agent.config.llm
        self.server.points.publish_price({'provider':config.provider,'gateway':config.base_url,'model':config.model,
            'adapter':'deepseek','cache_read_supported':True,'cache_write_supported':False,
            'rates':{'input_uncached':'0.001','cache_read':'0.00025','cache_write':'0.001','billable_output':'0.002'}})
        payload,quote=self.quoted(thread_id='wire-cached')
        status,events,_=self.post('/api/agent/stream',{**payload,'quote_id':quote['quote_id']})
        self.assertEqual(status,200,events)
        request_id=next(x['request_id'] for x in events if x['type']=='accepted')
        status,result,_=self.client.request('/api/points/requests/'+request_id)
        self.assertEqual(status,200,result)
        self.assertEqual(result['charged_points'],'0.000196')
        self.assertEqual(result['calls'][0]['usage'],{'input_uncached':100,'cache_read':23,'cache_write':0,'billable_output':45})
        self.assertEqual(agent.executions,1)
        self.assertTrue(agent.payloads[0]['stream_options']['include_usage'])
        self.assertNotIn('isolated-test-placeholder',json.dumps(events)+json.dumps(result))

    def test_actual_nonstream_client_and_stream_client_share_the_same_ledger(self):
        self.bootstrap()
        agent=self.wire_agent()
        agent.config.llm.stream=False
        payload,quote=self.quoted(thread_id='wire-json')
        status,events,_=self.post('/api/agent/stream',{**payload,'quote_id':quote['quote_id']})
        self.assertEqual(status,200,events)
        request_id=next(x['request_id'] for x in events if x['type']=='accepted')
        status,result,_=self.client.request('/api/points/requests/'+request_id)
        self.assertEqual(status,200,result)
        self.assertEqual(result['charged_points'],'0.000213')
        self.assertEqual(agent.executions,1)
        self.assertFalse(agent.payloads[0]['stream'])

    def test_actual_stream_missing_tail_usage_is_pending_not_zero_cost(self):
        self.bootstrap();agent=self.wire_agent(missing_usage=True)
        payload,quote=self.quoted(thread_id='wire-pending')
        status,events,_=self.post('/api/agent/stream',{**payload,'quote_id':quote['quote_id']})
        self.assertEqual(status,200,events)
        self.assertIn('usage_pending',[x['type'] for x in events])
        request_id=next(x['request_id'] for x in events if x['type']=='accepted')
        status,result,_=self.client.request('/api/points/requests/'+request_id)
        self.assertEqual(status,200,result)
        self.assertEqual(result['state'],'usage_pending')
        self.assertGreater(result['wallet']['held_micro'],0)
        self.assertEqual(agent.executions,1)

    def test_actual_payload_above_quote_cap_never_reaches_provider(self):
        self.bootstrap();agent=self.wire_agent(extra_system='测'*5000)
        self.server.billing_input_cap=8192
        payload,quote=self.quoted(thread_id='wire-budget-cap')
        status,events,_=self.post('/api/agent/stream',{**payload,'quote_id':quote['quote_id']})
        self.assertEqual(status,200,events)
        self.assertIn('error',[x['type'] for x in events])
        request_id=next(x['request_id'] for x in events if x['type']=='accepted')
        status,result,_=self.client.request('/api/points/requests/'+request_id)
        self.assertEqual(status,200,result)
        self.assertEqual(result['charged_micro'],0)
        self.assertEqual(result['wallet']['available'],'100.000000')
        self.assertEqual(agent.executions,0)

    def test_client_operation_can_recover_when_accepted_response_was_lost(self):
        self.bootstrap()
        payload,quote=self.quoted(thread_id='lost-accepted')
        status,events,_=self.post('/api/agent/stream',{**payload,'quote_id':quote['quote_id']})
        self.assertEqual(status,200,events)
        # A browser already knows its operation ID before the server's first
        # event; losing accepted must not force it to create a new request.
        status,result,_=self.client.request('/api/points/requests/'+payload['client_request_id'])
        self.assertEqual(status,200,result)
        self.assertEqual(result['charged_points'],'0.000213')
        self.assertIn('隔离HTTP测试',result.get('answer',''))
        stranger=CookieClient(self.server);self.bootstrap(stranger)
        self.assertEqual(stranger.request('/api/points/requests/'+payload['client_request_id'])[0],404)

    def test_claimed_account_can_recover_original_guest_client_operation(self):
        self.bootstrap()
        payload,quote=self.quoted(thread_id='lost-accepted-claim')
        status,events,_=self.post('/api/agent/stream',{**payload,'quote_id':quote['quote_id']})
        self.assertEqual(status,200,events)
        self.authenticated()
        status,claimed,_=self.post('/api/account/guest-claim',{'claim_id':uuid.uuid4().hex})
        self.assertEqual(status,200,claimed)
        self.assertEqual(claimed['state'],'committed')
        status,result,_=self.client.request('/api/points/requests/'+payload['client_request_id'])
        self.assertEqual(status,200,result)
        self.assertEqual(result['charged_points'],'0.000213')
        self.assertIn('隔离HTTP测试',result.get('answer',''))
        self.assertEqual(self.server.agent.executions,1)

    def test_search_without_verified_cash_bound_is_rejected_before_provider(self):
        self.bootstrap()
        agent=SearchWireHTTPAgent(self.server.agent.config)
        self.server.agent=agent;self.server.migration.agent=agent
        payload,_=self.prove('/api/points/quote',{'question':'需要联网搜索','mode':'research','thread_id':'search-unconfigured',
            'search':True,'client_request_id':uuid.uuid4().hex})
        status,result,_=self.post('/api/points/quote',payload)
        self.assertEqual(status,422,result)
        self.assertEqual(result['code'],'search_budget_unconfigured')
        self.assertEqual(agent.executions,0)
        self.assertEqual(agent.searches,0)

    def test_actual_search_adapter_uses_platform_budget_without_charging_user_tokens(self):
        self.bootstrap()
        agent=SearchWireHTTPAgent(self.server.agent.config)
        self.server.agent=agent;self.server.migration.agent=agent
        self.server.search_cash_bound='0.005'
        payload,quote=self.quoted(thread_id='search-budget',search=True)
        status,events,_=self.post('/api/agent/stream',{**payload,'quote_id':quote['quote_id']})
        self.assertEqual(status,200,events)
        request_id=next(x['request_id'] for x in events if x['type']=='accepted')
        status,result,_=self.client.request('/api/points/requests/'+request_id)
        self.assertEqual(status,200,result)
        self.assertEqual(result['state'],'settled')
        self.assertEqual(result['charged_points'],'0.000213')
        self.assertEqual(agent.executions,1)
        self.assertEqual(agent.searches,1)
        search=next(x for x in result['calls'] if x['kind']=='firecrawl')
        self.assertFalse(search['billable'])
        self.assertEqual(search['state'],'unknown')
        self.assertEqual(self.server.points.summary()['cash_committed_cny'],'0.005000213')
        tool=next(x for x in events if x['type']=='tool')
        self.assertTrue(tool['result']['untrusted'])
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM web_sources').fetchone()[0],0)


if __name__=='__main__':unittest.main()
