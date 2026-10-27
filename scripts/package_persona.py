#!/usr/bin/env python3
"""Package text sources, the installed skill, and unchanged test records."""
from pathlib import Path
import json,zipfile
ROOT=Path(__file__).resolve().parents[1]
output=ROOT/'毛泽东人物skill可用版.zip'
SAFE_DATA_FILES={'verified_early.jsonl','verified_late.jsonl','verified_timeline.jsonl','quote_event_links.jsonl'}
EXCLUDED_DIRS={'sessions','real_runs','runs','dialogue_runs','.agent-sessions','__pycache__','.git'}
SENSITIVE_SUFFIXES=('.db','.db-wal','.db-shm','.auth-secret','.provider-secrets','.admin-totp')


def is_safe_file(path: Path, root: Path = ROOT, *, data_only: bool = False) -> bool:
    """Keep runtime state and credentials out of distributable archives."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    if any(part in EXCLUDED_DIRS or '.security-config' in part for part in relative.parts):
        return False
    if path.name == 'config.json' or (path.name.startswith('.env') and path.name != '.env.example'):
        return False
    if any(part in path.name for part in ('.auth-secret', '.provider-secrets', '.session-secret', '.admin-totp')):
        return False
    if path.name.endswith(SENSITIVE_SUFFIXES):
        return False
    if data_only and path.name not in SAFE_DATA_FILES:
        return False
    return path.is_file() and path.suffix != '.pyc'


def collect_files(root: Path = ROOT):
    files={root/n for n in ['可用版本说明.md','人物对话方案.md','使用说明.txt']}
    for name in ['corpus','research','skills/mao-primary-sources','data','tests','scripts','qa_samples']:
        for p in (root/name).rglob('*'):
            if p.name == '.DS_Store' or p.name == 'ocr':
                continue
            if is_safe_file(p, root, data_only=name == 'data'):
                files.add(p)
    evidence_path = root/'skills/mao-primary-sources/references/verified-evidence.jsonl'
    if evidence_path.exists():
        for line in evidence_path.read_text(encoding='utf-8').splitlines():
            for f in json.loads(line).get('verification_images',[]):
                p=root/f
                if p.is_file():files.add(p)
    return files


def build_archive(root: Path = ROOT, archive: Path = output):
    files = collect_files(root)
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for p in sorted(files):z.write(p,p.relative_to(root))
    with zipfile.ZipFile(archive) as z:
        if z.testzip() is not None:raise ValueError('Archive integrity failed')
    return {'archive':str(archive),'files':len(files),'bytes':archive.stat().st_size}


if __name__ == '__main__':
    print(json.dumps(build_archive(),ensure_ascii=False,indent=2))
