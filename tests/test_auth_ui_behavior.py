"""Execute the login UI scripts with a minimal DOM to test state transitions."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node.js is unavailable")
class AuthUIBehaviorTests(unittest.TestCase):
    def run_script(self, page, assertions, setup=""):
        source = (ROOT / "web" / page).read_text(encoding="utf-8")
        script = re.search(r"<script>([\s\S]*)</script>", source).group(1)
        harness = r'''
const vm = require('node:vm');
const assert = require('node:assert/strict');
const elements=new Map();
function el(id){if(!elements.has(id))elements.set(id,{id,value:'',type:id==='password'?'password':'text',textContent:'',disabled:false,hidden:false,attributes:{},dataset:{},setAttribute(k,v){this.attributes[k]=v;},focus(){this.focused=true;},reportValidity(){return true;}});return elements.get(id);}
const tabs=[el('loginTab'),el('registerTab')]; tabs[0].dataset.mode='login';tabs[1].dataset.mode='register';
let captchaFailure=false, verificationFailure=false, postFailure=false, holdLogin=false, finishLogin;
const calls=[];let proofCounter=0;
const response=(data,ok=true,status=ok?200:400)=>({ok,status,json:async()=>data});
const context={URL,URLSearchParams,document:{body:{dataset:{surface:SURFACE}},querySelector:s=>el(s.slice(1)),querySelectorAll:s=>s==='[data-mode]'?tabs:[]},location:{origin:'http://localhost:8765',hostname:'localhost',protocol:'http:',search:'',href:''},fetch:async(url,opts)=>{
  calls.push({url,opts});
  if(url==='/api/security/status'){if(verificationFailure)throw new Error('network unavailable');return response({configured:true,provider:'test'});}
  if(url==='/api/auth/captcha'){if(captchaFailure)throw new Error('network unavailable');return response({challenge_id:'math-'+calls.length,image:'data:image/svg+xml;base64,captcha'});}
  if(url==='/api/account/guest-claim')return response({ok:true,state:'committed',thread_map:{}});
  const body=JSON.parse(opts?.body||'{}');
  if(url==='/api/security/verify'){assert.match(body.token,/^random-test-token-/);return response({ok:true,proof:'proof-'+(++proofCounter)});}
  if(['/api/auth/login','/api/auth/register'].includes(url)&&!body.security_proof)return response({ok:false,code:'challenge_required',operation_id:body.operation_id,digest:'a'.repeat(64),challenge:{provider:'test',challenge_id:'challenge-'+calls.length,test_token:'random-test-token-'+calls.length}},false,403);
  if(holdLogin&&url.includes('login'))return new Promise(resolve=>{finishLogin=()=>resolve(response({ok:true}));});
  return response(postFailure?{ok:false,error:'用户名或密码错误'}:{ok:true},!postFailure);
}};
const settle=()=>new Promise(resolve=>setImmediate(resolve));
'''.replace("SURFACE", json.dumps("admin" if page == "admin-login.html" else "user"))
        shared = "\ncontext.window=context;context.crypto=require('node:crypto').webcrypto;context.sessionStorage={getItem(){return null;},setItem(){},removeItem(){}};el('claimGuest').checked=true;\n"
        for asset in ("security.js", "credits.js"):
            shared += "vm.runInNewContext(" + json.dumps((ROOT / "web" / asset).read_text(encoding="utf-8")) + ",context);\n"
        code = harness + "\n" + setup + shared + "\nvm.runInNewContext(" + json.dumps(script) + ",context);\n(async()=>{await settle();\n" + assertions + "\n})().catch(e=>{console.error(e);process.exitCode=1;});"
        result = subprocess.run(["node", "-e", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_registration_preserves_credentials_and_returns_to_login(self):
        self.run_script("auth.html", r'''
el('username').value='new_reader';el('password').value='test-password';
tabs[1].onclick();
assert.equal(el('heading').textContent,'创建账户');
assert.equal(el('password').autocomplete,'new-password');
el('mathAnswer').value='4';
await el('form').onsubmit({preventDefault(){}});
assert.equal(calls.filter(c=>c.url==='/api/auth/register').length,2);assert.equal(calls.filter(c=>c.url==='/api/security/verify').length,1);assert.equal(calls.filter(c=>c.url==='/api/auth/captcha').length,0);
assert.equal(el('username').value,'new_reader');
assert.equal(el('password').value,'test-password');
assert.equal(el('heading').textContent,'登录工作台');
assert.match(el('message').textContent,/账户已创建/);
assert.equal(context.location.href,'');
assert.equal(el('submit').disabled,false);
''')

    def test_busy_login_prevents_duplicate_submissions(self):
        for page in ("auth.html", "admin-login.html"):
            with self.subTest(page=page):
                self.run_script(page, r'''
holdLogin=true;el('username').value='reader';el('password').value='test-password';
const first=el('form').onsubmit({preventDefault(){}});
await settle();
await el('form').onsubmit({preventDefault(){}});
const attempts=calls.filter(c=>c.url.includes('login'));assert.equal(attempts.length,context.document.body.dataset.surface==='admin'?1:2);if(context.document.body.dataset.surface!=='admin')assert.equal(JSON.parse(attempts[0].opts.body).operation_id,JSON.parse(attempts[1].opts.body).operation_id);
assert.equal(el('submit').disabled,true);
assert.equal(el('form').attributes['aria-busy'],'true');
finishLogin();await first;
assert.equal(el('form').attributes['aria-busy'],'false');
assert.ok(['/app','/admin'].includes(context.location.href));
''')

    def test_failed_login_keeps_input_and_uses_managed_challenge(self):
        self.run_script("auth.html", r'''
postFailure=true;el('username').value='reader';el('password').value='test-password';el('mathAnswer').value='99';
await el('form').onsubmit({preventDefault(){}});
assert.equal(el('username').value,'reader');assert.equal(el('password').value,'test-password');
assert.equal(calls.filter(c=>c.url==='/api/security/verify').length,1);
assert.equal(el('submit').disabled,false);
assert.equal(calls.filter(c=>c.url==='/api/auth/captcha').length,0);
assert.match(el('message').textContent,/用户名或密码错误/);
''')

    def test_verification_service_failure_can_be_retried(self):
        self.run_script("auth.html", r'''
assert.equal(el('submit').disabled,true);
assert.match(el('message').textContent,/重试/);
verificationFailure=false;el('refresh').onclick();await settle();
assert.equal(el('submit').disabled,false);
''', "verificationFailure=true;")

    def test_password_visibility_toggle(self):
        self.run_script("admin-login.html", r'''
el('reveal').onclick();assert.equal(el('password').type,'text');assert.equal(el('reveal').attributes['aria-pressed'],'true');
el('reveal').onclick();assert.equal(el('password').type,'password');assert.equal(el('reveal').attributes['aria-pressed'],'false');
''')

    def test_tabs_support_arrow_key_navigation(self):
        self.run_script("auth.html", r'''
tabs[0].onkeydown({key:'ArrowRight',preventDefault(){}});
assert.equal(el('heading').textContent,'创建账户');assert.equal(tabs[1].tabIndex,0);assert.equal(tabs[0].tabIndex,-1);assert.equal(tabs[1].focused,true);
tabs[1].onkeydown({key:'Home',preventDefault(){}});
assert.equal(el('heading').textContent,'登录工作台');assert.equal(tabs[0].tabIndex,0);
''')

    def test_login_redirect_stays_same_origin(self):
        for destination in ('//evil.example', '/\\evil.example', 'https://evil.example'):
            with self.subTest(destination=destination):
                self.run_script("auth.html", "context.location.search='?next='+encodeURIComponent(" + json.dumps(destination) + ");await el('form').onsubmit({preventDefault(){}});assert.equal(context.location.href,'/app');")


if __name__ == "__main__":
    unittest.main()
