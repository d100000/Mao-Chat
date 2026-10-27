#!/usr/bin/env python3
"""Verify page coverage and provenance; this does not prove OCR character accuracy."""
import hashlib,json,statistics
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
root=Path(__file__).resolve().parents[1]
m=json.loads((root/'corpus/manifest.json').read_text())
assert m['status']=='complete',m['status']
total=0;books=[]
for b in m['books']:
    p=root/'corpus'/b['pages_file'];rows=[json.loads(l) for l in p.read_text().splitlines()]
    assert [r['pdf_page'] for r in rows]==list(range(1,b['expected_pages']+1)),b['source_id']
    assert all(r['source_id']==b['source_id'] for r in rows)
    assert all(r['proofread'] is False and r['human_review_status']=='unreviewed' for r in rows)
    assert all(r['status']!='error' for r in rows),b['source_id']
    source_path=Path(b['source_file'])
    if not source_path.is_absolute(): source_path=root/source_path
    assert b['sha256']==hashlib.sha256(source_path.read_bytes()).hexdigest(),b['source_id']
    text=(root/'corpus'/b['text_file']).read_text()
    for row in rows:assert f'{b["source_id"]}:p{row["pdf_page"]:04d}' in text
    assert sum(len(r['text']) for r in rows)==b['characters']
    total+=len(rows)
    confidences=[r['mean_confidence'] for r in rows if r.get('mean_confidence') is not None]
    books.append(dict(source_id=b['source_id'],pages=len(rows),characters=b['characters'],status_counts=dict(Counter(r['status'] for r in rows)),low_confidence_pages=[r['pdf_page'] for r in rows if r.get('mean_confidence') is not None and r['mean_confidence']<.75],mean_model_confidence=statistics.mean(confidences),median_model_confidence=statistics.median(confidences),ai_visual_review_pages=[r['pdf_page'] for r in rows if r.get('ai_visual_review_status')],sha256_verified=True,page_sequence_verified=True,all_page_anchors_verified=True,all_pages_marked_unproofread=True))
    print(f'{b["source_id"]}: {len(rows)} pages, {b["characters"]} characters, hashes/anchors/coverage OK')
assert total==m['processed_pages']==m['expected_pages']
report=dict(result='pass',verified_at=datetime.now(timezone.utc).isoformat(),physical_pages=total,characters=sum(b['characters'] for b in books),books=books,checks=['consecutive physical PDF page coverage','source PDF SHA256','source IDs','TXT page anchors','JSONL character totals','all pages explicitly unproofread','no OCR execution errors'],limitation='These checks establish file and page integrity only. OCR model confidence is not character accuracy; no page is human proofread. AI visual transcription/review is separately marked, and historical authorship/source classification needs editorial verification.')
(root/'corpus/verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(f'PASS: {total} physical pages. OCR accuracy still requires source-image review.')
