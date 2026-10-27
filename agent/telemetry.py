"""Request-local measurements and reproducible versions; never conversation text."""
from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
import json
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parents[1]
_CURRENT = ContextVar('jiaoyuan_measurements', default=None)


@contextmanager
def telemetry_scope():
    data = {'calls': [], 'stages': {}, 'started': time.monotonic()}
    token = _CURRENT.set(data)
    try:
        yield data
    finally:
        _CURRENT.reset(token)


@contextmanager
def ensure_telemetry_scope():
    if _CURRENT.get() is not None:
        yield _CURRENT.get()
    else:
        with telemetry_scope() as data:
            yield data


def record_stage(name, elapsed_ms):
    current = _CURRENT.get()
    if current is not None and isinstance(name, str) and len(current['stages']) < 32:
        key = name[:80]
        current['stages'][key] = current['stages'].get(key, 0) + max(0, int(elapsed_ms))


def clean_usage(value):
    value = value if isinstance(value, dict) else {}
    result = {}
    for target, names in [('input_tokens', ('prompt_tokens', 'input_tokens')), ('output_tokens', ('completion_tokens', 'output_tokens'))]:
        count = next((value[k] for k in names if isinstance(value.get(k), int) and not isinstance(value[k], bool) and 0 <= value[k] <= 10**9), None)
        result[target] = count
    return result


def record_model_call(usage, elapsed_ms, status='ok'):
    current = _CURRENT.get()
    if current is not None and len(current['calls']) < 32:
        current['calls'].append({**clean_usage(usage), 'elapsed_ms': max(0, int(elapsed_ms)), 'status': 'ok' if status == 'ok' else 'error'})


def measurements():
    current = _CURRENT.get()
    calls = current['calls'] if current is not None else []
    totals = {}
    for key in ('input_tokens', 'output_tokens'):
        totals[key] = sum(x[key] for x in calls) if calls and all(x[key] is not None for x in calls) else None
    return {'usage': {'calls': len(calls), **totals, 'usage_complete': bool(calls) and all(x['input_tokens'] is not None and x['output_tokens'] is not None for x in calls)},
            'stage_timings_ms': dict(current['stages']) if current is not None else {}}


def _file_version(paths):
    digest = sha256()
    for name in paths:
        path = ROOT / name
        digest.update(name.encode())
        digest.update(path.read_bytes() if path.is_file() else b'<unavailable>')
    return digest.hexdigest()


# Capture the loaded deployment, rather than attributing a running old process
# to files edited later on disk. Mutable DB rows have their own fingerprints.
_VERSIONS = {
    'pipeline_version': _file_version(['agent/runtime.py','agent/dialogue.py','agent/claims.py','agent/statements.py','agent/support.py','agent/llm.py','agent/telemetry.py','agent/auth_server.py','agent/issues.py','agent/quality.py','agent/db.py','agent/index.py','agent/vector.py','agent/ingest.py','agent/firecrawl.py','web/index.html']),
    'prompt_version': _file_version(['agent/dialogue.py','agent/runtime.py','agent/support.py','agent/auth_server.py']),
    'knowledge_version': _file_version(['skills/mao-primary-sources/references/verified-evidence.jsonl','skills/mao-primary-sources/references/worldview.md','agent/principles.py','agent/cases.py']),
}


def audit_metadata(config, tools):
    fingerprints = {}
    for tool in tools or []:
        result = tool.get('result') or {}
        for key in ('results', 'method_notes', 'principles', 'cases'):
            for row in result.get(key, [])[:12] if isinstance(result.get(key), list) else []:
                if not isinstance(row, dict): continue
                identity = next((row[k] for k in ('case_id', 'principle_id', 'evidence_id', 'event_id', 'rule_id', 'quote_id', 'anchor', 'id') if isinstance(row.get(k), str)), None)
                if identity and len(fingerprints) < 64:
                    stable = {k:v for k,v in row.items() if k not in {'rank','score','hybrid_score','vector_score','distance','retrieval_score','retrieval_rank','retrieval_methods','retrieval_provenance','_tool_data'} and not k.startswith('retrieval_')}
                    fingerprints[identity[:160]] = sha256(json.dumps(stable, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    llm = getattr(config, "llm", None)
    return {**measurements(), **_VERSIONS, 'source_fingerprints': fingerprints,
            'runtime_settings': {key:getattr(config,key,None) for key in ('max_steps','local_limit','web_limit')},
            'model_settings': {key: getattr(llm, key, None) for key in ('provider', 'model', 'temperature', 'max_tokens', 'reasoning_effort','reasoning','stream','timeout_s')}}
