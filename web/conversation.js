/* Public progress, stable paragraphs, and source-backed inline explanations. */
(() => {
  const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text)n.textContent=text;if(cls)n.className=cls;return n;};
  const stages={understand:'理解处境',scope:'资料范围',local:'查找依据',web:'核实外部信息',relate:'联系当前问题',verify:'核对出处',review:'组织回答与核对引用'};
  let options, follow=true, popupOwner=null, pinned=false, closeTimer, suppressedFocus=null;
  const cards=new WeakMap(), turns=new WeakMap();
  const popup=el('section',null,'knowledge-popup');popup.id='knowledgePopup';popup.hidden=true;popup.setAttribute('aria-label','知识说明');popup.setAttribute('role','dialog');document.body.append(popup);
  function close(){popup.hidden=true;pinned=false;if(popupOwner){popupOwner.setAttribute('aria-expanded','false');popupOwner=null;}}
  function dismissLater(){clearTimeout(closeTimer);closeTimer=setTimeout(()=>{if(!pinned&&!popup.matches(':hover')&&!popup.contains(document.activeElement))close();},180);}
  popup.addEventListener('mouseenter',()=>clearTimeout(closeTimer));popup.addEventListener('mouseleave',dismissLater);
  document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!popup.hidden){const owner=popupOwner;close();suppressedFocus=owner;owner?.focus({preventScroll:true});}});
  document.addEventListener('pointerdown',e=>{if(!popup.hidden&&!popup.contains(e.target)&&!popupOwner?.contains(e.target))close();});
  function field(label,value){if(!value||(Array.isArray(value)&&!value.length))return;popup.append(el('h4',label),el('p',Array.isArray(value)?value.join('；'):String(value)));}
  function show(button,pin=false){
    const data=cards.get(button);if(!data)return;
    clearTimeout(closeTimer);if(popupOwner&&popupOwner!==button)popupOwner.setAttribute('aria-expanded','false');
    popupOwner=button;pinned=pin;button.setAttribute('aria-expanded','true');popup.replaceChildren();
    const {row,context}=data;const title=row.label||row.title||row.article_title||row.topic||'出处与说明';
    const x=el('button','关闭说明','message-action');x.type='button';x.onclick=()=>{close();suppressedFocus=button;button.focus({preventScroll:true});};popup.append(x,el('h3',title));
    const kind=row.record_type;
    popup.append(el('p',kind==='principle_note'||kind==='method_note'?'方法概括 · 研究者解释，不是历史原话':kind==='historical_case'||kind==='historical_event'?'历史背景 · 以收录资料范围为限':'来源摘要 · 引语资格以资料卡为准','source-meta'));
    field('简要解释',row.research_interpretation||row.decision_rule||row.summary||row.description);
    field('原始背景',row.historical_conditions||row.context||[row.article_title,row.earliest_wording_date].filter(Boolean).join(' · ')||row.source_locator);
    if(['historical_case','historical_event'].includes(kind)){field('已核事件事实',row.facts);field('当时的处境',row.problem_context);field('当时的行动',row.actions||row.action||'当前资料未单独收录行动过程。');field('已知结果',row.outcome||row.result||(row.outcome_evidence?.length?row.outcome_evidence:'未提供独立结果证据'));}
    if(['historical_case','historical_event'].includes(kind))field('资料缺口',row.unknowns||row.evidence_gap||row.limitations||'未提供独立核验的结果资料，不能据此认定行动成功。');
    field('本轮关联 · 回答片段，属于现代分析',context.trim().slice(0,500));
    field('可借鉴的用法 · 方法概括',row.modern_application||row.modern_analogy);
    field('适用边界',row.unsupported_extensions||row.non_analogies||row.boundary||row.conditions||(/AI-assisted source/.test(row.review_scope||'')?'核对范围仅限当前摘要和选定片段，不代表人工完成历史鉴定或整篇核验。':row.review_scope)||'所处时代与现实条件可能不同；资料不能保证今天的结果。');
    const ids=[...new Set([options.sourceId(row),...(row.evidence_ids||row.supporting_evidence_ids||[])].filter(Boolean))];
    for(const id of ids.slice(0,5)){const b=el('button','查看依据 '+id,'message-action');b.type='button';b.onclick=()=>{close();options.focusSource(id);};popup.append(b);}
    popup.hidden=false;
    if(matchMedia('(min-width: 681px)').matches){const r=button.getBoundingClientRect();popup.style.left=Math.max(12,Math.min(r.left,innerWidth-popup.offsetWidth-12))+'px';popup.style.top=Math.max(12,Math.min(r.bottom+8,innerHeight-popup.offsetHeight-12))+'px';}else{popup.style.left='';popup.style.top='';}
    if(pin)x.focus({preventScroll:true});
  }
  function decorate(button,row,context){if(cards.has(button))return;cards.set(button,{row,context});button.setAttribute('aria-haspopup','dialog');button.setAttribute('aria-controls',popup.id);button.setAttribute('aria-expanded','false');button.addEventListener('mouseenter',()=>{if(matchMedia('(hover:hover)').matches)show(button);});button.addEventListener('focus',()=>{if(suppressedFocus===button){suppressedFocus=null;return;}show(button);});button.addEventListener('blur',dismissLater);button.addEventListener('mouseleave',dismissLater);button.onclick=()=>show(button,true);}
  const aliases={'principle:practice-and-revision':['实践检验','实践与认识的相互检验'],'principle:investigate-actual-conditions':['调查实际情况','调查研究'],'principle:concrete-contradictions':['具体问题具体分析']};
  function hydrate(root,rows){
    for(const button of root.querySelectorAll('.citation-link')){const id=button.dataset.ref;const row=rows.find(x=>options.sourceKeys(x).includes(id));if(row)decorate(button,row,root.textContent);}
    const notes=rows.filter(x=>['principle_note','method_note','historical_case'].includes(x.record_type));
    for(const row of notes){const names=[row.label,row.title,...(aliases[row.principle_id]||[])].filter(x=>typeof x==='string'&&x.length>=3);const ids=[options.sourceId(row),...(row.evidence_ids||row.supporting_evidence_ids||[])];
      if(!ids.some(id=>root.textContent.includes('【'+id)))continue;
      const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT,{acceptNode:n=>n.parentElement.closest('button,a,code')?NodeFilter.FILTER_REJECT:NodeFilter.FILTER_ACCEPT});const nodes=[];while(walker.nextNode())nodes.push(walker.currentNode);
      for(const node of nodes){const name=names.find(x=>node.textContent.includes(x));if(!name)continue;const text=node.textContent,at=text.indexOf(name),b=el('button',name,'knowledge-term');b.type='button';decorate(b,row,root.textContent);node.replaceWith(document.createTextNode(text.slice(0,at)),b,document.createTextNode(text.slice(at+name.length)));break;}
    }
  }
  function create(bubble){bubble.replaceChildren();const row=bubble.closest('.message');const box=el('details',null,'turn-progress'),summary=el('summary','正在理解处境'),live=el('span','','progress-live'),log=el('ol'),controls=el('div',null,'progress-controls');live.setAttribute('role','status');summary.append(live);box.append(summary,log);bubble.before(box,controls);const body=el('div',null,'answer-paragraphs');bubble.append(body);const value={bubble,row,box,summary,live,log,controls,body,text:'',tail:'',sources:[],finished:false};turns.set(bubble,value);return value;}
  function status(turn,event){if(turn.finished)return;const text=event.text||'';if(!text||turn.log.lastElementChild?.textContent===text)return;const item=el('li',text);turn.log.append(item);if(turn.log.children.length>30)turn.log.firstChild.remove();turn.summary.firstChild.textContent=(stages[event.stage]||'分析进展')+' · ';turn.live.textContent=text;}
  function paragraph(turn,text){if(!text)return;const part=el('div',null,'answer-paragraph');part.innerHTML=options.formatAnswer(text.replace(/\n\s*\n$/,''));options.hydrateCitations(part);hydrate(part,turn.sources);turn.body.append(part);}
  function append(turn,text){if(text.trim())status(turn,{stage:'review',text:'已收到可阅读段落；后续内容仍在处理。'});turn.text+=text;turn.tail+=text;let match;while((match=/\n\s*\n/.exec(turn.tail))){const end=match.index+match[0].length;paragraph(turn,turn.tail.slice(0,end));turn.tail=turn.tail.slice(end);}turn.row.dataset.raw=turn.text;scroll();}
  function sources(turn,rows){turn.sources=rows.map(x=>({...x}));for(const part of turn.body.children)hydrate(part,turn.sources);}
  function finish(turn,complete=true){if(turn.finished)return;paragraph(turn,turn.tail);turn.tail='';turn.finished=true;turn.box.open=false;turn.summary.firstChild.textContent=complete?'本轮分析记录 · ':'本轮未完成 · ';turn.live.textContent=complete?'已结束，可展开查看依据查找过程':'已停止推进，保留已收到的内容';turn.row.classList.toggle('incomplete',!complete);}
  function scroll(force=false){const chat=document.getElementById('chatScroll');if(force)follow=true;requestAnimationFrame(()=>{if(follow)chat.scrollTop=chat.scrollHeight;document.getElementById('newContent').hidden=follow;});}
  function configure(value){options=value;const chat=document.getElementById('chatScroll');const b=el('button','有新内容，回到底部','new-content');b.id='newContent';b.type='button';b.hidden=true;b.onclick=()=>scroll(true);chat.after(b);const position=()=>{const r=chat.getBoundingClientRect();b.style.left=(r.left+r.width/2)+'px';b.style.bottom=Math.max(12,innerHeight-r.bottom+12)+'px';};new ResizeObserver(position).observe(chat);window.addEventListener('resize',position);position();chat.addEventListener('scroll',()=>{follow=chat.scrollHeight-chat.scrollTop-chat.clientHeight<80;if(follow)b.hidden=true;},{passive:true});}
  window.JiaoyuanConversation={configure,create,status,append,sources,finish,scroll,hydrate,turn:b=>turns.get(b),close};
})();
