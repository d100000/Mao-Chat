#!/usr/bin/env python3
"""Local CJK bigram retrieval; strict historical eligibility is fail-closed."""
import argparse
from collections import Counter
from datetime import date
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile

DEFAULT_ROOT = Path(__file__).resolve().parents[3]

def tokens(text):
    out = []
    for run in re.findall(r'[\u3400-\u9fff]+|[A-Za-z0-9]+', text.lower()):
        if re.fullmatch(r'[\u3400-\u9fff]+', run):
            out.extend(run[i:i+2] for i in range(len(run)-1))
            if len(run) == 1:
                out.append(run)
        else:
            out.append(run)
    return out

def read_cards(root):
    p = root / 'skills/mao-primary-sources/references/verified-evidence.jsonl'
    if not p.exists(): p = root / 'skills/mao-primary-sources/references/evidence.jsonl'
    return [json.loads(line) for line in p.read_text(encoding='utf-8').splitlines() if line.strip()] if p.exists() else []

def strict_eligible(card, cutoff):
    if card.get('speaker') != '毛泽东' or card.get('quote_verified') is not True:
        return False
    if card.get('source_role') not in ('author_body', 'author_excerpt'):
        return False
    # Every date is independently required. Unknown is not equivalent to ancient.
    required = ('original_date_upper', 'wording_verified_as_of', 'known_to_character_from')
    try:
        if any(not card.get(key) or date.fromisoformat(card[key]) > cutoff for key in required):
            return False
    except (ValueError, TypeError):
        return False
    return card.get('historical_eligibility_reviewed') is True

def build(root):
    paths = sorted(p for p in (root / 'corpus').glob('*.pages.jsonl') if '.embedded-text.' not in p.name)
    if not paths:
        raise ValueError('未找到逐页JSONL；请先完成 scripts/run_ocr.py。')
    db = root / 'corpus/search.sqlite'
    handle = tempfile.NamedTemporaryFile(prefix='search-building-', suffix='.sqlite', dir=db.parent, delete=False)
    staging = Path(handle.name)
    handle.close()
    conn = None
    count = 0
    navpath = root / 'corpus/article_navigation.json'
    articles = json.loads(navpath.read_text(encoding='utf-8'))['articles'] if navpath.exists() else []
    try:
        conn = sqlite3.connect(staging)
        conn.executescript('CREATE TABLE pages(id INTEGER PRIMARY KEY, anchor TEXT UNIQUE, source_id TEXT, pdf_page INTEGER, text TEXT, metadata TEXT); CREATE VIRTUAL TABLE search USING fts5(terms);')
        for path in paths:
            for line in path.read_text(encoding='utf-8').splitlines():
                if not line.strip(): continue
                d = json.loads(line)
                text = d.get('text', '')
                source = d.get('source_id') or path.name.split('.')[0]
                page = int(d['pdf_page'])
                anchor = f'{source}:p{page:04d}'
                meta = {k: v for k, v in d.items() if k not in ('text', 'lines')}
                meta['article_candidates'] = [a['title'] for a in articles if a['source_id']==source and a['pdf_page_start']<=page<=a['pdf_page_end']]
                cur = conn.execute('INSERT INTO pages(anchor,source_id,pdf_page,text,metadata) VALUES(?,?,?,?,?)', (anchor,source,page,text,json.dumps(meta,ensure_ascii=False)))
                conn.execute('INSERT INTO search(rowid,terms) VALUES(?,?)', (cur.lastrowid, ' '.join(tokens(text))))
                count += 1
        manifest = root / 'corpus/manifest.json'
        if manifest.exists():
            m = json.loads(manifest.read_text(encoding='utf-8'))
            if m.get('status')=='complete' and count != m['expected_pages']:
                raise ValueError('索引页数与完整清单不符；旧索引保留。')
        conn.commit()
        conn.close()
        conn = None
        staging.replace(db)
    finally:
        if conn is not None: conn.close()
        staging.unlink(missing_ok=True)
    return {'pages_indexed': count, 'database': str(db), 'status': '机器转写全文索引；篇名范围仅为候选导航，页内仍可能有注释。'}

def search(root, query, limit, mode, as_of):
    query_tokens = list(dict.fromkeys(tokens(query)))[:100]
    if not query_tokens:
        return {'mode': mode, 'results': [], 'notice': '请输入可检索的中文词组或英文词。'}
    if mode == 'historical':
        import persona
        pool, results = persona.retrieve(query, 'historical', as_of, limit, 'paraphrase')
        return {'mode':mode,'as_of':as_of,'eligible_cards':len(pool),'results':results,'notice':'按核验内容日期过滤后排序；后编文本只支持标识的内容转述。'}
    if mode == 'strict' and (Path(__file__).resolve().parents[1] / 'references/verified-evidence.jsonl').exists():
        import persona
        pool, results = persona.retrieve(query, 'historical', as_of, limit, 'quote')
        return {'mode':mode,'as_of':as_of,'eligible_cards':len(pool),'results':results,'notice':'逐字引文按最早可核措辞日期过滤；不等于原始录音还原。'}
    cards = read_cards(root)
    if mode == 'strict':
        if not as_of:
            raise ValueError('严格历史模式必须给出 --as-of YYYY-MM-DD。')
        cutoff = date.fromisoformat(as_of)
        eligible = [c for c in cards if strict_eligible(c, cutoff)]
        scored = []
        for card in eligible:
            haystack = ' '.join(str(card.get(k,'')) for k in ('topic','summary','excerpt','article_title'))
            ct = Counter(tokens(haystack))
            score = sum(min(ct[t], 3) for t in query_tokens)
            if score:
                scored.append((score,card))
        scored.sort(key=lambda x: (-x[0], x[1]['id']))
        return {'mode':mode, 'as_of':as_of, 'eligible_cards':len(eligible), 'results':[c for _,c in scored[:limit]], 'notice':'仅返回已核准在截止日期前为角色可知的作者证据；无结果不表示历史上不存在相关材料。'}
    db = root / 'corpus/search.sqlite'
    if not db.exists():
        raise ValueError('索引未建立；先运行 build。')
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    match = ' OR '.join('"'+t.replace('"','""')+'"' for t in query_tokens)
    rows = conn.execute('SELECT p.*, bm25(search) AS rank FROM search JOIN pages p ON p.id=search.rowid WHERE search MATCH ? ORDER BY rank LIMIT ?', (match,limit)).fetchall()
    results = []
    for row in rows:
        text = row['text']
        lines = text.splitlines()
        best = max(range(len(lines)),key=lambda i: sum(t in lines[i] for t in query_tokens)) if lines else 0
        excerpt = '\n'.join(lines[max(0,best-2):best+8])[:1300]
        meta = json.loads(row['metadata'])
        results.append({'anchor':row['anchor'],'source_id':row['source_id'],'pdf_page':row['pdf_page'],'excerpt':excerpt,'page_text':text,'metadata':meta,'evidence_cards':[c['id'] for c in cards if c.get('source_id') == row['source_id'] and row['pdf_page'] in c.get('pdf_pages',[])]})
    conn.close()
    return {'mode':mode,'results':results,'notice':'研究模式查全部转写页，可能包含目录、前言、编者注与网站附录。结果不是已确认的本人原话；请回看PDF及前后页。'}

def get_page(root, anchor, context):
    match = re.fullmatch(r'([A-Za-z0-9_]+):p([0-9]{1,6})', anchor)
    if not match or not 0 <= context <= 3:
        raise ValueError('页锚格式应为 source_id:p0304；--context须为0至3。')
    source, page = match[1], int(match[2])
    db = root / 'corpus/search.sqlite'
    if not db.exists():
        raise ValueError('索引未建立；先运行 build。')
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    exact = conn.execute('SELECT id FROM pages WHERE source_id=? AND pdf_page=?',(source,page)).fetchone()
    if exact is None:
        conn.close()
        raise ValueError('该页不在当前索引中；请核对页锚或重建完整索引。')
    rows = conn.execute('SELECT * FROM pages WHERE source_id=? AND pdf_page BETWEEN ? AND ? ORDER BY pdf_page',(source,max(1,page-context),page+context)).fetchall()
    result = [dict(anchor=r['anchor'],pdf_page=r['pdf_page'],text=r['text'],metadata=json.loads(r['metadata'])) for r in rows]
    conn.close()
    return {'pages':result,'notice':'前后页可能跨篇或包含注释；直接引文仍须对照PDF图像。'}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=DEFAULT_ROOT)
    sub = parser.add_subparsers(dest='command',required=True)
    sub.add_parser('build')
    page_parser = sub.add_parser('page')
    page_parser.add_argument('anchor')
    page_parser.add_argument('--context',type=int,default=0)
    p = sub.add_parser('search')
    p.add_argument('query')
    p.add_argument('--limit',type=int,default=5)
    p.add_argument('--mode',choices=['research','historical','strict'],default='research')
    p.add_argument('--as-of')
    args = parser.parse_args()
    try:
        if args.command == 'build':
            result = build(args.root)
        elif args.command == 'page':
            result = get_page(args.root,args.anchor,args.context)
        else:
            if not 1 <= args.limit <= 20:
                raise ValueError('--limit须在1至20之间。')
            result = search(args.root,args.query,args.limit,args.mode,args.as_of)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    except (ValueError,sqlite3.Error) as exc:
        print(json.dumps({'error':str(exc)},ensure_ascii=False),file=sys.stderr)
        sys.exit(2)

if __name__ == '__main__':
    main()
