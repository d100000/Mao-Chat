/* Qualified source reader. Selection is an ID/version/paragraph tuple, never HTML. */
(() => {
  let options, selection = null, token = 0, opener, scroll = 0;
  const positions = new Map();
  const dialog = document.createElement('dialog');
  dialog.id = 'sourceReader'; dialog.className = 'source-reader'; dialog.setAttribute('aria-labelledby','readerTitle');
  dialog.innerHTML = '<header class="reader-head"><h2 id="readerTitle">阅读依据</h2><button type="button" class="message-action" id="closeReader">返回对话</button></header><p id="readerStatus" role="status" aria-live="polite"></p><div id="readerBody"></div>';
  document.body.append(dialog);
  const $ = id => document.getElementById(id);
  let currentKey = '';
  function close() { token++; dialog.close(); }
  $('closeReader').onclick=close;
  dialog.addEventListener('cancel',()=>{token++;});
  dialog.addEventListener('close',()=>{
    if(currentKey) positions.set(currentKey,dialog.scrollTop);
    const chat=$('chatScroll'); if(chat)chat.scrollTop=scroll;
    opener?.focus({preventScroll:true});
  });
  function clear(){selection=null;const box=$('readingSelection');if(box){box.hidden=true;box.textContent='';}options?.changed?.();}
  function choose(source,part){
    selection={source_id:source.source_id,version:source.version,paragraph_id:part.id};
    const box=$('readingSelection');box.textContent='';box.hidden=false;
    const label=document.createElement('span');label.textContent='围绕选段 · '+source.title+' · '+part.label;
    const button=document.createElement('button');button.type='button';button.className='message-action';button.textContent='清除选段';button.onclick=clear;
    box.append(label,button);options?.changed?.();close();$('question')?.focus();
  }
  async function open(id,version){
    if(!options)return;
    const request=++token;opener=document.activeElement;scroll=$('chatScroll')?.scrollTop||0;
    currentKey='';$('readerTitle').textContent='阅读依据';$('readerStatus').textContent='正在读取当前核验版本…';$('readerBody').textContent='';
    if(!dialog.open)dialog.showModal();
    try{
      const {source}=await options.api('/api/agent/reader?id='+encodeURIComponent(id)+(version?'&version='+encodeURIComponent(version):''));
      if(request!==token||!dialog.open)return;
      currentKey=source.source_id+':'+source.version;
      $('readerTitle').textContent=source.title;
      $('readerStatus').textContent=source.scope+'。'+source.notice;
      const versionLabel=document.createElement('p');versionLabel.className='source-meta';versionLabel.textContent=source.source_id+' · 版本 '+source.version.slice(0,12);$('readerBody').append(versionLabel);
      for(const part of source.paragraphs){
        const section=document.createElement('section');section.className='reader-paragraph';section.dataset.paragraphId=part.id;
        const title=document.createElement('h3');title.textContent=part.label;
        const text=document.createElement('p');text.textContent=part.text;
        const button=document.createElement('button');button.type='button';button.className='message-action';button.textContent='围绕这段提问';button.disabled=!options.canSelect();
        button.onclick=()=>{if(options.canSelect())choose(source,part);};section.append(title,text,button);$('readerBody').append(section);
      }
      if(!options.canSelect()){const p=document.createElement('p');p.textContent='可先阅读；切回对话解惑且当前请求结束后，可围绕选段提问。';$('readerBody').append(p);}
      for(const id of source.supporting_ids||[]){const button=document.createElement('button');button.type='button';button.className='message-action';button.textContent='查看支撑原著 '+id;button.onclick=()=>open(id);$('readerBody').append(button);}
      for(const url of source.source_urls||[]){const parsed=new URL(url);if(!['http:','https:'].includes(parsed.protocol))continue;const a=document.createElement('a');a.href=url;a.target='_blank';a.rel='noopener noreferrer';a.className='source-link';a.textContent='公开来源 ↗';$('readerBody').append(a);}
      dialog.scrollTop=positions.get(currentKey)||0;
    }catch(error){if(request!==token)return;$('readerStatus').textContent=error.message||'资料暂时不可用，请重试。';const button=document.createElement('button');button.type='button';button.className='message-action';button.textContent=version?'读取当前版本':'重新读取';button.onclick=()=>open(id);$('readerBody').append(button);}
  }
  window.JiaoyuanReader={configure:value=>{options=value;},open,clear,selection:()=>selection?{...selection}:null};
})();
