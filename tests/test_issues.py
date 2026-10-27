"""Account-scoped issue state, explicit memory and real deletion semantics."""
import base64
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent.admin_config import AdminConfigStore
from agent.auth_server import MAX_HISTORY_CHARS, _issue_prompt_history, make_server
from agent.db import connect, ensure_schema
from agent.dialogue import understanding
from agent.firecrawl import FirecrawlConfig
from agent.issues import IssueError, IssueStore, public_metadata
from agent.llm import LLMConfig
from agent.runtime import Agent, AgentConfig


def analysis(goal='保住现有客户', **extra):
    data = {'phase': 'analyze', 'summary': '正在核对用户明确表达的目标', 'goal': goal,
            'known_facts': ['团队两人'], 'constraints': ['六周'], 'uncertainties': ['稳定性的具体含义未知'],
            'next_question': '', 'tentative': True}
    data.update(extra)
    return data


def action(title='核对一次实际故障'):
    return {'title': title, 'steps': ['向实际使用者核对一条记录'], 'continue_signal': '得到具体事件',
            'adjust_signal': '仍只有笼统意见', 'review_hint': '用户愿意时带反馈回来'}


class IssueStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='issue-store-')
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.db = root / 'agent.db'
        conn = connect(self.db); ensure_schema(conn); conn.close()
        config = AgentConfig(LLMConfig(api_key_env='UNUSED_ISSUE_TEST_KEY'), FirecrawlConfig(api_key_env='UNUSED_ISSUE_WEB_KEY'),
                             self.db, root/'corpus.db', root/'sessions')
        self.agent = Agent(config)
        self.store = IssueStore(self.db, config.session_dir)

    def save(self, user=1, tid='issue', *, a=None, goal='保住现有客户'):
        snapshot = self.store.begin_stream(user, tid, self.agent)
        current = a or analysis(goal)
        question = '用户的原始问题：' + goal
        messages = [{'role': 'user', 'content': question}, {'role': 'assistant', 'content': '按原话继续核对。【VE08】'}]
        meta = self.store.complete_stream(user, tid, self.agent, messages,
                {'mode': 'consult', 'as_of': None, 'analysis': current, 'used_evidence_ids': ['VE08'], 'tools': []}, question, lambda: None,
                stream_token=snapshot['stream_token'])
        self.store.end_stream(user, tid, snapshot['stream_token'])
        return meta

    def test_legacy_discovery_pagination_and_account_isolation(self):
        self.agent.save_session('u1_old', [{'role':'user','content':'旧目标'}], {'mode':'research','as_of':None})
        self.agent.save_session('u2_hidden', [{'role':'user','content':'其他用户的私事'}], {'mode':'consult','as_of':None})
        self.save(tid='newer')
        page = self.store.list(1, self.agent, limit=1)
        self.assertEqual(page['total'], 2)
        self.assertEqual(len(page['sessions']), 1)
        more = self.store.list(1, self.agent, limit=1, offset=1)
        self.assertEqual({x['thread_id'] for x in page['sessions'] + more['sessions']}, {'old','newer'})
        self.assertNotIn('hidden', json.dumps(page))
        for call in (lambda: self.store.delete(2, 'newer', self.agent),
                     lambda: self.store.update(2, 'newer', self.agent, {'title':'窃取'}),
                     lambda: self.store.export(2, 'newer', self.agent)):
            with self.assertRaises(IssueError) as error: call()
            self.assertEqual(error.exception.status, 404)
        for limit in (True, 0, 101, '10'):
            with self.assertRaises(IssueError): self.store.list(1, self.agent, limit=limit)

    def test_only_user_can_advance_action_and_new_generation_cannot_reset_it(self):
        suggested = {**action(), 'status':'tried', 'feedback':'模型虚构已经成功'}
        first = self.save(a=analysis(proposed_action=suggested))
        aid = first['actions'][0]['id']
        self.assertEqual(first['actions'][0]['status'], 'proposed')
        self.assertEqual(first['actions'][0]['feedback'], '')
        with self.assertRaises(IssueError):
            self.store.update(1, 'issue', self.agent, {'action_id':aid,'action_status':'reviewed'})
        accepted = self.store.update(1, 'issue', self.agent, {'action_id':aid,'action_status':'accepted'})
        self.assertEqual(accepted['actions'][0]['status'], 'accepted')
        another = self.save(a=analysis(proposed_action=action()))
        self.assertEqual(len(another['actions']), 1)
        self.assertEqual(another['actions'][0]['status'], 'accepted')
        tried = self.store.update(1, 'issue', self.agent, {'action_id':aid,'action_status':'tried','action_feedback':'问过了，目前没拿到具体记录'})
        self.assertEqual(tried['actions'][0]['feedback'], '问过了，目前没拿到具体记录')
        reviewed = self.store.update(1, 'issue', self.agent, {'action_id':aid,'action_status':'reviewed'})
        self.assertEqual(reviewed['actions'][0]['status'], 'reviewed')
        self.assertEqual(len(reviewed['understanding_history']), 1)

    def test_metadata_update_preserves_transcript_citations_and_validates_types(self):
        self.save()
        with sqlite3.connect(self.db) as conn:
            mid = conn.execute("SELECT message_id FROM messages WHERE session_id='u1_issue' AND role='assistant'").fetchone()[0]
            conn.execute("INSERT INTO citations(message_id,evidence_id) VALUES(?,?)", (mid,'VE08'))
        result = self.store.update(1, 'issue', self.agent, {'title':'客户稳定性','archived':True,'remember':True})
        self.assertEqual(result['title'], '客户稳定性')
        self.assertEqual(self.store.list(1, self.agent)['total'], 0)
        self.assertEqual(self.store.list(1, self.agent, include_archived=True)['total'], 1)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM citations').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 2)
        self.assertEqual(self.agent.session_metadata('u1_issue')['title'], '客户稳定性')
        for bad in ({'remember':'yes'}, {'archived':1}, {'title':''}, {'title':'x'*101}, {'config':{'api_key':'secret'}}):
            with self.assertRaises(IssueError): self.store.update(1, 'issue', self.agent, bad)

    def test_delete_clears_all_owned_content_and_late_stream_cannot_resurrect(self):
        self.save(a=analysis(proposed_action=action()))
        self.store.update(1, 'issue', self.agent, {'remember':True})
        with sqlite3.connect(self.db) as conn:
            mid = conn.execute("SELECT message_id FROM messages WHERE session_id='u1_issue' LIMIT 1").fetchone()[0]
            conn.execute('INSERT INTO citations(message_id,evidence_id) VALUES(?,?)', (mid,'VE08'))
            conn.execute("INSERT INTO llm_runs(session_id,status) VALUES('u1_issue','ok')")
        pending = self.store.begin_stream(1, 'issue', self.agent)
        self.store.delete(1, 'issue', self.agent)
        self.assertFalse((self.agent.config.session_dir/'u1_issue.json').exists())
        with self.assertRaises(IssueError) as late:
            self.store.complete_stream(1, 'issue', self.agent, [], {'mode':'consult'}, '晚到的私事', lambda: self.fail('must not audit deleted content'), stream_token=pending['stream_token'])
        self.assertTrue(late.exception.deleted)
        self.store.end_stream(1, 'issue', pending['stream_token'])
        with self.assertRaises(IssueError) as deleted: self.store.begin_stream(1, 'issue', self.agent)
        self.assertEqual(deleted.exception.status, 410)
        with sqlite3.connect(self.db) as conn:
            for table in ('sessions','messages','citations','llm_runs'):
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0], 0)
            tombstone = conn.execute('SELECT title,remember,state_json,deleted FROM issue_state').fetchone()
            self.assertEqual(tombstone, ('',0,'{}',1))
        self.assertEqual(self.store.list(1, self.agent)['total'], 0)

    def test_explicit_memory_permission_and_export_whitelist(self):
        self.save()
        self.assertEqual(self.store.memory(1, [], self.agent), [])
        with self.assertRaises(IssueError): self.store.memory(1, ['issue'], self.agent)
        self.store.update(1, 'issue', self.agent, {'remember':True})
        memory = self.store.memory(1, ['issue'], self.agent)
        self.assertEqual(memory[0]['thread_id'], 'issue')
        for bad in (['issue']*2, ['issue']*4, 'issue'):
            with self.assertRaises(IssueError): self.store.memory(1, bad, self.agent)
        with self.assertRaises(IssueError): self.store.memory(2, ['issue'], self.agent)
        payload = json.loads((self.agent.config.session_dir/'u1_issue.json').read_text())
        payload['metadata']['api_key'] = 'must-not-export'
        payload['metadata']['tools'] = [{'name':'local_search','result':{'authorization':'must-not-export','results':[{'evidence_id':'VE08','api_key':'must-not-export'}]}}]
        (self.agent.config.session_dir/'u1_issue.json').write_text(json.dumps(payload))
        exported = self.store.export(1, 'issue', self.agent)
        self.assertNotIn('must-not-export', json.dumps(exported))
        self.assertEqual(exported['tools'][0]['result']['results'], [{'evidence_id':'VE08'}])

    def test_user_update_while_generation_runs_survives_completion(self):
        self.save(a=analysis(proposed_action=action()))
        snapshot = self.store.begin_stream(1, 'issue', self.agent)
        aid = snapshot['metadata']['actions'][0]['id']
        self.store.update(1, 'issue', self.agent, {'title':'用户的新标题','remember':True,'action_id':aid,'action_status':'accepted'})
        finished = self.store.complete_stream(1, 'issue', self.agent, snapshot['messages'],
            {'mode':'consult','as_of':None,'analysis':analysis(goal='新的阶段目标', proposed_action=action())}, '本轮更正', lambda:None,
            stream_token=snapshot['stream_token'])
        self.store.end_stream(1, 'issue', snapshot['stream_token'])
        self.assertEqual(finished['title'], '用户的新标题')
        self.assertTrue(finished['remember'])
        self.assertEqual(finished['actions'][0]['status'], 'accepted')
        self.assertEqual(len(finished['understanding_history']), 2)

    def test_same_issue_rejects_concurrent_generation_and_bounds_snapshots(self):
        pending = self.store.begin_stream(1, 'issue', self.agent)
        with self.assertRaises(IssueError) as busy: self.store.begin_stream(1, 'issue', self.agent)
        self.assertEqual(busy.exception.status, 409)
        self.store.end_stream(1, 'issue', pending['stream_token'])
        for index in range(16): self.save(a=analysis(goal=str(index), proposed_action=action(str(index))))
        snapshot = self.store.load(1, 'issue', self.agent)
        self.assertLessEqual(len(snapshot['metadata']['understanding_history']), 10)
        self.assertLessEqual(len(snapshot['metadata']['actions']), 12)

    def test_cancelled_generation_cannot_save_or_release_a_new_stream_token(self):
        old = self.store.begin_stream(1, 'issue', self.agent)
        self.assertTrue(self.store.stop_stream(1, 'issue', old['stream_token']))
        new = self.store.begin_stream(1, 'issue', self.agent)
        self.assertFalse(self.store.stop_stream(1, 'issue', old['stream_token']))
        with self.assertRaises(IssueError) as cancelled:
            self.store.complete_stream(1, 'issue', self.agent, [], {'mode':'consult'}, '旧消息',
                                      lambda:self.fail('cancelled draft cannot be audited'), stream_token=old['stream_token'])
        self.assertTrue(cancelled.exception.cancelled)
        self.store.end_stream(1, 'issue', old['stream_token'])
        self.store.assert_active(1, 'issue', new['stream_token'])
        self.store.complete_stream(1, 'issue', self.agent, [{'role':'user','content':'新问题'}],
                                   {'mode':'consult'}, '新问题', lambda:None, stream_token=new['stream_token'])
        self.store.end_stream(1, 'issue', new['stream_token'])
        self.assertEqual(self.agent.load_session('u1_issue')[0]['content'], '新问题')


class IssuePromptTests(unittest.TestCase):
    def test_combined_history_understanding_actions_and_three_memories_share_one_budget(self):
        a = understanding(phase='analyze', summary='摘'*600, goal='标'*240,
            known_facts=['实'*240]*6, constraints=['限'*240]*6, uncertainties=['疑'*240]*6)
        actions = [{**action('事'*140), 'id':str(i),'status':'tried','feedback':'回'*2000,'steps':['步'*300]*5} for i in range(12)]
        histories = [{'role':'user' if i%2==0 else 'assistant','content':'长'*12000} for i in range(40)]
        memories = [{'thread_id':'memory'+str(i),'title':'题'*100,'analysis':a,'actions':actions} for i in range(3)]
        result = _issue_prompt_history(histories, 'consult', {'analysis':a,'actions':actions}, memories)
        self.assertLessEqual(sum(len(x['content']) for x in result), MAX_HISTORY_CHARS)
        self.assertEqual(result[-1]['role'], 'assistant')
        self.assertIn('不是本轮用户新事实', result[-1]['content'])
        for mode in ('research','historical','modern'):
            self.assertEqual(_issue_prompt_history(histories, mode, {'analysis':a,'actions':actions}, memories), histories)


class IssueHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='issue-http-')
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name)/'agent.db'
        env = patch.dict(os.environ, {'MAO_AGENT_DB':str(self.db),'MAO_ADMIN_SECRET_FILE':str(self.db)+'.provider-secrets'})
        env.start();self.addCleanup(env.stop)
        for name,value in (('_keychain_get',''),('_keychain_set',False),('_keychain_delete',None)):
            item=patch.object(AdminConfigStore,name,return_value=value);item.start();self.addCleanup(item.stop)
        self.server=make_server('127.0.0.1',0,self.db)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.stop)
        self.base='http://127.0.0.1:%d'%self.server.server_address[1]
        self.server.auth_service.validate_session=lambda token: {'ok':True,'user':{'user_id':1 if token=='one' else 2,'username':'reader'}} if token in {'one','two'} else {'ok':False}

    def stop(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=3)

    def req(self,path,data=None,method=None,user='one',origin=None,response_hook=None):
        headers={'Content-Type':'application/json'}
        if user:headers['Cookie']='mao_session='+user
        if origin:headers['Origin']=origin
        request=Request(self.base+path,data=json.dumps(data).encode() if data is not None else None,
                        method=method or ('POST' if data is not None else 'GET'),headers=headers)
        try:
            with urlopen(request,timeout=5) as r:
                if response_hook: response_hook(dict(r.headers))
                return r.status,dict(r.headers),r.read().decode()
        except HTTPError as e:return e.code,dict(e.headers),e.read().decode()

    def stream(self,question='请帮我梳理',tid='issue',response_hook=None,**extra):
        return self.req('/api/agent/stream',{'question':question,'thread_id':tid,**extra},response_hook=response_hook)

    def fake(self,question,mode,as_of,history=None,**kwargs):
        yield {'type':'analysis','analysis':analysis(proposed_action=action())}
        yield {'type':'content','text':'先核对一个具体事件，是否尝试由你决定。【VE08】'}
        yield {'type':'done','steps':1}

    def test_sessions_actions_export_origin_and_delete(self):
        with patch.object(self.server.agent,'stream',side_effect=self.fake):
            status,headers,body=self.stream()
        self.assertEqual(status,200)
        self.assertRegex(headers['X-Stream-Token'], r'^[a-f0-9]{32}$')
        self.assertLess(body.index('"type":"session_meta"'),body.index('"type":"done"'))
        _,_,body=self.req('/api/agent/sessions'); sessions=json.loads(body)['sessions']
        self.assertEqual(len(sessions),1)
        self.assertEqual(sessions[0]['thread_id'],'issue')
        aid=sessions[0]['actions'][0]['id']
        status,_,body=self.req('/api/agent/session/update',{'thread_id':'issue','title':'客户留存','remember':True,'action_id':aid,'action_status':'accepted'})
        self.assertEqual(status,200);self.assertEqual(json.loads(body)['actions'][0]['status'],'accepted')
        status,headers,body=self.req('/api/agent/session/export?thread_id=issue')
        self.assertEqual(status,200);self.assertIn('attachment',headers['Content-Disposition'])
        self.assertEqual(json.loads(body)['metadata']['title'],'客户留存')
        for path in ('/api/agent/sessions','/api/agent/session/export?thread_id=issue','/api/agent/evidence?ids=VE08'):
            self.assertEqual(self.req(path,user=None)[0],401)
        self.assertEqual(self.req('/api/agent/session/update',{'thread_id':'issue','title':'偷看'},user='two')[0],404)
        self.assertEqual(self.req('/api/agent/session?thread_id=issue',method='DELETE',origin='https://evil.example')[0],403)
        self.assertEqual(self.req('/api/agent/session?thread_id=issue',method='DELETE',user='two')[0],404)
        self.assertEqual(self.req('/api/agent/sessions?limit=100000')[0],400)
        self.assertEqual(self.req('/api/agent/session?thread_id=issue',method='DELETE')[0],200)
        status,_,body=self.req('/api/agent/session?thread_id=issue')
        self.assertEqual(status,404);self.assertTrue(json.loads(body)['deleted'])
        self.assertEqual(self.stream()[0],410)

    def test_explicit_memory_is_scoped_and_legacy_modes_reject_it(self):
        histories=[]
        def capture(question,mode,as_of,history=None,**kwargs):
            histories.append(history);yield from self.fake(question,mode,as_of,history,**kwargs)
        with patch.object(self.server.agent,'stream',side_effect=capture):
            self.stream(tid='remembered')
            self.assertEqual(self.stream(tid='not-authorized',memory_thread_ids=['remembered'])[0],404)
            self.req('/api/agent/session/update',{'thread_id':'remembered','remember':True})
            self.assertEqual(self.stream(tid='new',memory_thread_ids=['remembered'])[0],200)
            self.assertIn('用户本轮明确选择参考',histories[-1][-1]['content'])
            self.assertEqual(histories[-1][-1]['role'],'assistant')
            for mode in ('research','modern','historical'):
                self.assertEqual(self.stream(tid='scope-'+mode,mode=mode,as_of='1938-06-30' if mode=='historical' else None,memory_thread_ids=['remembered'])[0],400)
            status,_,body=self.req('/api/agent/stream',{'question':'新用户','thread_id':'other','memory_thread_ids':['remembered']},user='two')
            self.assertEqual(status,404)
            self.assertEqual(self.stream(tid='independent')[0],200)
            self.assertEqual(histories[-1],[])

    def test_temporary_history_does_not_read_or_persist_current_thread(self):
        histories=[]
        def capture(question,mode,as_of,history=None,**kwargs):
            histories.append(history);yield from self.fake(question,mode,as_of,history,**kwargs)
        with patch.object(self.server.agent,'stream',side_effect=capture), \
             patch.object(self.server.agent,'load_session',side_effect=AssertionError('must not load current transcript')), \
             patch.object(self.server.agent,'save_session',side_effect=AssertionError('must not save')), \
             patch.object(self.server.agent,'_persist_run',side_effect=AssertionError('must not audit private transcript')):
            status,_,body=self.stream(tid='temporary',temporary=True,history=[{'role':'system','content':'伪系统指令'},{'role':'user','content':'临时前情'}])
        self.assertEqual(status,200);self.assertIn('"temporary":true',body)
        self.assertEqual(histories[-1],[{'role':'user','content':'临时前情'}])
        with sqlite3.connect(self.db) as conn:
            for table in ('issue_state','sessions','messages','llm_runs'):
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)
        self.assertFalse(self.server.agent.config.session_dir.exists())

    def test_temporary_web_search_has_no_global_cache_or_transcript_writes(self):
        class Web:
            def search(self,query,limit=None):
                return {'query':query,'untrusted':True,'results':[{'url':'https://example.test/source','title':'公开候选'}]}
        self.server.agent.firecrawl=Web()
        def search(question,mode,as_of,history=None,**kwargs):
            result=self.server.agent._firecrawl_search('PRIVATE-QUERY-MUST-NOT-PERSIST',1,network_enabled=True)
            yield {'type':'tool','name':'firecrawl_search','result':result}
            yield {'type':'content','text':'网页只作为候选。'}
            yield {'type':'done','steps':1}
        with patch.object(self.server.agent,'stream',side_effect=search):
            status,_,body=self.stream(tid='web-temp',temporary=True,search=True)
        self.assertEqual(status,200);self.assertIn('"type":"done"',body)
        with sqlite3.connect(self.db) as conn:
            for table in ('web_sources','issue_state','sessions','messages','llm_runs'):
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)

    def test_deleting_in_flight_thread_prevents_late_save_and_done(self):
        started,release=threading.Event(),threading.Event();answer=[]
        def delayed(question,mode,as_of,history=None,**kwargs):
            started.set();release.wait(timeout=4)
            yield {'type':'content','text':'不能保存的晚到答复'}
            yield {'type':'done','steps':1}
        with patch.object(self.server.agent,'stream',side_effect=delayed):
            worker=threading.Thread(target=lambda:answer.append(self.stream(tid='pending')));worker.start()
            self.assertTrue(started.wait(timeout=2))
            self.assertEqual(self.req('/api/agent/session?thread_id=pending',method='DELETE')[0],200)
            release.set();worker.join(timeout=5)
        self.assertTrue(answer)
        self.assertIn('"deleted":true',answer[0][2]);self.assertNotIn('"type":"done"',answer[0][2])
        self.assertNotIn('不能保存的晚到答复',answer[0][2])
        self.assertFalse((self.server.agent.config.session_dir/'u1_pending.json').exists())

    def test_evidence_lookup_is_bounded_public_and_authenticated(self):
        conn=connect(self.db)
        conn.execute("INSERT INTO evidence_cards(evidence_id,summary,reviewed,quote_verified) VALUES('VE08','已核摘要',1,0)")
        conn.commit();conn.close()
        status,_,body=self.req('/api/agent/evidence?ids=VE08,FAKE01')
        self.assertEqual(status,200)
        data=json.loads(body);self.assertEqual(data['results'][0]['evidence_id'],'VE08')
        self.assertIn('不构成历史截止日期',data['notice'])
        self.assertEqual(self.req('/api/agent/evidence?ids='+','.join('ID'+str(x) for x in range(7)))[0],400)
        self.assertEqual(self.req('/api/agent/evidence?ids=VE08,VE08')[0],400)

    def test_stop_is_authenticated_scoped_and_allows_immediate_retry(self):
        started,release=threading.Event(),threading.Event();old_result=[]
        new_started,new_release=threading.Event(),threading.Event();new_result=[]
        old_headers,new_headers={},{}
        old_ready,new_ready=threading.Event(),threading.Event()
        def old_response(headers): old_headers.update(headers);old_ready.set()
        def new_response(headers): new_headers.update(headers);new_ready.set()
        def generation(question,mode,as_of,history=None,**kwargs):
            if question == '旧请求':
                started.set();release.wait(timeout=4)
                yield {'type':'content','text':'旧草稿不得发布或保存'}
                yield {'type':'done','steps':1}
            else:
                new_started.set();new_release.wait(timeout=4)
                yield {'type':'content','text':'新请求完成'}
                yield {'type':'done','steps':1}
        with patch.object(self.server.agent,'stream',side_effect=generation):
            worker=threading.Thread(target=lambda:old_result.append(self.stream(question='旧请求',tid='stop',response_hook=old_response)));worker.start()
            self.assertTrue(started.wait(timeout=2))
            self.assertTrue(old_ready.wait(timeout=2))
            token=old_headers['X-Stream-Token']
            self.assertEqual(self.req('/api/agent/session/stop',{'thread_id':'stop','stream_token':token},user=None)[0],401)
            self.assertEqual(self.req('/api/agent/session/stop',{'thread_id':'stop'})[0],400)
            status,_,body=self.req('/api/agent/session/stop',{'thread_id':'stop','stream_token':token},user='two')
            self.assertEqual(status,200);self.assertFalse(json.loads(body)['stopped'])
            status,_,body=self.req('/api/agent/session/stop',{'thread_id':'stop','stream_token':token})
            self.assertEqual(status,200);self.assertTrue(json.loads(body)['stopped'])
            new_worker=threading.Thread(target=lambda:new_result.append(self.stream(question='新请求',tid='stop',response_hook=new_response)));new_worker.start()
            self.assertTrue(new_started.wait(timeout=2))
            self.assertTrue(new_ready.wait(timeout=2))
            self.assertNotEqual(token,new_headers['X-Stream-Token'])
            status,_,body=self.req('/api/agent/session/stop',{'thread_id':'stop','stream_token':token})
            self.assertEqual(status,200);self.assertFalse(json.loads(body)['stopped'])
            new_release.set();new_worker.join(timeout=5)
            self.assertEqual(new_result[0][0],200);self.assertIn('"type":"done"',new_result[0][2])
            release.set();worker.join(timeout=5)
        self.assertIn('"cancelled":true',old_result[0][2])
        self.assertNotIn('旧草稿不得发布或保存',old_result[0][2])
        saved=self.server.agent.load_session('u1_stop')
        self.assertEqual([x['content'] for x in saved],['新请求','新请求完成'])

    def test_temporary_stop_uses_no_persistent_issue_or_transcript(self):
        started,release=threading.Event(),threading.Event();result=[];headers={};ready=threading.Event()
        def response(data): headers.update(data);ready.set()
        def delayed(question,mode,as_of,history=None,**kwargs):
            started.set();release.wait(timeout=4)
            yield {'type':'content','text':'未完成的临时答复'}
            yield {'type':'done','steps':1}
        with patch.object(self.server.agent,'stream',side_effect=delayed):
            worker=threading.Thread(target=lambda:result.append(self.stream(tid='temp-stop',temporary=True,response_hook=response)));worker.start()
            self.assertTrue(started.wait(timeout=2))
            self.assertTrue(ready.wait(timeout=2))
            self.assertEqual(self.req('/api/agent/session/stop',{'thread_id':'temp-stop','stream_token':headers['X-Stream-Token']})[0],200)
            release.set();worker.join(timeout=5)
        self.assertIn('"cancelled":true',result[0][2])
        with sqlite3.connect(self.db) as conn:
            for table in ('issue_state','sessions','messages','llm_runs'):
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)


if __name__=='__main__':unittest.main()
