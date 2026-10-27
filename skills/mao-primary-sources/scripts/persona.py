#!/usr/bin/env python3
"""Prepare dated evidence, preserve conversation cutoff, and check source/quote use.
This tool prepares an agent's context; it does not call or emulate a language model.
"""
import argparse
from collections import Counter
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import uuid

SKILL = Path(__file__).resolve().parents[1]
ROOT = Path(__file__).resolve().parents[3]
DATA = SKILL / 'references/verified-evidence.jsonl'
ALLOWED_ROLES = {'author_body', 'author_excerpt', 'author_speech'}
MODES = ('historical', 'research', 'modern')

def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    tmp.replace(path)

def normalize(s):
    return re.sub(r'\s+', '', s).translate(str.maketrans({'“':'"','”':'"','‘':"'",'’':"'"}))

def cjk_tokens(s):
    result=[]
    for word in re.findall(r'[\u3400-\u9fff]+|[a-zA-Z0-9]+',s.lower()):
        if re.fullmatch(r'[\u3400-\u9fff]+',word):
            result.extend(word[i:i+2] for i in range(len(word)-1))
            if len(word)==1:result.append(word)
        else:result.append(word)
    return result

def digest(card):
    content={k:v for k,v in card.items() if k!='integrity_sha256'}
    return hashlib.sha256(json.dumps(content,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def load_cards():
    if not DATA.exists():
        raise ValueError('核验资料尚未生成：'+str(DATA))
    cards=[json.loads(line) for line in DATA.read_text(encoding='utf-8').splitlines() if line.strip()]
    seen=set()
    for c in cards:
        if c['id'] in seen:raise ValueError('重复证据ID：'+c['id'])
        seen.add(c['id'])
        if c.get('integrity_sha256') != digest(c):raise ValueError('证据记录已改变，请重新核验：'+c['id'])
    return cards

def valid_day(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):return None
    try:return date.fromisoformat(value)
    except ValueError:return None

def available(card, cutoff, use='paraphrase'):
    """Eligibility is specific to a claim, not a whole article or page."""
    if card.get('reviewed') is not True:return False
    biography = card.get('source_role')=='biographical_record'
    if biography:
        if use=='quote' or card.get('actor')!='毛泽东' or card.get('public_event_verified') is not True:return False
    elif card.get('speaker')!='毛泽东' or card.get('source_role') not in ALLOWED_ROLES:return False
    if use=='quote':
        if card.get('quote_verified') is not True:return False
        since=valid_day(card.get('earliest_wording_date'))
    else:
        if card.get('content_time_verified') is not True:return False
        since=valid_day(card.get('earliest_content_date'))
    if since is None or since>cutoff:return False
    # Unknown or later knowledge date must not be filled with an event date.
    known=valid_day(card.get('known_to_character_from'))
    return known is not None and known<=cutoff

def score(card, question):
    query=set(cjk_tokens(question))
    hay=' '.join([card.get('topic') or '',card.get('article_title') or '',card.get('summary') or '',card.get('excerpt') or '',' '.join(x for x in (card.get('topics') or []) if isinstance(x,str))])
    terms=Counter(cjk_tokens(hay))
    s=sum(min(terms[t],2) for t in query)
    # Exact thematic labels help short Chinese queries; no facts are inferred here.
    for label in [card.get('topic',''),card.get('article_title',''),*(card.get('topics') or [])]:
        if label and label in question:s+=8
    return s

def retrieve(question, mode, as_of, limit=6, use='paraphrase'):
    cards=load_cards()
    cutoff=valid_day(as_of) if as_of else None
    if mode=='historical' and cutoff is None:raise ValueError('历史模拟必须指定 --as-of YYYY-MM-DD。')
    if cutoff and not date(1893,12,26)<=cutoff<=date(1976,9,9):
        raise ValueError('角色截止日期须在人物生年与卒年范围内；现代问题请用 modern 模式。')
    if mode=='historical':
        pool=[c for c in cards if available(c,cutoff,use)]
    else:pool=cards
    ranked=sorted(((score(c,question),c) for c in pool),key=lambda x:(-x[0],x[1]['id']))
    selected=[dict(c,retrieval_score=s) for s,c in ranked if s>0][:limit]
    # A nonmatching query must not silently produce generic slogans.
    return pool,selected

def check_session(session):
    if session.get('mode') not in MODES or not isinstance(session.get('turns'),list):raise ValueError('会话结构不正确。')
    if session['mode']=='historical' and valid_day(session.get('as_of')) is None:raise ValueError('历史会话缺少有效截止日期。')
    return session

def create_session(path,mode,as_of):
    if Path(path).exists():raise ValueError('会话文件已存在；用原文件继续或换新文件名。')
    if mode=='historical' and valid_day(as_of) is None:raise ValueError('历史模拟必须给出明确截止日期。')
    if as_of and (not valid_day(as_of) or not date(1893,12,26)<=valid_day(as_of)<=date(1976,9,9)):
        raise ValueError('角色日期超出人物生卒范围。')
    data={'schema_version':2,'mode':mode,'as_of':as_of,'created_at':datetime.now(timezone.utc).isoformat(),'turns':[]}
    write_json(path,data)
    return data

def prepare(question, mode, as_of, session_path=None, limit=6, query=None):
    session=check_session(read_json(session_path)) if session_path else None
    if session:
        if mode and mode!=session['mode']:raise ValueError('会话模式已固定；切换模式请新建会话，防止记忆串入。')
        if as_of and as_of!=session['as_of']:raise ValueError('会话截止日期已固定；改变日期请新建会话。')
        mode,as_of=session['mode'],session['as_of']
    mode=mode or 'research'
    pool,selected=retrieve(query or question,mode,as_of,limit)
    cutoff=valid_day(as_of) if as_of else None
    future_years=sorted({int(y) for y in re.findall(r'(?<!\d)(1[89]\d{2}|20\d{2})(?!\d)',question) if cutoff and int(y)>cutoff.year})
    profile = None
    profile_path = SKILL / 'references/periods.json'
    if cutoff and profile_path.exists():
        choices = read_json(profile_path)['profiles']
        matched = [x for x in choices if date.fromisoformat(x['from'])<=cutoff<=date.fromisoformat(x['until'])]
        if matched:
            profile = {k:v for k,v in matched[-1].items() if k not in ('from','until')}
    evidence=[]
    for c in selected:
        out={k:v for k,v in c.items() if k not in ('integrity_sha256','verification_artifacts')}
        quote_ok=available(c,cutoff,'quote') if cutoff else c.get('quote_verified') is True
        if mode=='historical':
            out={k:out.get(k) for k in ('id','topic','article_title','source_id','pdf_pages','original_date_display','summary','source_role','speaker','actor','content_qualification','topics','source_urls')}
            out['source_urls']=[{'url':x['url'],'title':x['title']} for x in c.get('source_urls',[])]
            out['excerpt']=c.get('excerpt','') if quote_ok else ''
            out['provenance_note']='机构编年只支持公开经历；不支持私下回忆或本人原话。' if c.get('source_role')=='biographical_record' else '依据核验过的具体摘要作回溯转述；参考链接/版本属于研究者的出处信息，不等于角色当时读过该网页或后编本。'
        out['quote_eligible_at_role_date']=quote_ok
        evidence.append(out)
    return {
        'mode':mode,'as_of':as_of,'question':question,'retrieval_query':query or question,'eligible_evidence_count':len(pool),
        'future_years_in_question':future_years,'period_reading_guidance':profile,'evidence':evidence,
        'conversation':session['turns'][-8:] if session else [],
        'answer_contract':{
            'identity':'历史人物模拟（回溯重构），不是本人原话或真实亲历答问' if mode=='historical' else '史料解释' if mode=='research' else '当代方法类比',
            'content':'历史模式只用返回证据中获准的具体摘要，不将整篇后编原文或会话内容变成当时知识。',
            'wording':'只有quote_eligible_at_role_date为真时才能把证据excerpt用作角色当时逐字原话；其余转述。研究模式可引用现存版本，但注明版本。',
            'citations':'每个史料判断引证据ID，末尾列篇名、原作/发表日期及所给PDF页。生成句不加原话引号。',
            'missing':'无相关证据时说明缺口；可描述下一步所需材料，不能编造经历或推知未来。',
            'judgment':'认识方法、政治目标与组织条件一并说明，保留跨阶段差异。公开规范不证明所有实践效果。',
        },
        'draft_schema':{'answer':'自然语言答复；可含【证据ID】','claims':[{'text':'答复中的史料主张或直接引语','kind':'paraphrase|quote|inference','evidence_ids':['证据ID']}]} }

def check_draft(draft,mode,as_of,allowed_ids=None):
    cards={c['id']:c for c in load_cards()}
    cutoff=valid_day(as_of) if as_of else None
    if mode=='historical' and cutoff is None:raise ValueError('检查历史回答须给截止日期。')
    errors=[];warnings=[]
    if not isinstance(draft,dict) or not isinstance(draft.get('answer'),str) or not isinstance(draft.get('claims'),list):
        return {'passed':False,'errors':['答复须含answer字符串与claims列表。'],'warnings':[]}
    if not draft['claims'] and re.search(r'\w',draft['answer']):warnings.append('没有可机检的主张；缺证回答可为空，其余需补claims。')
    allowed=set(allowed_ids) if allowed_ids is not None else None
    cited=set()
    for i,claim in enumerate(draft['claims'],1):
        if not isinstance(claim,dict):errors.append(f'主张{i}格式错误。');continue
        kind=claim.get('kind');ids=claim.get('evidence_ids');txt=claim.get('text')
        if kind not in ('paraphrase','quote','inference') or not isinstance(txt,str) or not txt.strip():
            errors.append(f'主张{i}类型/文本无效。');continue
        if normalize(txt) not in normalize(draft['answer']):errors.append(f'主张{i}未出现在答复中。')
        if not isinstance(ids,list) or not ids or not all(isinstance(x,str) for x in ids):
            errors.append(f'主张{i}缺少有效出处。');continue
        known=[]
        for eid in ids:
            cited.add(eid)
            if eid not in cards:errors.append(f'主张{i}引用未知证据{eid}。');continue
            c=cards[eid];known.append(c)
            if allowed is not None and eid not in allowed:errors.append(f'主张{i}引用了本轮检索包之外的证据{eid}。')
            if mode=='historical' and not available(c,cutoff,'quote' if kind=='quote' else 'paraphrase'):
                errors.append(f'主张{i}的{eid}不具备该日期的{kind}资格。')
        if kind=='quote' and known:
            if not any(c.get('quote_verified') is True and normalize(txt) in normalize(c.get('excerpt','')) for c in known):
                errors.append(f'主张{i}的直接引语与核对文本不匹配。')
        if kind=='inference':warnings.append(f'主张{i}为推断，须在答复中明确表述为推断/类比。')
    markers=set(re.findall(r'【([A-Za-z][A-Za-z0-9_-]+)】',draft['answer']))
    for eid in markers:
        if eid not in cards:errors.append('答复含未知引注：'+eid)
        elif eid not in cited:errors.append('可见引注未列入主张：'+eid)
    for eid in cited:
        if f'【{eid}】' not in draft['answer']:errors.append('主张缺少可见引注：'+eid)
    if cutoff:
        years=sorted({int(y) for y in re.findall(r'(?<!\d)(1[89]\d{2}|20\d{2})(?!\d)',re.sub(r'https?://[^\s)]+','',draft['answer'])) if int(y)>cutoff.year})
        if years:warnings.append('答复提及截止点以后的年份，需确认仅用于拒绝未来问题或研究说明：'+str(years))
    return {'passed':not errors,'errors':errors,'warnings':warnings,
        'scope':'只检查证据ID、日期资格、引用文本及可见引注；不自动证明转述蕴含、哲学判断或所有事实的正确性。'}

def log_attempt(directory, draft, result, mode, as_of, packet=None):
    directory=Path(directory)/'attempts'
    directory.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    path=directory/(stamp+'-'+uuid.uuid4().hex[:8]+'.json')
    entry={'at_utc':datetime.now(timezone.utc).isoformat(),'mode':mode,'as_of':as_of,
        'question':packet.get('question') if packet else None,
        'retrieval_query':packet.get('retrieval_query') if packet else None,
        'draft':draft,'check':result,'evidence_sha256':hashlib.sha256(DATA.read_bytes()).hexdigest()}
    with path.open('x',encoding='utf-8') as stream:
        stream.write(json.dumps(entry,ensure_ascii=False,indent=2)+'\n')
    return str(path)

def record(session_path, draft, packet=None):
    session=check_session(read_json(session_path))
    if packet:
        if packet.get('mode')!=session['mode'] or packet.get('as_of')!=session['as_of']:raise ValueError('检索包与会话日期/模式不一致。')
    result=check_draft(draft,session['mode'],session['as_of'],[c['id'] for c in packet['evidence']] if packet else None)
    attempt=log_attempt(Path(session_path).parent,draft,result,session['mode'],session['as_of'],packet)
    if not result['passed']:return {'saved':False,'attempt_file':attempt,**result}
    if packet is None:raise ValueError('保存回合须提供本轮--packet，以保留用户问题与来源。')
    session['turns'].append({'user':packet['question'],'retrieval_query':packet.get('retrieval_query',packet['question']),'assistant':draft['answer'],'evidence_ids':sorted({eid for c in draft['claims'] for eid in c['evidence_ids']}),'note':'会话记忆不属于历史事实库'})
    write_json(session_path,session)
    return {'saved':True,'turn_count':len(session['turns']),'attempt_file':attempt,**result}

def audit():
    cards=load_cards();errors=[]
    manifest=read_json(ROOT/'corpus/manifest.json') if (ROOT/'corpus/manifest.json').exists() else None
    sources={c['source_id']:c for c in manifest['books']} if manifest else {}
    for c in cards:
        if c.get('reviewed') is not True:errors.append(c['id']+':缺核验标志')
        if not isinstance(c.get('summary'),str) or not c['summary'].strip():errors.append(c['id']+':摘要缺失或类型错误')
        if not c.get('source_urls') or any(not x.get('url','').startswith('https://') or not x.get('supports') for x in c.get('source_urls',[])):
            errors.append(c['id']+':缺可追溯外部出处及支持范围')
        if c.get('content_time_verified') is True and (valid_day(c.get('earliest_content_date')) is None or not c.get('content_time_basis')):
            errors.append(c['id']+':内容日期缺少依据')
        if c.get('earliest_wording_date') is not None and (c.get('quote_verified') is not True or not c.get('wording_time_basis')):
            errors.append(c['id']+':措辞日期缺依据')
        if manifest:
            src=sources.get(c.get('source_id'))
            if c.get('source_role')=='biographical_record' and c.get('source_id') is None:
                continue
            if not src or c.get('pdf_sha256')!=src['sha256']:errors.append(c['id']+':底本指纹不匹配')
            elif not all(isinstance(p,int) and 1<=p<=src['expected_pages'] for p in c.get('pdf_pages',[])):errors.append(c['id']+':PDF页无效')
    return {'passed':not errors,'cards':len(cards),'errors':errors,'qualification':'字段、日期格式、底本指纹及记录完整性检查；不替代史料学审查。'}

def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    init=sub.add_parser('init');init.add_argument('--session',required=True,type=Path);init.add_argument('--mode',choices=MODES,default='research');init.add_argument('--as-of')
    prep=sub.add_parser('prepare');prep.add_argument('question');prep.add_argument('--mode',choices=MODES);prep.add_argument('--as-of');prep.add_argument('--session',type=Path);prep.add_argument('--limit',type=int,default=6);prep.add_argument('--out',type=Path);prep.add_argument('--query',help='独立检索词；不改用户原问题和会话记录')
    chk=sub.add_parser('check');chk.add_argument('--draft',required=True,type=Path);chk.add_argument('--mode',choices=MODES,default='research');chk.add_argument('--as-of');chk.add_argument('--packet',type=Path)
    rec=sub.add_parser('record');rec.add_argument('--draft',required=True,type=Path);rec.add_argument('--packet',required=True,type=Path);rec.add_argument('--session',required=True,type=Path)
    sub.add_parser('audit')
    args=p.parse_args()
    try:
        if args.command=='init':result=create_session(args.session,args.mode,args.as_of)
        elif args.command=='prepare':
            if not 1<=args.limit<=12:raise ValueError('--limit须在1至12之间。')
            result=prepare(args.question,args.mode,args.as_of,args.session,args.limit,args.query)
            if args.out:write_json(args.out,result)
        elif args.command=='check':
            packet=read_json(args.packet) if args.packet else None
            if packet and (packet.get('mode')!=args.mode or packet.get('as_of')!=args.as_of):raise ValueError('检索包与检查参数不一致。')
            draft=read_json(args.draft)
            result=check_draft(draft,args.mode,args.as_of,[c['id'] for c in packet['evidence']] if packet else None)
            result['attempt_file']=log_attempt(args.draft.parent,draft,result,args.mode,args.as_of,packet)
        elif args.command=='record':result=record(args.session,read_json(args.draft),read_json(args.packet))
        else:result=audit()
        print(json.dumps(result,ensure_ascii=False,indent=2))
        if result.get('passed') is False or result.get('saved') is False:sys.exit(1)
    except (ValueError,OSError,KeyError,json.JSONDecodeError) as exc:
        print(json.dumps({'error':str(exc)},ensure_ascii=False),file=sys.stderr);sys.exit(2)

if __name__=='__main__':main()
