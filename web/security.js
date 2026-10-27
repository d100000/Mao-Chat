/* Shared, server-authoritative challenge UI. No credential or balance is stored here. */
(() => {
  'use strict';
  let loader, currentDialog;
  const loopback = () => ['localhost', '127.0.0.1', '[::1]', '::1'].includes(location.hostname);
  const requestId = () => crypto.randomUUID?.().replaceAll('-', '') || Array.from(crypto.getRandomValues(new Uint8Array(16)), x => x.toString(16).padStart(2, '0')).join('');
  const riskListeners=new Set();
  function reportRiskError(data={}){if(data.challenge&&['challenge_required','verification_required'].includes(data.code))return null;const id=typeof data.risk_event_id==='string'&&/^[a-f0-9]{32}$/.test(data.risk_event_id)?data.risk_event_id:null;if(id)for(const listener of riskListeners)listener({...data,risk_event_id:id});return id;}
  function riskErrorMessage(data,fallback){const id=reportRiskError(data);return (data.error||fallback)+(id?'（风险编号：'+id+'）':'');}
  function createAppeals({button,context}={}){
    let currentRisk=null,selectedRisk=null,dialog=null,busy=false,nextOffset=null,operation=null;const statuses={pending:'等待复核',approved:'申诉获支持',denied:'未予支持'};
    riskListeners.add(data=>{currentRisk=data.risk_event_id;if(button){button.dataset.riskEventId=currentRisk;button.textContent='安全申诉 · 查看风险编号';}});
    function elements(){return {form:dialog.querySelector('[data-appeal-form]'),note:dialog.querySelector('[data-appeal-note]'),code:dialog.querySelector('[data-appeal-code]'),status:dialog.querySelector('[data-appeal-status]'),list:dialog.querySelector('[data-appeal-list]'),more:dialog.querySelector('[data-appeal-more]')};}
    function controls(value){busy=value;for(const el of dialog.querySelectorAll('button,textarea,select'))el.disabled=value;elements().form.setAttribute('aria-busy',String(value));}
    async function progress(offset=0){if(busy)return;controls(true);const {list,more}=elements();try{const result=await json('/api/security/appeals?limit=20&offset='+offset);list.textContent='';const rows=Array.isArray(result.items)?result.items.slice(0,20):[];if(!rows.length)list.textContent='当前身份暂无申诉。';for(const row of rows){const article=document.createElement('article'),title=document.createElement('strong'),note=document.createElement('p'),reason=document.createElement('p');title.textContent=(statuses[row.status]||row.status||'待确认')+' · '+row.appeal_id;note.textContent=row.note||'';reason.textContent=row.review_reason?'复核说明：'+row.review_reason:row.status==='approved'?'':'提交后请等待复核，重复操作不会加快处理。';article.append(title,note,reason);if(row.status==='approved'){const policy=document.createElement('p');policy.textContent='复核结论已记录，账户和积分由管理员独立处理。';article.append(policy);}list.append(article);}nextOffset=result.has_more?(result.next_offset??offset+rows.length):null;more.hidden=nextOffset===null;}catch(error){list.textContent=error.message||'进度暂时不可用，请重试。';}finally{controls(false);}}
    function ensure(){if(dialog)return;dialog=document.createElement('dialog');dialog.className='security-dialog appeal-dialog';dialog.setAttribute('aria-labelledby','appealHeading');dialog.innerHTML='<h2 id="appealHeading">安全申诉与进度</h2><p data-appeal-risk></p><p>仅处理当前浏览器或账户的安全限制。申诉不会自动发放积分或解除限制。</p><form data-appeal-form><label>申诉类型<select data-appeal-code><option value="verification_required">安全验证问题</option><option value="benefit_pending">赠送资格待复核</option><option value="account_restricted">账户受限</option><option value="rate_limited">请求频率限制</option></select></label><label>补充说明<textarea data-appeal-note required maxlength="500" rows="4" placeholder="简要说明遇到的限制，最多500字。请勿填写密码、密钥或对话原文。"></textarea></label><p data-appeal-status role="status" aria-live="polite"></p><button type="submit">验证并提交申诉</button></form><details open><summary>当前身份的申诉进度</summary><div data-appeal-list></div><button type="button" data-appeal-more hidden>读取下一页</button></details><div class="appeal-actions"><button type="button" data-appeal-refresh>刷新进度</button><button type="button" data-appeal-close>返回</button></div>';
      const style=document.createElement('style');style.textContent='.appeal-dialog{width:min(460px,calc(100% - 32px));max-height:calc(100dvh - 48px);overflow:auto;padding:24px;border:1px solid var(--line,#dfdfd5);border-radius:8px;background:var(--panel,#fffefa);color:var(--ink,#20302d);font:14px/1.7 var(--sans,sans-serif)}.appeal-dialog::backdrop{background:#20302d66}.appeal-dialog h2{font-size:18px;margin:0 0 10px}.appeal-dialog p{font-size:12px;color:var(--muted,#65716b);overflow-wrap:anywhere}.appeal-dialog label{display:block;margin:14px 0;font-size:12px}.appeal-dialog select,.appeal-dialog textarea{display:block;width:100%;margin-top:6px;padding:10px;border:1px solid var(--line,#dfdfd5);border-radius:5px;background:transparent;color:inherit;font:inherit;box-sizing:border-box}.appeal-dialog textarea{resize:vertical}.appeal-dialog button{min-height:42px;padding:8px 12px;border:1px solid var(--line,#dfdfd5);border-radius:5px;background:transparent;color:inherit;font:inherit;cursor:pointer}.appeal-dialog button:disabled{opacity:.5}.appeal-dialog details{margin-top:20px;padding-top:16px;border-top:1px solid var(--line,#dfdfd5)}.appeal-dialog article{border-bottom:1px solid var(--line,#dfdfd5);padding:12px 0}.appeal-dialog article strong{font-size:11px;overflow-wrap:anywhere}.appeal-actions{display:flex;justify-content:space-between;gap:8px;margin-top:20px}';dialog.prepend(style);document.body.append(dialog);
      const {form,note,code,status,more}=elements();form.onsubmit=async event=>{event.preventDefault();if(busy||!form.reportValidity())return;const parameters={...(selectedRisk?{risk_event_id:selectedRisk}:{}),code:code.value,note:note.value.trim()},signature=JSON.stringify(parameters);if(operation?.signature!==signature)operation={signature,id:requestId()};controls(true);status.textContent='正在完成安全验证并提交…';try{const result=await secureRequest('/api/security/appeals',{method:'POST',body:JSON.stringify({...parameters,operation_id:operation.id})});status.textContent='申诉已提交，等待复核。编号：'+result.appeal_id;controls(false);await progress();}catch(error){status.textContent=error.message||'提交未确认，说明已保留，请重试。';}finally{controls(false);}};
      dialog.querySelector('[data-appeal-close]').onclick=()=>{if(!busy)dialog.close();};dialog.addEventListener('cancel',event=>{if(busy)event.preventDefault();});dialog.querySelector('[data-appeal-refresh]').onclick=()=>progress();more.onclick=()=>{if(nextOffset!==null)progress(nextOffset);};
    }
    async function open(){ensure();selectedRisk=currentRisk;const data=context?.()||{},el=elements();if(data.code&&['benefit_pending','account_restricted','rate_limited','verification_required'].includes(data.code))el.code.value=data.code;dialog.querySelector('[data-appeal-risk]').textContent=currentRisk?'风险编号：'+currentRisk:'当前未关联具体风险编号，将按所选问题类型提交。';if(!dialog.open)dialog.showModal();await progress();}
    if(button)button.onclick=()=>open();return Object.freeze({open});
  }
  async function json(url, options = {}) {
    const response = await fetch(url, {credentials: 'same-origin', ...options, headers: {'Content-Type': 'application/json', ...options.headers}});
    let data = {};
    try { data = await response.json(); } catch (_) {}
    if (!response.ok || data.ok === false) {
      const error = new Error(riskErrorMessage(data,'验证服务暂不可用，请稍后重试。'));
      error.status = response.status; error.data = data; throw error;
    }
    return data;
  }
  function loadWidget() {
    if (window.turnstile) return Promise.resolve(window.turnstile);
    if (!loader) loader = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      const timer=setTimeout(()=>{loader=null;script.remove();reject(new Error('验证组件加载超时。草稿已保留，请稍后重试。'));},15000);
      script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
      script.async = true; script.defer = true;
      script.onload = () => {clearTimeout(timer);if(window.turnstile)resolve(window.turnstile);else{loader=null;reject(new Error('验证组件未能加载，请稍后重试。'));}};
      script.onerror = () => {clearTimeout(timer);loader = null; reject(new Error('无法连接验证服务。草稿已保留，请稍后重试。'));};
      document.head.append(script);
    });
    return loader;
  }
  async function proofFromChallenge(challenge) {
    if (!challenge || typeof challenge.challenge_id !== 'string') throw new Error('验证请求无效，请重新提交。');
    let token;
    // Test proofs exist only when the server explicitly starts an isolated test mode.
    // The real verifier never accepts these tokens or a client-controlled test flag.
    if (challenge.provider === 'test' && loopback() && typeof challenge.test_token === 'string') token = challenge.test_token;
    else {
      if (challenge.provider !== 'turnstile' || !challenge.site_key) throw new Error('人机验证尚未配置，请联系管理员。');
      if (currentDialog) throw new Error('请先完成当前验证。');
      const widget = await loadWidget();
      token = await new Promise((resolve, reject) => {
        const dialog = document.createElement('dialog'); dialog.className = 'security-dialog';
        dialog.setAttribute('aria-labelledby', 'securityHeading');
        dialog.innerHTML = '<h2 id="securityHeading">完成一次安全验证</h2><p>用于保护账户和体验额度。你的问题与草稿会保留。</p><div class="security-widget"></div><p class="security-status" role="status" aria-live="polite">正在加载验证…</p><button type="button">取消并返回</button>';
        const style = document.createElement('style');
        style.textContent = '.security-dialog{--challenge-width:390px;width:min(var(--challenge-width),calc(100% - 32px));max-height:calc(100dvh - 48px);overflow:auto;padding:24px;border:1px solid var(--line,#dcded4);border-radius:8px;background:var(--panel,#fffefa);color:var(--ink,#20302d);font:14px/1.7 var(--sans,sans-serif)}.security-dialog::backdrop{background:#20302d66}.security-dialog h2{font-size:18px;margin:0 0 10px}.security-dialog p{color:var(--muted,#65716b);font-size:12px}.security-dialog button{min-height:44px;padding:8px 14px;border:1px solid var(--line,#dcded4);background:transparent;border-radius:5px;color:inherit;cursor:pointer}.security-widget{min-height:70px;overflow:auto}.security-status{min-height:20px}';
        dialog.prepend(style); document.body.append(dialog); currentDialog = dialog;
        let widgetId, finished = false;
        function finish(error, value) {if (finished) return; finished = true; if (widgetId !== undefined) widget.remove(widgetId); currentDialog = null; dialog.close(); dialog.remove(); error ? reject(error) : resolve(value);}
        dialog.querySelector('button').onclick = () => finish(new Error('已取消验证，问题已保留。'));
        dialog.addEventListener('cancel', event => {event.preventDefault(); finish(new Error('已取消验证，问题已保留。'));});
        dialog.showModal();
        widgetId = widget.render(dialog.querySelector('.security-widget'), {
          sitekey: challenge.site_key, action: challenge.action, cData: challenge.challenge_id, language: 'zh-cn', size:innerWidth<380?'compact':'normal',
          callback: value => finish(null, value),
          'error-callback': () => {dialog.querySelector('.security-status').textContent = '验证服务暂不可用，请取消后重试。';},
          'expired-callback': () => {dialog.querySelector('.security-status').textContent = '本次验证已过期，请取消后重新获取。';},
          'timeout-callback': () => {dialog.querySelector('.security-status').textContent = '验证超时，请取消后重试。';}
        });
      });
    }
    const result = await json('/api/security/verify', {method: 'POST', body: JSON.stringify({challenge_id: challenge.challenge_id, token})});
    if (typeof result.proof !== 'string') throw new Error('验证结果未确认，请重新提交。');
    return result.proof;
  }
  async function verifyAction(parameters) {
    const challenge = await json('/api/security/challenge', {method: 'POST', body: JSON.stringify(parameters)});
    return proofFromChallenge(challenge.challenge || challenge);
  }
  async function secureRequest(url, options = {}) {
    try { return await json(url, options); }
    catch (error) {
      const data = error.data || {};
      if (!['challenge_required', 'verification_required'].includes(data.code) && !data.challenge_required && !data.challenge) throw error;
      let payload;
      try { payload = JSON.parse(options.body || '{}'); } catch (_) { throw error; }
      const challenge = data.challenge || await json('/api/security/challenge', {method: 'POST', body: JSON.stringify({action: data.action, operation_id: data.operation_id || payload.operation_id, digest: data.digest})});
      const proof = await proofFromChallenge(challenge.challenge || challenge);
      const body = {...payload, operation_id: data.operation_id || payload.operation_id, security_proof: proof};
      return json(url, {...options, body: JSON.stringify(body)});
    }
  }
  window.JiaoyuanSecurity = Object.freeze({requestId, json, request: secureRequest, verifyAction, proofFromChallenge,createAppeals,reportRiskError,riskErrorMessage});
})();
