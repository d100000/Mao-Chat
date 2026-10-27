#!/usr/bin/env python3
"""Preserve the quotations PDF's existing embedded text as a separate comparison layer.
The embedded layer has garbled headings; it is not a corrected or authoritative text.
"""
import json
from pathlib import Path
from pypdf import PdfReader
from run_ocr import ROOT,SOURCE_DIR,BOOKS
sid,filename,title=BOOKS[-1]
r=PdfReader(SOURCE_DIR/filename)
rows=[dict(source_id=sid,source_file=str(SOURCE_DIR/filename),pdf_page=i+1,text=page.extract_text() or '',proofread=False,human_review_status='unreviewed',method='pypdf embedded PDF text; uncorrected; some headings garbled') for i,page in enumerate(r.pages)]
(ROOT/'corpus'/f'{sid}.embedded-text.pages.jsonl').write_text(''.join(json.dumps(p,ensure_ascii=False)+'\n' for p in rows),encoding='utf-8')
intro='毛主席语录 PDF 原有文字层对照\n\n此文件直接提取 PDF 已有文字层，保留乱码和缺字，未作人工校订；部分标题乱码，另有 Apple Vision OCR 版本可对照。PDF页码是1基物理页码。后附网站编辑材料同样按原文件保留，不代表均为作者正文。\n'
(ROOT/'corpus'/f'{sid}.embedded-text.txt').write_text(intro+''.join(f'\n\n===== PDF 第 {p["pdf_page"]:04d} 页 | {sid}:p{p["pdf_page"]:04d} =====\n\n'+p['text'] for p in rows),encoding='utf-8')
print(f'Preserved {len(rows)} pages; {sum(len(p["text"]) for p in rows)} embedded characters')
