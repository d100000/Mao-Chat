#!/usr/bin/env python3
"""Offline, frozen-corpus retrieval comparison. Labels are editorial, not blind review."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.bootstrap_library import bootstrap
from agent.dialogue import CuratedMethodLibrary
from agent.principles import search_principles
from agent.db import connect
from agent.semantic_experiment import LocalSemanticExperiment, semantic_diagnostics


def evaluate(semantic_model_dir=None):
    fixture = ROOT / 'agent/benchmarks/mrmao/retrieval.json'
    cases = json.loads(fixture.read_text())
    with tempfile.TemporaryDirectory(prefix='mrmao-benchmark-') as directory:
        db = Path(directory) / 'library.db'
        counts = bootstrap(db)
        conn = connect(db)
        ids = CuratedMethodLibrary._reviewed_author_ids(conn, {'evidence_cards'})
        conn.close()
        result = {'labels':'editorial synthetic; independent human review pending',
                  'fixture_sha256':hashlib.sha256(fixture.read_bytes()).hexdigest(),
                  'corpus_counts':{k:counts[k] for k in ('evidence_cards','quotes','events') if k in counts},
                  'semantic':{'enabled':False,'evaluated':False,'reason':'No approved local model or independent quality gate'},
                  'channels':{}}
        experiment=LocalSemanticExperiment(semantic_model_dir)
        channels=['lexical','enhanced']+(['hybrid'] if semantic_model_dir is not None else [])
        for channel in channels:
            rows, times = [], []
            for case in cases:
                start = time.perf_counter()
                hits = experiment.search(case['query'],ids) if channel=='hybrid' else search_principles(case['query'], ids, enhanced=channel=='enhanced')
                times.append((time.perf_counter()-start)*1000)
                actual = [r['principle_id'].split(':',1)[1] for r in hits]
                expected = set(case['relevant'])
                rows.append({'id':case['id'],'top3':actual,'top1':bool(actual and actual[0] in expected) if expected else not actual,
                             'hit3':bool(expected & set(actual)) if expected else not actual})
            result['channels'][channel] = {
                'top1_pass':sum(r['top1'] for r in rows),'hit3_pass':sum(r['hit3'] for r in rows),'total':len(rows),
                'p50_ms':round(statistics.median(times),3),'p95_ms':round(sorted(times)[int((len(times)-1)*.95)],3),'rows':rows}
        result['semantic']={**semantic_diagnostics(),'evaluated':experiment.status=='ready','experiment_status':experiment.status,'model':Path(semantic_model_dir).name if semantic_model_dir else None,'minimum_similarity':experiment.minimum_similarity}
        return result

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path);parser.add_argument('--semantic-model-dir',type=Path,help='already installed local model; no download and no production activation')
    args=parser.parse_args();result=evaluate(args.semantic_model_dir);text=json.dumps(result,ensure_ascii=False,indent=2)+'\n'
    if args.output: args.output.write_text(text)
    else: print(text)
