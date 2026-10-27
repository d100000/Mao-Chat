"""Run the actual workbench rendering helpers against hostile and failed data."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('node'), 'Node.js unavailable')
class WorkbenchBehaviorTests(unittest.TestCase):
    def test_rendering_and_evidence_status_fail_closed(self):
        source = (ROOT / 'web/index.html').read_text(encoding='utf-8')
        esc = re.search(r'const esc=.*?;\n', source).group()
        formatter = source[source.index('function formatAnswer'):source.index('function addMessage')]
        status = source[source.index('function evidenceStatus'):source.index('function renderEvidence')]
        code = esc + formatter + status + """
const assert=require('node:assert/strict');
assert.equal(evidenceStatus({}), '候选');
assert.equal(evidenceStatus({reviewed:1}), '已审核');
assert.equal(evidenceStatus({verification_status:'reviewed',untrusted:true}), '候选');
assert.equal(evidenceStatus({reviewed:1,status:'candidate_page_match'}), '候选');
assert.equal(evidenceStatus({record_type:'method_note'}), '方法概括');
assert.ok(!formatAnswer('<img src=x onerror=alert(1)>').includes('<img'));
assert.ok(formatAnswer('**原文**').includes('<strong>原文</strong>'));
"""
        result = subprocess.run(['node', '-e', code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_public_understanding_updates_and_remains_correctable(self):
        source = (ROOT / 'web/index.html').read_text(encoding='utf-8')
        esc = re.search(r'const esc=.*?;\n', source).group()
        render = source[source.index('function renderUnderstanding'):source.index('function renderTab')]
        harness = r'''
const assert=require('node:assert/strict');
class Element {
 constructor(){this.children=[];this.textContent='';this.html='';}
 append(x){this.children.push(x);}
 set innerHTML(value){this.html=value;this.children=[];if(value.includes('id="analysisBody"'))elements.analysisBody=new Element();}
 get innerHTML(){return this.html;}
 get text(){return this.textContent+this.html+this.children.map(x=>x.text).join('');}
}
const elements={evidenceBody:new Element(),evidenceCount:new Element()};
const $=s=>elements[s.slice(1)],document={createElement:()=>new Element()};
const labels={consult:['对话解惑']};let corrected=false;
const closeDrawers=()=>{},setComposer=()=>{corrected=true},setTab=()=>{};
const state={mode:'consult',sources:[],analysis:{phase:'analyze',summary:'两人团队，六周内解决客户问题',goal:'获取新客户',known_facts:['已访谈两人'],constraints:['不能招人'],uncertainties:['付费意愿待确认'],next_question:'现有客户最看重什么？'}};
RENDER
renderUnderstanding();assert.match(elements.analysisBody.text,/获取新客户/);assert.match(elements.analysisBody.text,/待你确认/);
state.analysis={...state.analysis,phase:'review',goal:'保住现有客户',known_facts:['现有三家客户更看重稳定性']};
renderUnderstanding();assert.match(elements.analysisBody.text,/保住现有客户/);assert.doesNotMatch(elements.analysisBody.text,/获取新客户/);
elements.analysisBody.children.find(x=>x.className==='understanding-correct').onclick();assert.equal(corrected,true);
state.analysis=null;renderUnderstanding();assert.match(elements.analysisBody.text,/先谈事情/);
'''.replace('RENDER', esc + render)
        result = subprocess.run(['node', '-e', harness], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_provider_token_gives_actionable_error(self):
        source = (ROOT / 'web/index.html').read_text(encoding='utf-8')
        helper = source[source.index('function friendlyError'):source.index('const safeURL')]
        code = helper + """
const assert=require('node:assert/strict');
assert.match(friendlyError('LLM HTTP 401: Invalid token'), /管理员后台更新模型密钥/);
assert.match(friendlyError('Failed to fetch'), /本地服务/);
assert.equal(friendlyError('自定义错误'), '自定义错误');
"""
        result = subprocess.run(['node', '-e', code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_temporary_history_respects_utf8_request_budget_and_keeps_public_context(self):
        source = (ROOT / 'web/index.html').read_text(encoding='utf-8')
        helper = source[source.index('function boundedTemporaryHistory'):source.index('async function send')]
        code = helper + r'''
const assert=require('node:assert/strict');
const history=[];for(let i=0;i<25;i++)history.push({role:'user',content:'问题'+i+'中'.repeat(4000)},{role:'assistant',content:'回答'+i+'文'.repeat(5000)});
const original=JSON.stringify(history),payload={question:'现'.repeat(4000),temporary:true,memory_thread_ids:['remembered-issue']};
const result=boundedTemporaryHistory(history,payload,{summary:'当前仍以客户留存为目标',stage_goal:'留住三家客户',known_facts:['两人团队'],uncertainties:['稳定性具体含义未明']});
assert.ok(new TextEncoder().encode(JSON.stringify({...payload,history:result})).length<=60000);
assert.equal(JSON.stringify(history),original);assert.ok(result.some(x=>x.content.startsWith('问题24')));
assert.equal(result.at(-1).role,'assistant');assert.match(result.at(-1).content,/不是用户新陈述/);assert.match(result.at(-1).content,/留住三家客户/);
assert.ok(!result.some(x=>x.content.startsWith('问题0')));
const short=boundedTemporaryHistory([{role:'system',content:'bad'},{role:'user',content:'hello'},{role:'assistant',content:'hi'}],payload,null);
assert.deepEqual(short.map(x=>x.role),['user','assistant']);
'''
        result = subprocess.run(['node', '-e', code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_used_sources_are_exact_and_do_not_promote_linked_event_cards(self):
        source = (ROOT / 'web/index.html').read_text(encoding='utf-8')
        helpers = source[source.index('function sourceKeys'):source.index('function hydrateCitations')]
        code = helpers + r'''
const assert=require('node:assert/strict');
const state={sources:[{row:{record_type:'evidence_card',evidence_id:'VE08'}},{row:{record_type:'historical_event',event_id:'event:TL01',evidence_id:'NOT_RETURNED_AUTHOR'}},{row:{record_type:'principle_note',principle_id:'principle:care',evidence_ids:['VE11']}}]};
assert.deepEqual(citedIds('依据【VE08：原著位置】，未知【FAKE01】'),['VE08']);
assert.deepEqual(citedIds('【NOT_RETURNED_AUTHOR】【VE11】'),[]);
assert.deepEqual(citedIds('【event:TL01：公开事实】【principle:care｜思想参考】'),['event:TL01','principle:care']);
'''
        result = subprocess.run(['node', '-e', code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
