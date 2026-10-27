/* Small-batch editorial workflow. No model can grant source eligibility. */
(() => {
  const $=id=>document.getElementById(id), root=$('knowledge');if(!root)return;
  let busy=false, draft=null, preview=null, latest=null, rollbackArmed=false;
  const status=(text,error=false)=>{$('knowledgeStatus').textContent=text;$('knowledgeStatus').classList.toggle('error',error);};
  async function api(path,data){const response=await fetch(path,{method:data?'POST':'GET',credentials:'same-origin',headers:data?{'Content-Type':'application/json'}:{},body:data?JSON.stringify({...data,otp_code:$('knowledgeOtp').value.trim()}):undefined});const result=await response.json();if(!response.ok||result.ok===false)throw new Error(result.error||'操作未完成');return result;}
  function controls(){root.querySelectorAll('button').forEach(b=>b.disabled=busy||b.dataset.published==='true');$('knowledgeReview').disabled=busy||!draft;$('knowledgePublish').disabled=busy||!preview||preview.status!=='reviewed';$('knowledgeRollback').disabled=busy||!latest;}
  async function run(work){if(busy)return;busy=true;controls();try{await work();}catch(error){status(error.message,true);}finally{busy=false;controls();}}
  function paragraph(parent,text){const p=document.createElement('p');p.textContent=text;parent.append(p);}
  async function load(){
    const data=await api('/api/admin/knowledge');const coverage=data.coverage||{};
    $('knowledgeCoverage').textContent='已核 '+coverage.reviewed+' · 候选 '+coverage.candidate+' · 来源 '+coverage.sources+' · 缺年代 '+coverage.undated+'。'+coverage.full_text;
    $('knowledgeRevision').textContent='资料版本 '+data.revision.slice(0,12)+'；语义通道：'+data.semantic.reason;
    $('knowledgeIds').textContent='';for(const card of data.cards){const option=document.createElement('option');option.value=card.evidence_id;option.label=card.article_title;$('knowledgeIds').append(option);}
    const list=$('knowledgeDrafts');list.textContent='';for(const row of data.drafts){const b=document.createElement('button');b.type='button';b.className='btn small';b.textContent=row.source_id+' · '+({candidate:'候选',reviewed:'待发布',published:'已发布'}[row.status]||row.status);b.onclick=()=>run(async()=>{const p=await api('/api/admin/knowledge/preview?id='+encodeURIComponent(row.id));draft=row.id;renderPreview(p);status('已载入草稿，请核对内容、范围和资格。');});if(row.status==='published'){b.disabled=true;b.dataset.published='true';}list.append(b);}
    latest=data.releases[0]||null;rollbackArmed=false;$('knowledgeRollback').textContent='预览回退最近发布';
    const releases=$('knowledgeReleases');releases.textContent='';for(const r of data.releases.slice(0,5))paragraph(releases,(r.rollback_of?'回退':'发布')+' · '+r.created_at+' · '+r.after_revision.slice(0,12)+' · 阅读与检索同版');
    if(!data.releases.length)paragraph(releases,'尚无编辑器发布记录；当前资料库保持可用。');
  }
  function renderPreview(value){preview=value;const box=$('knowledgePreview');box.textContent='';box.hidden=false;
    paragraph(box,'草稿 '+value.draft_id+' · '+(value.status==='reviewed'?'已人工核验，待发布':'候选，尚未授予资格'));
    const table=document.createElement('table');table.className='knowledge-diff';const tbody=document.createElement('tbody');
    for(const [key,label]of [['article_title','篇名'],['summary','摘要'],['excerpt','原文片段'],['earliest_content_date','内容日期'],['earliest_wording_date','措辞日期'],['known_to_character_from','可获知日期']]){const tr=document.createElement('tr');for(const text of [label,value.before?.[key]||'（空）',value.after[key]||'（空）']){const td=document.createElement('td');td.textContent=text;tr.append(td);}tbody.append(tr);}
    const head=document.createElement('thead');const tr=document.createElement('tr');for(const text of ['字段','当前版本','草稿']){const th=document.createElement('th');th.textContent=text;tr.append(th);}head.append(tr);table.append(head,tbody);box.append(table);
    paragraph(box,'资格：'+(value.after.reviewed?'已核摘要':'候选')+'；逐字引用：'+(value.after.quote_verified?'仅已核片段':'不具备')+'；范围：'+(value.after.review_scope||'待填写'));
    paragraph(box,'需检查重复：'+(value.duplicates.join('、')||'没有同篇名/同摘要记录')+'。受影响解释：'+(value.affected_interpretations.join('、')||'无')+'；事件：'+(value.affected_events.join('、')||'无'));
    paragraph(box,value.notice);
  }
  $('knowledgeRefresh').onclick=()=>run(async()=>{await load();status('已读取最新资料状态。');});
  $('knowledgeLoad').onclick=()=>run(async()=>{const row=await api('/api/admin/knowledge/card?id='+encodeURIComponent($('knowledgeSource').value.trim()));for(const [id,key]of Object.entries({knowledgeTitle:'article_title',knowledgeSummary:'summary',knowledgeExcerpt:'excerpt',knowledgeContentDate:'earliest_content_date',knowledgeWordingDate:'earliest_wording_date',knowledgeKnownDate:'known_to_character_from'}))$(id).value=row[key]||'';$('knowledgeUrls').value=(row.source_urls||[]).join('\n');draft=null;preview=null;$('knowledgePreview').hidden=true;status('已载入当前内容。保存为候选后，原版本仍保持生效。');});
  $('knowledgeForm').onsubmit=event=>{event.preventDefault();run(async()=>{const patch={article_title:$('knowledgeTitle').value.trim(),summary:$('knowledgeSummary').value.trim(),excerpt:$('knowledgeExcerpt').value.trim(),source_urls:$('knowledgeUrls').value.split(/\n/).map(x=>x.trim()).filter(Boolean),earliest_content_date:$('knowledgeContentDate').value||null,earliest_wording_date:$('knowledgeWordingDate').value||null,known_to_character_from:$('knowledgeKnownDate').value||null};const created=await api('/api/admin/knowledge/draft',{source_id:$('knowledgeSource').value.trim(),patch});draft=created.draft_id;preview=null;$('knowledgeApproved').checked=false;$('knowledgeQuote').checked=false;$('knowledgeSourceChecked').checked=false;renderPreview(await api('/api/admin/knowledge/preview?id='+draft));await load();status('候选已保存；当前引用与资料未改变。请人工核对来源后填写下面的核验范围。');});};
  $('knowledgeReview').onclick=()=>run(async()=>{await api('/api/admin/knowledge/review',{draft_id:draft,approved:$('knowledgeApproved').checked,quote_verified:$('knowledgeQuote').checked,source_checked:$('knowledgeSourceChecked').checked,scope:$('knowledgeScope').value,reason:$('knowledgeReason').value});renderPreview(await api('/api/admin/knowledge/preview?id='+draft));status('核验已记录。请检查前后差异，点击确认发布才会生效。');});
  $('knowledgePublish').onclick=()=>run(async()=>{await api('/api/admin/knowledge/publish',{draft_id:draft,preview_token:preview.preview_token});draft=null;preview=null;$('knowledgePreview').hidden=true;await load();status('资料、索引和阅读版本已同时发布；旧引用若版本改变会明确提示重新读取。');});
  $('knowledgeRollback').onclick=()=>run(async()=>{if(!rollbackArmed){rollbackArmed=true;$('knowledgeRollback').textContent='确认回退最近发布';status('将从 '+latest.after_revision.slice(0,12)+' 回退到 '+latest.before_revision.slice(0,12)+'。这会恢复当时的内容和资格，请再次确认。');return;}await api('/api/admin/knowledge/rollback',{release_id:latest.id,expected_revision:latest.after_revision});await load();status('已回退，资料与索引仍为同一版本。');});
  $('knowledgeForm').addEventListener('input',()=>{draft=null;preview=null;$('knowledgePreview').hidden=true;controls();});
  for(const id of ['knowledgeSourceChecked','knowledgeApproved','knowledgeQuote','knowledgeScope','knowledgeReason'])$(id).addEventListener('input',()=>{preview=null;controls();});
  run(load);
})();
