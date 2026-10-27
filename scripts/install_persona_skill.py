#!/usr/bin/env python3
"""Register the reviewed project skill via a reversible symlink; never overwrite another skill."""
from pathlib import Path
import json
ROOT=Path(__file__).resolve().parents[1]
source=ROOT/'skills/mao-primary-sources'
destination=Path.home()/'.codex/skills/mao-primary-sources'
if not (source/'SKILL.md').is_file():raise SystemExit('SKILL.md missing')
if destination.is_symlink():
    if destination.resolve()!=source.resolve():raise SystemExit('Existing skill points elsewhere; preserved.')
elif destination.exists():raise SystemExit('Existing skill directory preserved; no overwrite.')
else:
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination.symlink_to(source,target_is_directory=True)
print(json.dumps({'installed':True,'path':str(destination),'source':str(source),'mechanism':'symlink; project remains canonical'},ensure_ascii=False,indent=2))
