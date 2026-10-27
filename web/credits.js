/* Display-only wallet and expiring conversation cache. The server owns every balance. */
(() => {
  'use strict';
  const CACHE_TTL = 180 * 86400000, HANDOFF_KEY = 'jiaoyuan-account-handoff';
  let database;
  const cacheDB = () => {
    if (!window.indexedDB) return Promise.resolve(null);
    if (!database) database = new Promise(resolve => {
      const request = indexedDB.open('jiaoyuan-display-cache', 1);
      request.onupgradeneeded = () => request.result.createObjectStore('records');
      request.onsuccess = () => {
        const db=request.result,tx=db.transaction('records','readwrite'),cursor=tx.objectStore('records').openCursor();
        cursor.onsuccess=()=>{const item=cursor.result;if(item){if(!Number.isFinite(item.value?.saved_at)||Date.now()-item.value.saved_at>CACHE_TTL)item.delete();item.continue();}};
        resolve(db);
      };
      request.onerror = () => resolve(null);
    });
    return database;
  };
  async function mirrorCache(key, entry) {try {const db = await cacheDB(); if (db) db.transaction('records', 'readwrite').objectStore('records').put(entry, key);} catch (_) {}}
  function readCache(key, fallback) {
    try {
      const stored = JSON.parse(localStorage.getItem(key));
      if (stored?.cache_version === 2) {
        if (Date.now() - stored.saved_at > CACHE_TTL) {localStorage.removeItem(key); return fallback;}
        return stored.value ?? fallback;
      }
      return stored ?? fallback;
    } catch (_) {return fallback;}
  }
  function writeCache(key, value) {try {const entry = {cache_version: 2, saved_at: Date.now(), value}; localStorage.setItem(key, JSON.stringify(entry)); mirrorCache(key, entry);} catch (_) {}}
  async function clearCache(prefix) {
    if (!prefix) return;
    try {
      Object.keys(localStorage).filter(key => key.startsWith(prefix)).forEach(key => localStorage.removeItem(key));
      const db = await cacheDB();
      if (db) await new Promise(resolve => {
        const tx = db.transaction('records', 'readwrite'), request = tx.objectStore('records').openCursor();
        request.onsuccess = () => {const cursor = request.result; if (cursor) {if (String(cursor.key).startsWith(prefix)) cursor.delete(); cursor.continue();}};
        tx.oncomplete = resolve; tx.onerror = resolve;
      });
    } catch (_) {}
  }
  function pointLabel(value) {
    const text=String(value??'');if(!/^\d+(?:\.\d{1,6})?$/.test(text))return '—';
    const [whole,part='']=text.split('.'),fraction=part.slice(0,2).padEnd(2,'0');
    if(BigInt(whole)===0n&&fraction==='00'&&/[1-9]/.test(part))return '低于0.01';
    return BigInt(whole).toLocaleString('zh-CN')+(fraction==='00'?'':'.'+fraction.replace(/0$/,''));
  }
  function preciseLabel(value) {const text=String(value??'');return /^\d+(?:\.\d{1,6})?$/.test(text)?text.replace(/(\.\d*?[1-9])0+$|\.0+$/,'$1'):'—';}
  function dateTime(value) {if (!value) return '未设置'; const date = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value); return Number.isNaN(date.valueOf()) ? '未设置' : date.toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai', hour12: false});}
  function handoff() {try {const item = JSON.parse(sessionStorage.getItem(HANDOFF_KEY)); return item && Date.now() - item.saved_at < 86400000 ? item : null;} catch (_) {return null;}}
  function create({state, $, api, showToast, setBusy, closeDrawers, friendlyError, beforeGeneration}) {
    function renderWallet(wallet) {
      state.wallet = wallet; const available = wallet.available ?? wallet.available_points, held = wallet.held ?? wallet.held_points;
      $('#pointsAvailable').textContent = pointLabel(available); $('#pointsHeld').textContent = pointLabel(held);
      const nearest = wallet.next_expiry_at || wallet.expires_at || (wallet.lots || []).filter(item => Number(item.available ?? item.remaining) > 0).map(item => item.expires_at).sort()[0];
      $('#walletStatus').classList.remove('wallet-status-error');
      $('#walletStatus').textContent = wallet.benefit_pending ? '赠送资格待验证' : nearest ? '最近到期 ' + dateTime(nearest) : state.isGuest ? wallet.trial_granted?'访客额度仅赠送一次':'首次有效发送验证后领取100积分' : '每日赠送100积分，当天未用部分到期';
      $('#creditAction').classList.toggle('hidden', !state.isGuest || Number(available) > 0);$('#creditAction').textContent=wallet.trial_granted?'注册后今天可获100积分':'注册 / 登录';
    }
    function renderDetails(items) {
      const wallet = state.wallet || {}, body = $('#walletBody'); body.textContent = '';
      const summary = document.createElement('div'); summary.className = 'wallet-breakdown';
      for (const [label, value] of [['可用积分', wallet.available ?? wallet.available_points], ['本轮冻结', wallet.held ?? wallet.held_points]]) {
        const cell = document.createElement('div'), name = document.createElement('small'), amount = document.createElement('strong'); name.textContent = label; amount.textContent = pointLabel(value); cell.append(name, amount); summary.append(cell);
      }
      body.append(summary);
      const note = document.createElement('p'); note.textContent = state.isGuest ? '访客一次性100积分，自领取起180天有效。注册后每天获100积分，当日未用部分到期。' : '每日积分按北京时间自然日发放，未使用部分当日到期。访客承接额度保留原到期时间。'; body.append(note);
      for (const lot of wallet.lots || []) {
        const row = document.createElement('div'); row.className = 'wallet-lot'; row.textContent = ({guest: '访客体验', daily: '每日赠送', guest_trial: '访客体验', daily_grant: '每日赠送', daily_gift: '每日赠送', adjustment: '额度调整', refund: '费用补偿'}[lot.source || lot.kind] || lot.source || lot.kind || '额度批次') + ' · 可用 ' + pointLabel(lot.available ?? lot.remaining) + '积分';
        const expiry = document.createElement('small'); expiry.textContent = '到期 ' + dateTime(lot.expires_at); row.append(expiry); body.append(row);
      }
      const heading = document.createElement('h3'); heading.textContent = '最近使用明细'; body.append(heading);
      if (!items.length) {const empty = document.createElement('p'); empty.textContent = '暂无积分变动。浏览已有回答不会扣分。'; body.append(empty);}
      for (const item of items) {
        const row = document.createElement('div'); row.className = 'wallet-entry'; const title = document.createElement('strong');
        title.textContent = ({grant: '赠送', reserve: '冻结', settle: '实际消费', release: '冻结释放', expire: '额度到期', transfer: '额度转移', reversal: '账目冲正'}[item.kind || item.event] || item.kind || item.event || '积分变动') + ' · ' + String(item.points ?? item.amount ?? '—') + '积分';
        const meta = document.createElement('small'); meta.textContent = dateTime(item.created_at || item.occurred_at) + ' · ' + (item.status || item.request_id || ''); row.append(title, meta);
        if(item.request_id){
          const details=document.createElement('details'),toggle=document.createElement('summary'),content=document.createElement('div');
          details.className='wallet-charge';toggle.textContent='本轮计费依据';content.setAttribute('role','status');content.setAttribute('aria-live','polite');details.append(toggle,content);
          details.addEventListener('toggle',async()=>{if(!details.open||details.dataset.loaded)return;details.dataset.loaded='loading';content.textContent='正在读取本轮用量与价格版本…';try{const result=await api('/api/points/requests/'+encodeURIComponent(item.request_id));renderCharge(result,content);details.dataset.loaded='done';}catch(error){content.textContent=error.message||'明细暂时不可用，请关闭后重新展开。';delete details.dataset.loaded;}});
          row.append(details);
        }
        body.append(row);
      }
    }
    function renderCharge(result,container){
      container.textContent='';const price=result.price||{},rates=price.user_rates||{},calls=(Array.isArray(result.calls)?result.calls:[]).filter(call=>call.billable!==false);
      const model=document.createElement('p');model.textContent='模型 '+(price.model||result.model||'待确认')+' · 价格版本 '+(price.id||price.price_id||price.version||'待确认');container.append(model);
      const charge=document.createElement('p');charge.textContent='本轮扣分 '+preciseLabel(result.charged_points??result.charge?.points)+'积分 · '+({usage_pending:'用量待核实',settled:'已结算',refunded:'已返还',failed:'本轮未交付'}[result.state||result.status]||result.state||result.status||'待核实');container.append(charge);
      const table=document.createElement('table');table.className='wallet-usage-table';const head=document.createElement('thead'),header=document.createElement('tr');
      for(const label of ['计费分类','token','元 / 百万token']){const cell=document.createElement('th');cell.scope='col';cell.textContent=label;header.append(cell);}head.append(header);table.append(head);
      const rows=document.createElement('tbody');
      for(const [key,label] of [['input_uncached','未缓存输入'],['cache_read','缓存读取'],['cache_write','缓存写入'],['billable_output','输出']]){
        const known=Array.isArray(result.calls)&&calls.every(call=>Number.isSafeInteger(call.usage?.[key])&&call.usage[key]>=0),count=known?calls.reduce((sum,call)=>sum+call.usage[key],0):null,row=document.createElement('tr');
        for(const value of [label,count===null?'待核实':String(count),rates[key]===undefined?'待确认':String(rates[key])]){const cell=document.createElement('td');cell.textContent=value;row.append(cell);}rows.append(row);
      }
      table.append(rows);container.append(table);const note=document.createElement('p');note.textContent='只展示用户计费价。每轮汇总正常调用后统一换算积分，缓存明细和推理明细不会重复加收；平台自动重试的额外成本不计入用户扣分，扣分不超过确认时的最高额度。';container.append(note);
    }
    async function refreshWallet(details = false) {
      if (!state.ready) return;
      try {const result = await api('/api/points/wallet'); renderWallet(result.wallet || result); if (details) {const ledger = await api('/api/points/ledger?limit=30'); renderDetails(ledger.items || ledger.ledger || []);}}
      catch (error) {$('#walletStatus').textContent = error.message; $('#walletStatus').classList.add('wallet-status-error'); if (details) $('#walletBody').textContent = error.message;}
    }
    function updateQuoteControls() {
      if (state.busy) return;
      const label = state.recoveryBlocking?'正在确认停止':state.pendingRecovery?'查询本轮状态':state.authorizing?'正在开始':state.quoting ? '正在计算费用' : state.pendingQuote ? '确认发送' : '发送';
      $('#sendButton').textContent = state.pendingQuote ? '✓' : '↑'; $('#sendButton').title = label; $('#sendButton').setAttribute('aria-label', label);
    }
    function clearQuote() {state.pendingQuote = null; state.authorizing=false;$('#quoteHint').classList.add('hidden'); updateQuoteControls();}
    function beginAccount() {
      try {sessionStorage.setItem(HANDOFF_KEY, JSON.stringify({thread_id: state.temporary ? null : state.threadId, draft: state.temporary?'':$('#question').value, saved_at: Date.now(), guest_prefix: state.prefix, claimed: false}));} catch (_) {}
      location.href = '/auth?next=/app';
    }
    async function prepareGeneration(payload) {
      if(beforeGeneration&&!await beforeGeneration())return false;
      const signature = JSON.stringify(payload), pending = state.pendingQuote;
      if (pending && pending.signature === signature && pending.expires > Date.now()) {
        Object.assign(payload, {quote_id: pending.quote.quote_id, client_request_id: pending.requestId, operation_id: pending.requestId}); clearQuote();state.authorizing=true;$('#sendButton').disabled=true;updateQuoteControls();return true;
      }
      state.quoting = true; setBusy(false);
      try {
        const requestId = window.JiaoyuanSecurity.requestId();
        const quote = await window.JiaoyuanSecurity.request('/api/points/quote', {method: 'POST', body: JSON.stringify({...payload, client_request_id: requestId, operation_id: requestId})});
        if (quote.wallet) renderWallet(quote.wallet); if (!quote.quote_id||preciseLabel(quote.max_points??quote.maximum_points)==='—') throw new Error('服务没有返回有效报价，请稍后重试。');
        const expires = new Date(typeof quote.expires_at === 'number' ? quote.expires_at * 1000 : quote.expires_at).valueOf();
        state.pendingQuote = {signature, quote, requestId, expires: Number.isFinite(expires) ? expires : Date.now() + 240000};
        $('#quoteSummary').textContent = '本轮最多 ' + preciseLabel(quote.max_points ?? quote.maximum_points) + '积分，按实际消耗结算。请确认发送。'; $('#quoteHint').classList.remove('hidden');

        return false;
      } catch (error) {
        if (error.status === 402) {await refreshWallet(); $('#quoteSummary').textContent = error.message || (state.isGuest ? '访客体验额度已用完，注册后今天可获100积分。' : '本轮所需额度不足，明日00时自动赠送100积分。'); $('#quoteHint').classList.remove('hidden'); $('#creditAction').classList.toggle('hidden', !state.isGuest);}
        showToast(friendlyError(error.message)); return false;
      } finally {state.quoting = false; setBusy(false);}
    }
    $('#walletButton').onclick = () => {closeDrawers(); $('#walletDialog').showModal(); refreshWallet(true);};
    $('#closeWallet').onclick = () => $('#walletDialog').close(); $('#refreshWallet').onclick = () => refreshWallet(true);
    $('#creditAction').onclick = event => {event.preventDefault(); beginAccount();}; $('#quoteCancel').onclick = () => {clearQuote(); $('#question').focus();};
    return {renderWallet, refreshWallet, updateQuoteControls, clearQuote, beginAccount, prepareGeneration};
  }
  window.JiaoyuanCredits = Object.freeze({create, readCache, writeCache, clearCache, pointLabel, preciseLabel, dateTime, handoff, HANDOFF_KEY});
})();
