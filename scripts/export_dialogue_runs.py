#!/usr/bin/env python3
"""Snapshot original test records without rewriting them and render readable transcripts."""
from pathlib import Path
import hashlib,json,shutil
ROOT=Path(__file__).resolve().parents[1]
source=ROOT/'tmp/dialogue_runs';target=ROOT/'tests/runs'
scenario_data=json.loads((ROOT/'tests/dialogue_scenarios.json').read_text())
questions={s['id']:s['prompts'] for s in scenario_data['scenarios']}
for p in source.rglob('*'):
    if not p.is_file():continue
    dest=target/p.relative_to(source);dest.parent.mkdir(parents=True,exist_ok=True)
    if dest.exists() and dest.read_bytes()!=p.read_bytes():raise ValueError('Snapshot already differs; preserved: '+str(dest))
    if not dest.exists():shutil.copy2(p,dest)
transcripts=[]
for run in ['round1','round2','round3']:
    for scenario in sorted((source/run).glob('D*')):
        for answer in sorted(scenario.glob('turn*-answer.txt')):
            number=int(answer.name.split('-')[0][4:]);packet=scenario/f'turn{number}-packet.json'
            c=json.loads(packet.read_text());txt=answer.read_text()
            transcripts.append(dict(run=run,scenario=scenario.name,turn=number,original_user_prompt=questions[scenario.name][number-1],recorded_question=c['question'],retrieval_query=c.get('retrieval_query',c['question']),answer=txt,raw_path=str((target/answer.relative_to(source)).relative_to(ROOT))))
(target/'transcripts.json').write_text(json.dumps(transcripts,ensure_ascii=False,indent=2))
text='# 人物技能实际对话原文\n\n初轮24个连续回合与后续定向复测分别保留。检索包和代码检查不能替代史料与思想质量评审；完整评分见同目录的独立评审。原始用户问题按实际投递题目列出，同时保留工具中当时记录的问题，便于看到并修复输入污染。\n'
for t in transcripts:
    text+=f'\n## {t["run"]} {t["scenario"]} 第{t["turn"]}轮\n\n用户：{t["original_user_prompt"]}\n\n'
    if t['recorded_question']!=t['original_user_prompt']:text+='原工具记录（已观察到的输入污染，未回写）：'+t['recorded_question']+'\n\n'
    text+=t['answer'].strip()+'\n\n[原始记录](../'+t['raw_path']+')\n'
(ROOT/'research/v2_dialogue_transcripts.md').write_text(text)
print('Exported',len(transcripts),'actual responses')
