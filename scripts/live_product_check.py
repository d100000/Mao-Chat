#!/usr/bin/env python3
"""Explicit, real-provider product acceptance in an isolated account/database.

Use --run to spend provider credits. Normal unit/CI tests never invoke this.
"""
from __future__ import annotations
import argparse
import base64
import json
from pathlib import Path
import re
import secrets
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.live_test_server import make_live_server, safe_error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--output', type=Path, default=ROOT/'tmp/live-product-check/report.json')
    args = parser.parse_args(argv)
    if not args.run:
        print('此验收会调用真实模型；明确运行时加 --run。')
        return 0
    report = {'provider':'live-openai-compatible','running':True,'checks':[],'answers':[]}
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        data = json.dumps(report, ensure_ascii=False, indent=2)
        data = re.sub(r'\b(?:sk|fc)-[A-Za-z0-9_-]+', '[redacted]', data)
        args.output.write_text(data)
        args.output.chmod(0o600)

    def step(name, fn):
        started = time.monotonic()
        try:
            result = fn()
            report['checks'].append({'name':name,'status':'passed','elapsed_s':round(time.monotonic()-started,2)})
            print('PASS '+name, flush=True)
            return result
        except Exception as exc:
            report['checks'].append({'name':name,'status':'failed','error':safe_error(exc),'elapsed_s':round(time.monotonic()-started,2)})
            raise
        finally:
            save()

    server = None
    thread = None
    failure = None
    with tempfile.TemporaryDirectory(prefix='jiaoyuan-live-product-') as state:
        try:
            server, state_path = make_live_server(state_dir=state)
            report['model'] = server.agent.config.llm.model
            thread = threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            base = 'http://127.0.0.1:%d' % server.server_address[1]
            service = server.auth_service

            def captcha():
                data=service.create_math_challenge()
                svg=base64.b64decode(data['image'].split(',')[1]).decode()
                a, op, b=re.search(r'>(\d+) ([+−\-×]) (\d+) = \?</text>',svg).groups()
                a,b=int(a),int(b)
                return {'math_challenge_id':data['challenge_id'],'math_answer':str(a+b if op=='+' else a*b if op=='×' else a-b)}

            user='product_'+secrets.token_hex(5); password=secrets.token_urlsafe(20)
            service.register(user,password,**captcha())
            login=service.login(user,password,**captcha())
            cookie='mao_session='+login['session_token']

            def request(path, data=None, method=None):
                body=json.dumps(data,ensure_ascii=False).encode() if data is not None else None
                req=Request(base+path,data=body,method=method,headers={'Cookie':cookie,'Content-Type':'application/json'})
                try:
                    with urlopen(req,timeout=240) as response:
                        return response.status,response.read().decode()
                except HTTPError as exc:
                    return exc.code,exc.read().decode()

            def data(path, payload=None, method=None):
                status,body=request(path,payload,method)
                assert status==200,(status,safe_error(body))
                return json.loads(body)

            def ask(label,question,**extra):
                payload={'question':question,'mode':'consult','thread_id':'live-product',**extra}
                status,body=request('/api/agent/stream',payload)
                assert status==200,(status,safe_error(body))
                events=[json.loads(line[5:].strip()) for line in body.splitlines() if line.startswith('data:')]
                errors=[e.get('error') for e in events if e.get('type')=='error']
                assert not errors,safe_error(errors)
                assert any(e.get('type')=='done' for e in events)
                analysis=next((e['analysis'] for e in reversed(events) if e.get('type')=='analysis'),None)
                assert isinstance(analysis,dict)
                answer=''.join(e.get('text','') for e in events if e.get('type')=='content')
                assert answer.strip()
                record={'label':label,'question':question,'analysis':analysis,'answer':answer,
                        'tool_names':[e.get('name') for e in events if e.get('type')=='tool'],
                        'used_evidence_ids':next((e.get('used_evidence_ids',[]) for e in events if e.get('type')=='answer_meta'),[])}
                report['answers'].append(record)
                save()
                assert '证据门提示：回答含未在本轮工具结果中返回' not in answer
                return record

            def listen():
                r=ask('listen','我带一个两人的软件团队，最近压力很大。这一轮我只想说说，先不要安排任务或给行动建议。')
                assert r['analysis'].get('intent')=='listen' or r['analysis'].get('phase')=='listen'
                assert not r['analysis'].get('proposed_action')
            step('real listening respects a request not to assign an action',listen)

            def plan():
                r=ask('plan','现在可以一起想办法。我的长期方向是让团队成员不被长期透支；当前阶段目标是六周内留住现有三家客户，人手仍是两人，没有额外预算。客户说更在意稳定性，但我还不知道具体指什么。请结合对人的代价和实际调查的思考，给一个本周可撤回的小尝试，用不超过350字说清判断和信号。')
                a=r['analysis']
                assert re.search('客户|留存|留住|留下',a.get('stage_goal',''))
                assert a.get('turn_request')
                assert a.get('long_term_direction')
                assert a.get('proposed_action')
                assert 'method_search' in r['tool_names']
                assert re.search('代价|透支|健康|负担|参与|意愿',r['answer'])
                assert len(r['answer']) <= 350, '明确的350字上限没有得到遵守'
                assert r['used_evidence_ids'], '建议应使用可追溯的独立来源标记'
                snapshot=data('/api/agent/session?thread_id=live-product')
                actions=snapshot.get('actions') or snapshot.get('metadata',{}).get('actions',[])
                assert actions
                assert all(x['status']=='proposed' for x in actions)
                return actions[-1]['id']
            action_id=step('real advice separates goals and creates only a proposal',plan)
            step('user explicitly accepts the action',lambda:data('/api/agent/session/update',{'thread_id':'live-product','action_id':action_id,'action_status':'accepted'}))

            def compact():
                r=ask('compact','只把刚才的做法压成三句话、合计不超过200字，依据用简短标记放在句内，不加其他段落。阶段目标和长期方向都不变。这次只是换表达方式，不代表我已经做了。')
                assert re.search('客户|留存|留住|留下',r['analysis'].get('stage_goal',''))
                assert len(r['answer']) <= 200, '压缩诉求没有得到遵守'
                snapshot=data('/api/agent/session?thread_id=live-product')
                actions=snapshot.get('actions') or snapshot.get('metadata',{}).get('actions',[])
                assert next(x for x in actions if x['id']==action_id)['status']=='accepted'
            step('shortening a reply preserves the stage goal and user action state',compact)

            step('user authorizes the issue as a memory',lambda:data('/api/agent/session/update',{'thread_id':'live-product','remember':True,'title':'稳定性与团队负担'}))

            def temporary():
                r=ask('temporary-memory','请以我明确选择的旧议题为背景。这轮只想说说，不需要任务；我有点担心团队负担，请先听我讲。',thread_id='live-temporary',temporary=True,history=[],memory_thread_ids=['live-product'])
                assert not r['analysis'].get('proposed_action')
                snapshot=data('/api/agent/session?thread_id=live-temporary')
                assert not snapshot.get('messages')
                me=data('/api/auth/me')['user']['user_id']
                assert not (Path(state_path)/'sessions'/('u%s_live-temporary.json'%me)).exists()
            step('authorized memory supports a temporary real conversation without a saved transcript',temporary)

            def lifecycle():
                data('/api/agent/session/update',{'thread_id':'live-product','action_id':action_id,'action_status':'tried','action_feedback':'已记录反馈，尚未承诺交付。'})
                exported=data('/api/agent/session/export?thread_id=live-product')
                assert exported.get('messages')
                assert not any(k in json.dumps(exported).lower() for k in ['api_key_value','session_token','provider-secrets'])
                status,_=request('/api/agent/session?thread_id=live-product',method='DELETE')
                assert status==200
                status,body=request('/api/agent/stream',{'question':'删除后不应重新生成','thread_id':'live-product','mode':'consult'})
                assert status==410,(status,safe_error(body))
            step('action feedback export and deletion remain controlled by the user',lifecycle)
        except Exception as exc:
            failure=exc
            report['error']=safe_error(exc)
        finally:
            if server is not None:
                server.shutdown();server.server_close()
                if thread is not None: thread.join(timeout=3)
            report['running']=False
            report['passed']=sum(x['status']=='passed' for x in report['checks'])
            report['failed']=sum(x['status']=='failed' for x in report['checks'])
            save()
    if failure:
        print(safe_error(failure),file=sys.stderr)
        return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
