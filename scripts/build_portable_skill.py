#!/usr/bin/env python3
"""Build a portable full-sources delivery; preserve the original project."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / '毛泽东史料对话Skill完整包'
SKILL = BUNDLE / 'mao-primary-sources'
VERSION = '2.1.0'
SAFE_DATA_FILES = {'verified_early.jsonl', 'verified_late.jsonl', 'verified_timeline.jsonl', 'quote_event_links.jsonl'}
EXCLUDED_DIRS = {'sessions', 'real_runs', 'runs', 'dialogue_runs','.agent-sessions', '__pycache__', '.git'}
SENSITIVE_SUFFIXES = ('.db', '.db-wal', '.db-shm', '.auth-secret', '.provider-secrets')


def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def is_safe_file(path, *, data_only=False):
    """Return whether a file is safe to include in a portable bundle."""
    if any(part in EXCLUDED_DIRS for part in path.parts):
        return False
    if path.name == 'config.json' or (path.name.startswith('.env') and path.name != '.env.example'):
        return False
    if any(part in path.name for part in ('.auth-secret', '.provider-secrets', '.session-secret')):
        return False
    if path.name.endswith(SENSITIVE_SUFFIXES) or path.name in {'ocr', '.DS_Store'}:
        return False
    if data_only and path.name not in SAFE_DATA_FILES:
        return False
    return path.is_file() and path.suffix != '.pyc'


def copy_tree(source, target, *, data_only=False):
    """Copy source files while excluding credentials and runtime state."""
    source = Path(source)
    target = Path(target)
    target.mkdir(parents=True, exist_ok=False)
    for path in source.rglob('*'):
        if not is_safe_file(path, data_only=data_only):
            continue
        destination = target / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)


def replace_once(path, old, new):
    text = path.read_text(encoding='utf-8')
    if text.count(old) != 1:
        raise ValueError('Portable patch no longer matches: ' + str(path))
    path.write_text(text.replace(old, new, 1), encoding='utf-8')


def build():
    if SKILL.exists():
        raise ValueError('完整技能目录已存在；保留已有内容，不自动覆盖。')
    BUNDLE.mkdir(exist_ok=True)
    copy_tree(ROOT / 'skills/mao-primary-sources', SKILL)
    for name in ('corpus', 'data', 'research', 'tests', 'qa_samples'):
        copy_tree(ROOT / name, SKILL / name, data_only=name == 'data')
    cards = [json.loads(line) for line in (SKILL / 'references/verified-evidence.jsonl').read_text(encoding='utf-8').splitlines()]
    for card in cards:
        for rel in card.get('verification_images', []):
            source = ROOT / rel
            if not source.is_file():
                raise ValueError('核验图像缺失：' + rel)
            destination = SKILL / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    corpus_path = SKILL / 'corpus/manifest.json'
    corpus = json.loads(corpus_path.read_text(encoding='utf-8'))
    sources = SKILL / 'sources'
    sources.mkdir()
    mapping = {}
    for book in corpus['books']:
        original = Path(book['source_file'])
        rel = 'sources/' + book['source_id'] + '.pdf'
        if sha256(original) != book['sha256']:
            raise ValueError('原始PDF指纹变化：' + str(original))
        shutil.copy2(original, SKILL / rel)
        mapping[book['source_file']] = rel
        book['source_original_filename'] = original.name
        book['source_file'] = rel
    corpus['portable_metadata_note'] = '仅将source_file路径改为相对技能目录；六份PDF与OCR文本保持原内容。全文索引由同一转写页重新建立。'
    corpus_path.write_text(json.dumps(corpus, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    for path in (SKILL / 'corpus').glob('*.pages.jsonl'):
        rows = []
        for line in path.read_text(encoding='utf-8').splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get('source_file') in mapping:
                row['source_file'] = mapping[row['source_file']]
            rows.append(row)
        path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows), encoding='utf-8')
    replace_once(SKILL / 'scripts/persona.py',
                 "ROOT = Path(__file__).resolve().parents[3]",
                 "ROOT = SKILL if (SKILL / 'corpus').is_dir() else Path(__file__).resolve().parents[3]")
    replace_once(SKILL / 'scripts/retrieve.py',
                 'DEFAULT_ROOT = Path(__file__).resolve().parents[3]',
                 "SKILL = Path(__file__).resolve().parents[1]\nDEFAULT_ROOT = SKILL if (SKILL / 'corpus').is_dir() else Path(__file__).resolve().parents[3]")
    replace_once(SKILL / 'scripts/retrieve.py',
                 "    p = root / 'skills/mao-primary-sources/references/verified-evidence.jsonl'\n    if not p.exists(): p = root / 'skills/mao-primary-sources/references/evidence.jsonl'",
                 "    candidates = [root / 'references/verified-evidence.jsonl', root / 'skills/mao-primary-sources/references/verified-evidence.jsonl', root / 'references/evidence.jsonl', root / 'skills/mao-primary-sources/references/evidence.jsonl']\n    p = next((p for p in candidates if p.is_file()), candidates[0])")
    replace_once(SKILL / 'tests/test_persona.py',
                 "SCRIPTS=ROOT/'skills/mao-primary-sources/scripts'", "SCRIPTS=ROOT/'scripts'")
    replace_once(SKILL / 'SKILL.md',
                 '核验库范围有限。研究模式需深入上下文时，可用项目全文工具`retrieve.py search/page`，但其结果含OCR、编者注与目录，仅为候选。项目根目录由脚本定位，也可用`retrieve.py --root <语料根目录>`。查询未见不等于历史不存在。',
                 '核验库范围有限。研究模式需深入上下文时，可用本技能`scripts/retrieve.py search/page`；结果含OCR、编者注与目录，仅为候选。完整包的语料根目录就是本技能目录，已含`corpus/`和六份原始PDF的`sources/`，不依赖原项目。脚本自动定位，也可用`retrieve.py --root <语料根目录>`。按`corpus/manifest.json`中的相对`source_file`定位PDF，`pdf_page`为从1起的物理PDF页。查询未见不等于历史不存在。')
    replace_once(SKILL / 'references/evidence-rules.md',
                 '原始批次及核验报告保存在项目`data/`和`research/`',
                 '原始批次及核验报告保存在本技能`data/`和`research/`')
    # Rebuild from the relocated JSONL so SQLite metadata has portable source paths too.
    subprocess.run([sys.executable, str(SKILL / 'scripts/retrieve.py'), 'build'], check=True)
    print(json.dumps({'built': str(SKILL), 'version': VERSION}, ensure_ascii=False))


def finalize():
    ignored = {'package-manifest.json', '.installation.json'}
    files = [p for p in SKILL.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.name not in ignored and p.suffix != '.pyc']
    manifest = {'package_id': 'mao-primary-sources', 'package_version': VERSION,
                'created_date': '2026-10-25', 'files': [
                    {'path': p.relative_to(SKILL).as_posix(), 'bytes': p.stat().st_size, 'sha256': sha256(p)} for p in sorted(files)]}
    (SKILL / 'package-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (BUNDLE / 'VERSION').write_text(VERSION + '\n', encoding='utf-8')
    # Skip disposable interpreter caches in every package and archive.
    for cache in BUNDLE.rglob('__pycache__'):
        shutil.rmtree(cache)
    top = [p for p in BUNDLE.rglob('*') if p.is_file() and p.name != 'SHA256SUMS.txt']
    (BUNDLE / 'SHA256SUMS.txt').write_text(''.join(sha256(p) + '  ' + p.relative_to(BUNDLE).as_posix() + '\n' for p in sorted(top)), encoding='utf-8')
    output = ROOT / (BUNDLE.name + '.zip')
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(p for p in BUNDLE.rglob('*') if p.is_file()):
            archive.write(path, Path(BUNDLE.name) / path.relative_to(BUNDLE))
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise ValueError('压缩包CRC检查失败。')
    output.with_suffix('.zip.sha256').write_text(sha256(output) + '  ' + output.name + '\n', encoding='utf-8')
    print(json.dumps({'folder': str(BUNDLE), 'archive': str(output), 'files': len(top) + 1,
                      'uncompressed_bytes': sum(p.stat().st_size for p in BUNDLE.rglob('*') if p.is_file()),
                      'archive_bytes': output.stat().st_size}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--finalize', action='store_true')
    args = parser.parse_args()
    finalize() if args.finalize else build()
