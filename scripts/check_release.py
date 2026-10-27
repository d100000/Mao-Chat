#!/usr/bin/env python3
"""Inspect the actual Git release inventory without printing secret contents."""
from pathlib import Path
from io import BytesIO
import re
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SECRET_PATTERNS = [
    re.compile(rb'\bsk-[A-Za-z0-9_-]{32,}'),
    re.compile(rb'\bfc-[a-fA-F0-9]{24,}'),
    re.compile(rb'\bgh[opusr]_[A-Za-z0-9]{30,}'),
    re.compile(rb'\bgithub_pat_[A-Za-z0-9_]{30,}'),
    re.compile(rb'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----'),
]


def forbidden(path):
    parts = path.parts
    if any('.security-config' in part for part in parts):
        return True
    if path.as_posix() == 'scripts/ocr':
        return True
    if any(p in {'tmp','runtime','node_modules','__pycache__','.venv','.agent-sessions'} for p in parts):
        return True
    if parts and parts[0] == 'sources':
        return True
    if parts[:2] in {('data','sessions'),('data','real_runs'),('tests','runs')}:
        return True
    if path.name == 'config.json' or (path.name.startswith('.env') and path.name != '.env.example'):
        return True
    return bool(re.search(r'\.(?:db|sqlite)(?:-wal|-shm)?$|\.(?:auth-secret|provider-secrets|session-secret|admin-totp)(?:\..*)?$|\.zip$',path.name))


def contains_secret(data):
    return any(pattern.search(data) for pattern in SECRET_PATTERNS)


def contains_private_path(data):
    return bool(re.search(rb'/(?:Users|home)/[^/\s\"<>]+/', data) or
                re.search(rb'[A-Za-z]:\\Users\\[^\\\s\"<>]+\\', data))


def inspect(root=ROOT):
    result = subprocess.run(['git','ls-files','--stage','-z'],cwd=root,capture_output=True,check=True)
    files = []
    for item in result.stdout.split(b'\0'):
        if item:
            metadata, path = item.split(b'\t', 1)
            mode, revision, stage = metadata.split()
            files.append((Path(path.decode()), mode, revision))
    if not files:
        return ['Git发布清单为空，请先暂存准备发布的文件。'], 0
    findings = []
    for relative, mode, revision in files:
        if forbidden(relative):
            findings.append(str(relative) + ': 私有或生成状态不得发布')
            continue
        if mode == b'120000':
            findings.append(str(relative) + ': 请使用实际、可审阅的文件而非本机符号链接')
            continue
        data = subprocess.run(['git','cat-file','blob',revision.decode()],cwd=root,capture_output=True,check=True).stdout
        if len(data) > 20 * 1024 * 1024:
            findings.append(str(relative) + ': 超过源码发布体积上限')
            continue
        if relative.suffix == '.docx':
            with zipfile.ZipFile(BytesIO(data)) as z:
                data = b'\n'.join(z.read(n) for n in z.namelist() if n.endswith('.xml'))
        if contains_private_path(data):
            findings.append(str(relative) + ': 检出本机用户目录；请使用相对仓库路径')
        if contains_secret(data):
            findings.append(str(relative) + ': 检出疑似密钥或私钥；未输出内容')
    return findings, len(files)


if __name__ == '__main__':
    findings, count = inspect()
    for item in findings: print(item)
    print('release inventory:', count, 'files;', len(findings), 'findings')
    raise SystemExit(bool(findings))
