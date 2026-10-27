#!/usr/bin/env python3
"""Offline, resumable PDF OCR on macOS using Apple Vision. Never uploads files.
Usage: python3 scripts/run_ocr.py [--source-dir /path/to/pdfs] [--workers 8] [--edge 2400] [--sample-only]
Outputs preserve 1-based physical PDF page numbers, including covers and blank pages.
"""
import argparse, hashlib, json, os, subprocess, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
SOURCE_DIR=Path(os.environ.get('MAO_OCR_SOURCE_DIR', str(ROOT/'sources')))
BOOKS=[
('01_selected_works_v1','毛泽东选集 67年版 第一卷(毛泽东） .pdf','毛泽东选集 第一卷'),
('02_selected_works_v2','毛泽东选集 67年版 第二卷 (毛泽东) .pdf','毛泽东选集 第二卷'),
('03_selected_works_v3','毛泽东选集 67年版 第三卷 (毛泽东).pdf','毛泽东选集 第三卷'),
('04_selected_works_v4','毛泽东选集 67年版 第四卷 (毛泽东) .pdf','毛泽东选集 第四卷'),
('05_selected_works_v5','毛泽东选集 77年版 第五卷 (毛泽东).pdf','毛泽东选集 第五卷'),
('06_quotations','毛主席语录 .pdf','毛主席语录')]

def write_outputs(book, expected, phase):
    sid,filename,title=book
    page_dir=ROOT/'tmp/ocr_pages'/sid
    rows=[]
    annotation_file=ROOT/'corpus/page_annotations.json'
    annotations={}
    if annotation_file.exists():
        annotations={(a['source_id'],a['pdf_page']):a for a in json.loads(annotation_file.read_text())['annotations']}
    for page in range(1,expected+1):
        p=page_dir/f'{page:04d}.json'
        if p.exists():
            row=json.loads(p.read_text())
            row.update(source_id=sid,source_file=str(SOURCE_DIR/filename),title=title)
            annotation=annotations.get((sid,page))
            if annotation:
                row['ai_visual_review_status']=annotation['ai_visual_review_status']
                row['ai_visual_review_note']=annotation['note']
                if annotation.get('ai_visual_transcription'):
                    row['ocr_text']=row['text']
                    row['text']=annotation['ai_visual_transcription']
                    row['status']='ai_visual_transcription_unverified'
                    row['method']+='; AI visual handwriting transcription'
            row['proofread']=False
            row['human_review_status']='unreviewed'
            row.setdefault('mean_confidence',None)
            row.setdefault('error',None)
            row.setdefault('raster_longest_edge_pixels',2400)
            row['review_required']=row['status']!='ok' or (row.get('mean_confidence') or 0)<.75
            row['ocr_review_priority']='high' if row['review_required'] else 'normal'
            rows.append(row)
    if len(rows)==expected:phase='complete'
    out=ROOT/'corpus';out.mkdir(exist_ok=True)
    (out/f'{sid}.pages.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows),encoding='utf-8')
    header=f'{title}\n\n说明：本文件为本地 Apple Vision 机器转写，个别书法图页另有 AI 视觉补录（见逐页记录），未经人工逐字校订。按原 PDF 物理页顺序排列，封面、空白页及附加材料均保留。PDF 页码不是书籍印刷页码。原文可能含注释、编者文字或网站附录，不能一律视为作者正文。低置信度和字符坐标见同名 pages.jsonl。\n原始文件：{filename}\n覆盖：{len(rows)}/{expected} 页；处理阶段：{phase}\n\n'
    text=header+''.join(f'\n\n===== PDF 第 {r["pdf_page"]:04d} 页 | {sid}:p{r["pdf_page"]:04d} | {r["status"]} =====\n\n'+(r['text'] if r['text'] else '〔此页未识别到文字；请对照原 PDF 判断是否为空白页或图像页〕') for r in rows)
    (out/f'{sid}.txt').write_text(text,encoding='utf-8')
    return dict(source_id=sid,title=title,source_file=str(SOURCE_DIR/filename),expected_pages=expected,processed_pages=len(rows),characters=sum(len(r['text']) for r in rows),ok_pages=sum(r['status']=='ok' for r in rows),blank_or_unrecognized_pages=[r['pdf_page'] for r in rows if r['status']=='blank_or_unrecognized'],error_pages=[r['pdf_page'] for r in rows if r['status']=='error'],low_confidence_pages=[r['pdf_page'] for r in rows if r.get('mean_confidence') is not None and r['mean_confidence']<.75],text_file=f'{sid}.txt',pages_file=f'{sid}.pages.jsonl',phase=phase,sha256=hashlib.sha256((SOURCE_DIR/filename).read_bytes()).hexdigest())

def manifest(books,status):
    data={'status':status,'created_at':time.strftime('%Y-%m-%dT%H:%M:%S%z'),'method':'Apple Vision accurate offline zh-Hans zh-Hant en-US; CGPDF rasterization; longest edge 2400 px by default; separately marked AI visual transcription for a handwriting page','quality_notice':'Machine OCR, not a proofread edition. Confidence is an OCR model estimate, not character accuracy. Blank_or_unrecognized does not establish an actually blank page. Text includes author text, editorial notes and possible website additions.','page_numbering':'1-based physical PDF page; separate from printed page numbers','review_flags':'Every page is proofread:false and human_review_status:unreviewed. review_required and ocr_review_priority only prioritize OCR checks; false/normal never means reviewed or historically validated.','books':books,'expected_pages':sum(b['expected_pages'] for b in books),'processed_pages':sum(b['processed_pages'] for b in books)}
    data['annotations_file']='page_annotations.json'
    data['comparison_text_layer']={'source_id':'06_quotations','text_file':'06_quotations.embedded-text.txt','pages_file':'06_quotations.embedded-text.pages.jsonl','notice':'Uncorrected embedded PDF text; some headings are garbled. Main transcription is separate.'}
    (ROOT/'corpus/manifest.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')

def _positive_int(value):
    value = int(value)
    if value <= 0:
        raise argparse.ArgumentTypeError('必须是正整数')
    return value


def build_parser():
    ap=argparse.ArgumentParser()
    ap.add_argument('--source-dir',type=Path,default=SOURCE_DIR,help='包含六份原始 PDF 的目录（也可用 MAO_OCR_SOURCE_DIR）')
    ap.add_argument('--workers',type=_positive_int,default=8)
    ap.add_argument('--edge',type=_positive_int,default=2400)
    ap.add_argument('--sample-only',action='store_true')
    return ap


def main(argv=None):
    global SOURCE_DIR
    ap=build_parser();args=ap.parse_args(argv)
    SOURCE_DIR=args.source_dir.expanduser().resolve()
    if not SOURCE_DIR.is_dir():
        ap.error('PDF 源目录不存在：' + str(SOURCE_DIR))
    try:
        from pypdf import PdfReader
    except ImportError:
        ap.error('完整OCR需要可选依赖pypdf；先安装该依赖再运行。')
    binary=ROOT/'scripts/ocr';src=ROOT/'scripts/ocr.swift'
    if not binary.exists() or binary.stat().st_mtime<src.stat().st_mtime:
        subprocess.run(['swiftc','-O',str(src),'-o',str(binary)],check=True)
    records=[]
    for book in BOOKS:
        sid,filename,title=book;path=SOURCE_DIR/filename;n=len(PdfReader(path).pages);dest=ROOT/'tmp/ocr_pages'/sid;dest.mkdir(parents=True,exist_ok=True)
        log=ROOT/'tmp'/f'{sid}.log'
        with log.open('a') as stream:
            for lo,hi in [(1,min(16,n)),(max(17,n-5),n)]:
                subprocess.run([str(binary),str(path),str(dest),str(lo),str(hi),str(args.workers),str(args.edge)],stdout=stream,stderr=stream,check=True)
        records.append(write_outputs(book,n,'front_and_back_sample'))
        manifest(records,'in_progress')
        print(f'FRONT/BACK {sid} {records[-1]["processed_pages"]}/{n}',flush=True)
    if args.sample_only:return
    for i,book in enumerate(BOOKS):
        sid,filename,title=book;n=records[i]['expected_pages'];dest=ROOT/'tmp/ocr_pages'/sid;log=ROOT/'tmp'/f'{sid}.log'
        with log.open('a') as stream:
            subprocess.run([str(binary),str(SOURCE_DIR/filename),str(dest),'1',str(n),str(args.workers),str(args.edge)],stdout=stream,stderr=stream,check=True)
        records[i]=write_outputs(book,n,'complete');manifest(records,'in_progress')
        print(f'COMPLETE {sid} {n} pages {records[i]["characters"]} chars',flush=True)
    manifest(records,'complete' if all(r['processed_pages']==r['expected_pages'] and not r['error_pages'] for r in records) else 'needs_attention')
    qa=['source_id\tpdf_page\tstatus\tmean_confidence\ttext_preview']
    for b in records:
        for line in (ROOT/'corpus'/b['pages_file']).read_text().splitlines():
            row=json.loads(line)
            if row['review_required']:qa.append(f'{b["source_id"]}\t{row["pdf_page"]}\t{row["status"]}\t{row.get("mean_confidence")}\t{row["text"][:60].replace(chr(10)," ")}')
    (ROOT/'corpus/review_queue.tsv').write_text('\n'.join(qa)+'\n',encoding='utf-8')
if __name__=='__main__':main()
