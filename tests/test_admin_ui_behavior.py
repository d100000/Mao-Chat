"""Exercise admin browser logic with a small DOM and real async event handlers."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node.js is unavailable")
class AdminUIBehaviorTests(unittest.TestCase):
    def test_loading_validation_save_and_account_controls(self):
        html = (ROOT / "web/admin.html").read_text(encoding="utf-8")
        source = re.search(r"<script>([\s\S]*)</script>", html).group(1)
        harness = r'''
const vm=require('node:vm'), assert=require('node:assert/strict');
const source=SOURCE;
const config={llm:{base_url:'https://example.com/v1',model:'<img src=x onerror=alert(1)>',api_key_env:'TEST_KEY',reasoning_effort:'high',temperature:0.2,max_tokens:2000,timeout_s:20,reasoning:true,stream:true,api_key_set:true},firecrawl:{base_url:'https://example.com/v2',api_key_env:'TEST_FIRE_KEY',limit:5,configured:false},agent:{max_steps:6,local_limit:8,web_limit:5}};
function makeEnvironment({file=false,failed=false}={}){
 const elements={},requests=[],handlers={},classes=()=>{const set=new Set();return{add:x=>set.add(x),remove:x=>set.delete(x),contains:x=>set.has(x),toggle:(x,on)=>on?set.add(x):set.delete(x)}};
 function element(id){return elements[id]??=( {id,value:'',textContent:'',innerHTML:'',disabled:['saveConfig','testConfig','historyGuard'].includes(id),hidden:false,checked:false,inert:false,dataset:{},classList:classes(),attrs:{},listeners:{},addEventListener(n,fn){this.listeners[n]=fn},setAttribute(k,v){this.attrs[k]=v},removeAttribute(k){delete this.attrs[k]},focus(){},scrollIntoView(){},showModal(){this.open=true},close(){this.open=false},reportValidity(){return this.valid!==false}});}
 const document={querySelector:s=>element(s.slice(1)),querySelectorAll:s=>s==='#configForm input,#configForm select'?Object.values(elements).filter(e=>e.id!=='historyGuard'):[],addEventListener(n,fn){handlers[n]=fn},activeElement:null};
 let failSave=false,locked=false;
 const fetch=async(url,opt={})=>{requests.push({url,opt});if(failed)throw new Error('offline');if(url==='/api/admin/config'&&!opt.method)return{ok:true,status:200,json:async()=>({ok:true,config,admin:{username:'admin'}})};if(url==='/api/admin/users')return{ok:true,status:200,json:async()=>({ok:true,users:[{user_id:4,username:'reader',created_at:'2026-10-27 00:00:00',locked}],admins:[]})};if(url==='/api/admin/config'&&opt.method==='PUT')return{ok:!failSave,status:failSave?400:200,json:async()=>failSave?{ok:false,error:'save rejected'}:{ok:true,config}};if(url==='/api/admin/config/test')return{ok:true,status:200,json:async()=>({ok:true,message:'配置格式检查通过；未发起外部连接'})};if(url==='/api/admin/users/4/lock'){locked=true;return{ok:true,status:200,json:async()=>({ok:true})}};throw new Error('unexpected '+url);};
 const storage=new Map();const sandbox={crypto:require('node:crypto').webcrypto,TextEncoder,sessionStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)},document,location:{protocol:file?'file:':'http:',href:''},window:{matchMedia:()=>({matches:false,addEventListener(){}})},fetch,Date,Boolean,Number,String,Object,RegExp};
 vm.runInNewContext(source,sandbox);return{element,elements,requests,setFailSave:x=>failSave=x};
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 const env=makeEnvironment();assert.equal(env.element('saveConfig').disabled,true);await flush();await flush();
 assert.equal(env.element('saveConfig').disabled,false);assert.equal(env.element('metricModel').textContent,config.llm.model);assert.equal(env.element('metricModel').innerHTML,'');assert.equal(env.element('metricSearch').textContent,'未配置');assert.equal(env.element('adminName').textContent,'admin');
 env.element('configForm').valid=false;let before=env.requests.length;await env.element('testConfig').onclick();assert.equal(env.requests.length,before);assert.match(env.element('notice').textContent,/修正/);env.element('configForm').valid=true;
 env.element('llmKey').value='new-secret';env.element('configForm').listeners.input();env.setFailSave(true);await env.element('configForm').listeners.submit({preventDefault(){}});assert.equal(env.element('llmKey').value,'new-secret');assert.match(env.element('runtimeStatus').textContent,/修改内容已保留/);assert.equal(env.element('saveConfig').disabled,false);
 env.setFailSave(false);await env.element('configForm').listeners.submit({preventDefault(){}});assert.equal(env.element('llmKey').value,'');const saved=env.requests.findLast(r=>r.opt.method==='PUT');assert.equal(JSON.parse(saved.opt.body).llm.api_key,'new-secret');assert.equal(JSON.parse(saved.opt.body).firecrawl.api_key,undefined);
 await env.element('testConfig').onclick();assert.match(env.element('runtimeStatus').textContent,/未发起外部连接/);
 const button={dataset:{user:'4',action:'lock'},textContent:'锁定',disabled:false};await env.element('usersBody').listeners.click({target:{closest:()=>button}});assert.equal(env.element('userSecurityDialog').open,true);assert.equal(env.requests.filter(r=>r.url==='/api/admin/users/4/lock').length,0);env.element('userSecurityReason').value='测试复核后锁定';env.element('userSecurityOtp').value='123456';await env.element('userSecurityForm').onsubmit({preventDefault(){}});assert.equal(env.element('userSecurityDialog').open,false);const lock=JSON.parse(env.requests.find(r=>r.url==='/api/admin/users/4/lock').opt.body);assert.equal(lock.reason,'测试复核后锁定');assert.equal(lock.otp_code,'123456');assert.match(lock.operation_id,/^[a-f0-9]{32}$/);await flush();assert.ok(env.requests.some(r=>r.url==='/api/admin/users/4/lock'&&r.opt.method==='POST'));assert.match(env.element('usersBody').innerHTML,/解锁/);
 const bad=makeEnvironment({failed:true});await flush();assert.equal(bad.element('saveConfig').disabled,true);assert.match(bad.element('configStatus').textContent,/失败/);
 const file=makeEnvironment({file:true});await flush();assert.equal(file.requests.length,0);assert.equal(file.element('fileNotice').hidden,false);assert.equal(file.element('saveConfig').disabled,true);
})().catch(e=>{console.error(e);process.exitCode=1});
'''.replace("SOURCE", json.dumps(source, ensure_ascii=False), 1)
        result = subprocess.run(["node", "-e", harness], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
