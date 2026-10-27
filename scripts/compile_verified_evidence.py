#!/usr/bin/env python3
"""Compile the specifically reviewed evidence batches; never invent wording dates."""
from pathlib import Path
import importlib.util,json
ROOT=Path(__file__).resolve().parents[1]
P=ROOT/'skills/mao-primary-sources/scripts/persona.py'
spec=importlib.util.spec_from_file_location('persona',P);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
manifest=json.loads((ROOT/'corpus/manifest.json').read_text());sources={x['source_id']:x for x in manifest['books']}
rows=[]
for name in ['verified_early','verified_late','verified_timeline']:
    for line in (ROOT/'data'/f'{name}.jsonl').read_text(encoding='utf-8').splitlines():
        if not line.strip():continue
        c=json.loads(line)
        if not c.get('source_urls'):raise ValueError('Missing citations: '+c['id'])
        if c.get('source_role')=='biographical_record':
            c['summary']=c.get('historical_paraphrase') or c['summary']
            c['actor']=c['subject'];c['public_event_verified']=c.get('direct_participation_supported') is True
            c['content_time_verified']=c['public_event_verified']
            c['earliest_content_date']=c['event_date_upper']
            c['earliest_wording_date']=None
            c['article_title']=c['topic']
            c['content_time_basis']=c['direct_participation_basis']
            c['content_qualification']='public_event_retrospective_paraphrase' if c['public_event_verified'] else 'research_only_version_history'
            c['original_date_display']=c['event_date_lower'] if c['event_date_lower']==c['event_date_upper'] else c['event_date_lower']+'至'+c['event_date_upper']
            c['original_date_lower']=c['event_date_lower'];c['original_date_upper']=c['event_date_upper']
            anchors=c.get('local_context_anchors',[])
            c['source_id']=anchors[0].split(':p')[0] if anchors else None
            c['pdf_pages']=[int(x.split(':p')[1]) for x in anchors]
            if c['id']=='TL06':
                c['research_only_meeting_range']='1949年3月5日至13日；不将闭幕事实提前输入3月5日角色。'
                c['summary']='1949年3月5日，毛泽东在西柏坡召开的中共七届二中全会上作报告。'
        else:
            c['content_time_verified']=True
            if c.get('known_to_character_from') is None:
                c['known_to_character_from']=c['earliest_content_date']
                c['known_time_basis']='原作/讲话的署名者为本人；此卡只对已核具体内容作有标识回溯转述，以原作结束日或有据的发表日作可知上界，不对后编全文放行。'
            else:c['known_time_basis']='本人原作/讲话的具体内容；修订发表材料按已核发表文本日期保守处理。'
            if c.get('earliest_wording_date'):
                c['wording_time_basis']=c.get('review_notes') or c['content_time_basis']
        c['reviewed']=True
        c['review_scope']='AI-assisted source/date verification, limited to this summary and selected excerpt; not human historical certification'
        c['pdf_sha256']=sources[c['source_id']]['sha256'] if c.get('source_id') else None
        c['integrity_sha256']=m.digest(c)
        rows.append(c)
if len(rows)!=37 or len({x['id'] for x in rows})!=37:raise ValueError('Unexpected batch count')
out=ROOT/'skills/mao-primary-sources/references/verified-evidence.jsonl'
out.write_text(''.join(json.dumps(c,ensure_ascii=False)+'\n' for c in rows),encoding='utf-8')
print('Compiled',len(rows),'evidence records')
print(json.dumps(m.audit(),ensure_ascii=False,indent=2))
