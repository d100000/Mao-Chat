import {confirmGeneration,readyAuth,guestLogout} from './helpers/credits.mjs';
/** Explicit live-provider acceptance: real browser, HTTP, model and Firecrawl. */
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
const output=path.resolve(process.env.LIVE_E2E_OUTPUT||path.join(root,'tmp/live-browser'));
const provider='live-openai-compatible-and-firecrawl';
const questionTimeout=180000;
await mkdir(output,{recursive:true});
await rm(path.join(output,'failure.png'),{force:true});
const checks=[],answers=[];let child,browser,page,base=process.env.LIVE_E2E_BASE_URL;
const safeError=value=>String(value).replace(/\b(?:sk|fc)-[A-Za-z0-9_-]+/g,'[redacted]');
async function selectMode(mode){const button=page.locator('#modeSelect [data-mode='+mode+']');if(!await button.isVisible())await page.locator('#modeToolsToggle').click();await button.click();}
async function status(){const r=await fetch(base+'/__live_test/status');assert.ok(r.ok);return r.json();}
async function capture(name){await page.waitForTimeout(250);await page.screenshot({path:path.join(output,name+'.png'),fullPage:true});}
async function step(name,fn){const started=Date.now();try{const details=await fn();checks.push({name,status:'passed',elapsed_ms:Date.now()-started,...details});console.log('PASS '+name);}catch(e){checks.push({name,status:'failed',elapsed_ms:Date.now()-started,error:safeError(e)});await capture('failure').catch(()=>{});throw e;}}
async function captcha(){await readyAuth(page);}
async function ask(label,question){
 const started=Date.now();const streamIndex=await page.evaluate(()=>window.__liveStreams?.length||0);const responsePromise=page.waitForResponse(r=>r.url().endsWith('/api/agent/stream')&&r.request().method()==='POST',{timeout:questionTimeout});
 await page.locator('#question').fill(question);await confirmGeneration(page,{timeout:30000});const response=await responsePromise;assert.equal(response.status(),200,'question SSE status');
 await page.waitForFunction(index=>window.__liveStreams?.[index]?.finished,streamIndex,{timeout:questionTimeout});const captured=await page.evaluate(index=>window.__liveStreams[index],streamIndex);assert.ok(!captured.error,captured.error);const body=captured.text;
 const events=body.split(/\r?\n\r?\n/).map(block=>block.split(/\r?\n/).filter(line=>line.startsWith('data:')).map(line=>line.slice(5).trim()).join('\n')).filter(Boolean).map(data=>JSON.parse(data));
 const errors=events.filter(e=>e.type==='error');assert.deepEqual(errors,[],'real provider must complete without error');assert.ok(events.some(e=>e.type==='done'),'completed SSE event');
 const answer=events.filter(e=>e.type==='content').map(e=>e.text||'').join('');assert.ok(answer.trim().length>30,'real answer must contain substantive text');assert.doesNotMatch(answer,/浏览器测试替身|deterministic-test-double/,'a real provider must generate the answer');
 await page.waitForFunction(()=>document.querySelector('#sendButton')?.title==='发送',null,{timeout:10000});
 const snapshot=await status(),trace=snapshot.requests.at(-1);assert.equal(trace.status,'ok');const toolNames=events.filter(e=>e.type==='tool').map(e=>e.name);
 const details={answer_chars:answer.length,tool_names:toolNames,elapsed_ms:Date.now()-started,trace};answers.push({label,question,answer,...details});await capture(label);return details;
}
async function launchServer(){if(base)return;child=spawn(process.env.E2E_PYTHON||'python3',['scripts/live_test_server.py','--port','0'],{cwd:root,stdio:['ignore','pipe','pipe']});base=await new Promise((resolve,reject)=>{let buffer='';const timer=setTimeout(()=>reject(new Error('live test server readiness timeout')),30000);child.stdout.on('data',data=>{buffer+=data.toString();for(const line of buffer.split('\n')){try{const x=JSON.parse(line);if(x.ready){clearTimeout(timer);resolve(x.base_url);return;}}catch{}}});child.stderr.on('data',data=>process.stderr.write(safeError(data)));child.on('exit',code=>{clearTimeout(timer);reject(new Error('live test server exited '+code));});});}
let failure,snapshot;
try{
 await launchServer();snapshot=await status();assert.equal(snapshot.provider,provider,'only target the isolated live-test server');assert.equal(snapshot.model,'deepseek-v4.1-flash');assert.ok(snapshot.max_steps<=6);
 browser=await chromium.launch({headless:true,channel:process.env.E2E_BROWSER_CHANNEL==='bundled'?undefined:(process.env.E2E_BROWSER_CHANNEL||'chrome')});const context=await browser.newContext({viewport:{width:1440,height:1000}});await context.addInitScript(()=>{window.__liveStreams=[];const originalFetch=window.fetch.bind(window);window.fetch=async(...args)=>{const response=await originalFetch(...args);const url=typeof args[0]==='string'?args[0]:args[0]?.url;if(url?.includes('/api/agent/stream')){const entry={finished:false,status:response.status};window.__liveStreams.push(entry);response.clone().text().then(text=>{entry.text=text;entry.finished=true;}).catch(error=>{entry.error=String(error);entry.finished=true;});}return response;};});page=await context.newPage();page.setDefaultTimeout(15000);const browserErrors=[];page.on('pageerror',e=>browserErrors.push(safeError(e)));
 const username='live_'+Date.now(),password=randomBytes(20).toString('base64url');
 await step('register and login through the live UI',async()=>{await page.goto(base+'/auth');await page.locator('#registerTab').click();await page.locator('#username').fill(username);await page.locator('#password').fill(password);await captcha();await page.locator('#submit').click();await page.waitForFunction(()=>document.querySelector('#loginTab')?.getAttribute('aria-selected')==='true'&&!document.querySelector('#submit').disabled);await captcha();await page.locator('#submit').click();await page.waitForURL('**/app');await page.waitForFunction(()=>!document.querySelector('#question')?.disabled);await capture('workspace');});
 await step('research question uses real local retrieval',async()=>{await selectMode('research');const r=await ask('research','请先调用 local_search 检索“实践论”，再用不超过180字解释实践与认识的关系，给出可回看的来源或页码。OCR页面只能标为候选，不要无依据逐字引用。');assert.ok(r.tool_names.some(n=>n==='local_search'||n==='trace_quote'||n==='local_vector_search'));assert.equal(r.trace.mode,'research');assert.equal(r.trace.network_enabled,false);return r;});
 await step('follow-up receives real prior conversation context',async()=>{const r=await ask('follow-up','请接着上一轮的问题，用不超过120字解释这一认识方法如何避免空谈；继续检索本地原文来支持说明。');assert.ok(r.trace.history_count>=2);return r;});
 await step('historical mode respects cutoff and disables web search',async()=>{await page.locator('#newChat').click();await selectMode('historical');await page.locator('#asOf').fill('1938-06-30');const r=await ask('historical','现在的截止日期是1938年6月30日。请先调用 local_search 检索“实践论”，仅依据截止日期前的资料，用不超过160字解释实践与认识。不要谈论此日期之后的事情。');assert.equal(r.trace.mode,'historical');assert.equal(r.trace.as_of,'1938-06-30');assert.equal(r.trace.network_enabled,false);assert.ok(!r.tool_names.includes('firecrawl_search'));return r;});
 await step('modern mode generates explicitly modern advice',async()=>{await page.locator('#newChat').click();await selectMode('modern');const r=await ask('modern','请先检索《实践论》相关本地资料，再给今天的小型软件项目提出一个三步决策流程。明确标为现代建议，不超过180字。');assert.equal(r.trace.mode,'modern');return r;});
 await step('network-enabled answer calls the real Firecrawl search',async()=>{await page.locator('#newChat').click();await selectMode('research');await page.locator('#searchToggle').click();const r=await ask('firecrawl','请务必调用 firecrawl_search 搜索“site:docs.firecrawl.dev search scrapeOptions markdown”，核对 https://docs.firecrawl.dev/introduction 所属官方文档中 Firecrawl 搜索是否支持获取 Markdown。即使本地有资料也必须实际联网。请用不超过160字回答，附真实网页URL并标记网页为候选。');assert.equal(r.trace.network_enabled,true);assert.ok(r.tool_names.includes('firecrawl_search'),'the real search tool must run');const searches=r.trace.tool_results.filter(x=>x.name==='firecrawl_search');assert.ok(searches.some(x=>x.result_count>0&&!x.error),'Firecrawl must return a successful result');assert.ok(searches.every(x=>x.untrusted),'web data remains candidate material');assert.match(await page.locator('#evidenceBody').innerText(),/候选/);return r;});
 await step('restore conversation and logout',async()=>{await page.reload();await page.waitForFunction(()=>!document.querySelector('#question')?.disabled);assert.ok(await page.locator('.message.assistant').count());await guestLogout(page,base);});
 await step('no uncaught browser exceptions',async()=>assert.deepEqual(browserErrors,[]));
}catch(e){failure=e;console.error(safeError(e.stack||e));}finally{
 if(base)snapshot=await status().catch(()=>snapshot);await browser?.close();
 if(child){child.kill('SIGINT');await new Promise(resolve=>{child.once('exit',resolve);setTimeout(resolve,5000);});}
 const report={provider,model:snapshot?.model,reasoning:snapshot?.reasoning,reasoning_effort:snapshot?.reasoning_effort,max_steps:snapshot?.max_steps,passed:checks.filter(x=>x.status==='passed').length,failed:checks.filter(x=>x.status==='failed').length,checks,requests:snapshot?.requests||[]};
 await writeFile(path.join(output,'report.json'),JSON.stringify(report,null,2));await writeFile(path.join(output,'answers.json'),JSON.stringify({provider,answers},null,2));
}
if(failure)process.exitCode=1;
