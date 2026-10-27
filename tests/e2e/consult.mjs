/** Real-provider acceptance for evidence-based dialogue consultation. */
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {randomBytes} from 'node:crypto';
import {createRequire} from 'node:module';
import {mkdir, rm, writeFile} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const require=createRequire(import.meta.url);
const {chromium}=require('playwright');
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../..');
const output=path.resolve(process.env.CONSULT_E2E_OUTPUT||path.join(root,'tmp/consult-browser'));
const provider='live-openai-compatible-and-firecrawl';
const timeout=180000;
await mkdir(output,{recursive:true});
await rm(path.join(output,'failure.png'),{force:true});
const checks=[],answers=[];let child,browser,page,base=process.env.CONSULT_E2E_BASE_URL,snapshot,failure;
const safeError=value=>String(value).replace(/\b(?:sk|fc)-[A-Za-z0-9_-]+/g,'[redacted]');
async function selectMode(mode){const button=page.locator('#modeSelect [data-mode='+mode+']');if(!await button.isVisible())await page.locator('#modeToolsToggle').click();await button.click();}
async function status(){const r=await fetch(base+'/__live_test/status');assert.ok(r.ok);return r.json();}
async function capture(name){await page.waitForTimeout(250);await page.screenshot({path:path.join(output,name+'.png'),fullPage:true});}
async function report(running=true){await writeFile(path.join(output,'report.json'),JSON.stringify({provider,model:snapshot?.model,running,passed:checks.filter(x=>x.status==='passed').length,failed:checks.filter(x=>x.status==='failed').length,checks,requests:snapshot?.requests||[]},null,2));await writeFile(path.join(output,'answers.json'),JSON.stringify({provider,answers},null,2));}
async function step(name,fn){const start=Date.now();try{const details=await fn();checks.push({name,status:'passed',elapsed_ms:Date.now()-start,...details});console.log('PASS '+name);}catch(e){checks.push({name,status:'failed',elapsed_ms:Date.now()-start,error:safeError(e)});await capture('failure').catch(()=>{});throw e;}finally{snapshot=await status().catch(()=>snapshot);await report();}}
async function captcha(){await page.waitForFunction(()=>document.querySelector('#math')?.src.startsWith('data:'));const src=await page.locator('#math').getAttribute('src');const m=Buffer.from(src.split(',')[1],'base64').toString().match(/>(\d+) ([+−\-×]) (\d+) = \?<\/text>/);assert.ok(m);const a=+m[1],b=+m[3];await page.locator('#mathAnswer').fill(String(m[2]==='+'?a+b:m[2]==='×'?a*b:a-b));}
function evidenceIds(value,ids=new Set()){
 if(!value||typeof value!=='object')return ids;
 if(Array.isArray(value)){for(const item of value)evidenceIds(item,ids);return ids;}
 for(const [key,item] of Object.entries(value)){
  if(['id','evidence_id','quote_id','event_id','rule_id'].includes(key)&&typeof item==='string'){
   ids.add(item);for(const match of item.matchAll(/\b(?:VE|VL|TL)\d+\b/g))ids.add(match[0]);
  }
  if(key==='evidence_ids'&&Array.isArray(item))item.forEach(x=>ids.add(String(x)));
  if(item&&typeof item==='object')evidenceIds(item,ids);
 }
 return ids;
}
async function ask(label,question){
 const index=await page.evaluate(()=>window.__consultStreams?.length||0),start=Date.now();
 const responsePromise=page.waitForResponse(r=>r.url().endsWith('/api/agent/stream')&&r.request().method()==='POST',{timeout});
 await page.locator('#question').fill(question);await page.locator('#sendButton').click();assert.equal((await responsePromise).status(),200);
 await page.waitForFunction(index=>window.__consultStreams?.[index]?.finished,index,{timeout});
 const captured=await page.evaluate(index=>window.__consultStreams[index],index);assert.ok(!captured.error,captured.error);
 const events=captured.text.split(/\r?\n\r?\n/).map(x=>x.split(/\r?\n/).filter(l=>l.startsWith('data:')).map(l=>l.slice(5).trim()).join('\n')).filter(Boolean).map(x=>JSON.parse(x));
 assert.deepEqual(events.filter(x=>x.type==='error'),[]);assert.ok(events.some(x=>x.type==='done'));
 const contentEvents=events.filter(x=>x.type==='content');const answer=contentEvents.map(x=>x.text||'').join('');assert.ok(answer.trim().length>30);assert.doesNotMatch(answer,/浏览器测试替身|deterministic-test-double/);
 assert.doesNotMatch(answer,/证据门提示：回答含未在本轮工具结果中返回/,'compound citations must also use known evidence IDs');
 await page.waitForFunction(()=>document.querySelector('#sendButton')?.title==='发送',null,{timeout:10000});
 snapshot=await status();const trace=snapshot.requests.at(-1);assert.equal(trace.status,'ok');
 const tools=events.filter(x=>x.type==='tool');const ids=[...evidenceIds(tools.map(x=>x.result))];
 const analyses=events.filter(x=>x.type==='analysis').map(x=>x.analysis||x.data||x);
 if(trace.mode==='consult'){assert.ok(tools.some(x=>x.name==='understand_user'),'update the user-correctable understanding');assert.ok(analyses.length,'publish an understanding event');assert.equal(analyses.at(-1).tentative,true);}
 const details={answer_chars:answer.length,tool_names:tools.map(x=>x.name),evidence_ids:ids,analysis_events:analyses,elapsed_ms:Date.now()-start,trace};
 answers.push({label,question,answer,...details});await capture(label);return {answer,...details};
}
async function launch(){if(base)return;child=spawn(process.env.E2E_PYTHON||'python3',['scripts/live_test_server.py','--port','0'],{cwd:root,stdio:['ignore','pipe','pipe']});base=await new Promise((resolve,reject)=>{let buffer='';const timer=setTimeout(()=>reject(new Error('live consultation server readiness timeout')),30000);child.stdout.on('data',data=>{buffer+=data.toString();for(const line of buffer.split('\n')){try{const x=JSON.parse(line);if(x.ready){clearTimeout(timer);resolve(x.base_url);return;}}catch{}}});child.stderr.on('data',data=>process.stderr.write(safeError(data)));child.on('exit',code=>{clearTimeout(timer);reject(new Error('consult test server exited '+code));});});}
try{
 await launch();snapshot=await status();assert.equal(snapshot.provider,provider);
 browser=await chromium.launch({headless:true,channel:process.env.E2E_BROWSER_CHANNEL==='bundled'?undefined:(process.env.E2E_BROWSER_CHANNEL||'chrome')});
 const context=await browser.newContext({viewport:{width:1440,height:1000}});
 await context.addInitScript(()=>{window.__consultStreams=[];const fetchOriginal=window.fetch.bind(window);window.fetch=async(...args)=>{const response=await fetchOriginal(...args);const url=typeof args[0]==='string'?args[0]:args[0]?.url;if(url?.includes('/api/agent/stream')){const entry={finished:false,status:response.status};window.__consultStreams.push(entry);response.clone().text().then(text=>{entry.text=text;entry.finished=true;}).catch(e=>{entry.error=String(e);entry.finished=true;});}return response;};});
 page=await context.newPage();page.setDefaultTimeout(15000);const browserErrors=[];page.on('pageerror',e=>browserErrors.push(safeError(e)));
 await step('register, login and consultation is the default',async()=>{await page.goto(base+'/auth');await page.locator('#registerTab').click();await page.locator('#username').fill('consult_'+Date.now());await page.locator('#password').fill(randomBytes(20).toString('base64url'));await captcha();await page.locator('#submit').click();await page.waitForFunction(()=>document.querySelector('#loginTab')?.getAttribute('aria-selected')==='true'&&!document.querySelector('#submit').disabled);await captcha();await page.locator('#submit').click();await page.waitForURL('**/app');await page.waitForFunction(()=>!document.querySelector('#question')?.disabled);assert.equal(await page.locator('#modeSelect [data-mode=consult]').getAttribute('aria-pressed'),'true');await capture('workspace');});
 await step('ambiguous concern leads to focused clarification',async()=>{const r=await ask('clarify','最近工作很累，总觉得做什么都不对，想理清下一步。');assert.equal(r.trace.mode,'consult');assert.equal(r.trace.network_enabled,false);assert.equal(r.analysis_events.at(-1).phase,'clarify');const questionMarks=(r.answer.match(/[？?]/g)||[]).length;assert.ok(questionMarks>=1&&questionMarks<=4,'ask a small number of useful questions instead of inventing the situation');return {...r,question_marks:questionMarks};});
 await step('goals and constraints produce grounded practical analysis',async()=>{const r=await ask('grounded-plan','我带一个两人的软件团队，六周内需要做出让客户愿意用的版本。最近三个方向都有人催，我不知道先做哪个；不能再招人，也没有额外预算。我现在想知道该怎么选重点，先做哪一步。');assert.equal(r.trace.mode,'consult');assert.ok(r.trace.history_count>=2);assert.notEqual(r.analysis_events.at(-1).phase,'clarify');assert.ok(r.tool_names.some(x=>/method_search|search_methods/.test(x)),'use the grounded methodology tool for advice');assert.ok(r.evidence_ids.length,'retrieve traceable historical evidence');assert.ok(r.evidence_ids.some(id=>/^(VE|VL|TL)\d+$/.test(id)&&r.answer.includes(id)),'explain advice with a retrieved evidence locator');assert.match(r.answer,/六周|6周|六个星期|6个星期/);assert.match(r.answer,/验证|访谈|记录|复盘|试验|实验|检查|反馈/);return r;});
 await step('feedback and a corrected goal change the recommendation',async()=>{const r=await ask('revise-plan','我想纠正一下：现在最重要的不是获新客，而是让现有的三家客户留下来。我照你说的先访谈了两位新用户，他们都说不会付费；现有客户更在意稳定性。六周期限没变。请按这个新目标调整，不要继续把拉新当成主要任务。');assert.ok(r.trace.history_count>=4);assert.match(r.analysis_events.at(-1).goal,/现有|老客户|留存|续费|稳定|保留/);assert.match(r.answer,/三家|3家|三个|3个|现有客户|现有.*客户/);assert.match(r.answer,/稳定|留存|留下|保留|续费/);assert.doesNotMatch(r.answer,/你上周|上周你|上周的做法/,'do not invent when the user tried an action');assert.doesNotMatch(r.answer,/谁有面子|谁的面子/,'do not infer a stakeholder motive without user evidence');assert.doesNotMatch(r.answer,/三家客户加上?(?:两|2)个催促者|变成(?:四|4)个方向|会直接筛掉(?:两|2)个方向/,'unknown relationships and option contents cannot become facts');return r;});
 await step('restored consultation retains the goal and supports the next experiment',async()=>{await page.reload();await page.waitForFunction(()=>!document.querySelector('#question')?.disabled);assert.equal(await page.locator('#modeSelect [data-mode=consult]').getAttribute('aria-pressed'),'true');assert.ok(await page.locator('.message.assistant').count());assert.match(await page.locator('#analysisBody').innerText(),/待你确认/);assert.match(await page.locator('#analysisBody').innerText(),/现有|老客户|留存|续费|稳定|保留/);const r=await ask('follow-through','请只根据我们已经说过的情况，用不超过180字帮我定出本周一个小实验，并写清用什么信号判断要继续还是调整。');assert.ok(r.trace.history_count>=6);assert.match(JSON.stringify(r.analysis_events.at(-1)),/现有|老客户|留存|续费|稳定|保留|留下/);assert.match(r.answer,/稳定|现有|留存|续费|老客户|留下|续用|更稳/);assert.match(r.answer,/指标|次数|数量|记录|反馈|信号|是否|检查|比率|比例/);return r;});
 await step('legacy research mode still returns historical evidence',async()=>{await page.locator('#newChat').click();await selectMode('research');const r=await ask('research-regression','请先检索本地《实践论》，用不超过100字说明实践与认识的关系，并标出证据ID或PDF页码。');assert.equal(r.trace.mode,'research');assert.ok(r.tool_names.some(x=>['local_search','trace_quote','local_vector_search'].includes(x)));return r;});
 await step('consultation layout works on a narrow screen',async()=>{await page.locator('#newChat').click();await selectMode('consult');await page.setViewportSize({width:390,height:844});await page.waitForTimeout(300);assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await capture('mobile');});
 await step('logout revokes the live test session and browser has no uncaught errors',async()=>{await page.setViewportSize({width:1440,height:1000});await page.locator('#logout').click();await page.waitForURL('**/auth');assert.equal((await page.request.get(base+'/api/auth/me')).status(),401);assert.deepEqual(browserErrors,[]);});
}catch(e){failure=e;console.error(safeError(e.stack||e));}finally{
 if(base)snapshot=await status().catch(()=>snapshot);await browser?.close();
 if(child){child.kill('SIGINT');await new Promise(resolve=>{child.once('exit',resolve);setTimeout(resolve,5000);});}
 await report(false);
}
if(failure)process.exitCode=1;
