"""Private, deletable feedback and content-free operational measurements."""
from datetime import datetime, timezone
import json
import math
import sqlite3

SCHEMA = '''
CREATE TABLE IF NOT EXISTS issue_feedback(
 user_id INTEGER NOT NULL,thread_id TEXT NOT NULL,turn_id TEXT NOT NULL,
 kind TEXT NOT NULL,comment TEXT NOT NULL DEFAULT '',updated_at TEXT NOT NULL,
 PRIMARY KEY(user_id,thread_id,turn_id));
CREATE TABLE IF NOT EXISTS quality_runs(
 user_id INTEGER NOT NULL,thread_id TEXT NOT NULL,turn_id TEXT NOT NULL,
 status TEXT NOT NULL,elapsed_ms INTEGER NOT NULL,first_content_ms INTEGER,
 reason TEXT,metadata_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,
 PRIMARY KEY(user_id,thread_id,turn_id));
CREATE INDEX IF NOT EXISTS idx_quality_created ON quality_runs(created_at);
'''
KINDS = {'helpful','not_helpful','fact_error','source_issue'}


def failure_code(text):
    text=str(text or '')
    if any(x in text for x in ('字数','句数','表达要求')): return 'format'
    if any(x in text for x in ('引语','引用','逐字','出处资格')): return 'citation'
    if any(x in text for x in ('LLM','模型服务','密钥','网络请求','timed out')): return 'provider'
    if '未完成' in text: return 'incomplete'
    return 'unknown'


class QualityStore:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as conn: conn.executescript(SCHEMA)

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def feedback(self, user_id, tid, turn_id, kind, comment=''):
        if not isinstance(kind, str) or kind not in KINDS:
            raise ValueError('反馈类型无效')
        if not isinstance(comment, str) or len(comment) > 1000:
            raise ValueError('反馈说明最多1000字')
        with self.connect() as conn:
            conn.execute('INSERT INTO issue_feedback VALUES(?,?,?,?,?,?) ON CONFLICT(user_id,thread_id,turn_id) DO UPDATE SET kind=excluded.kind,comment=excluded.comment,updated_at=excluded.updated_at',
                         (int(user_id),tid,turn_id,kind,comment.strip(),datetime.now(timezone.utc).isoformat()))
        return {'kind':kind,'saved':True}

    def record_run(self, user_id, tid, turn_id, status, elapsed_ms, first_content_ms, metadata, reason=None):
        # Only allow numeric measurements, code/knowledge fingerprints and safe
        # configuration. Provider errors, prompts and answers never enter here.
        safe = {k:metadata[k] for k in ('pipeline_version','prompt_version','knowledge_version','source_fingerprints','usage','stage_timings_ms','model_settings','runtime_settings','first_status_ms','delivery_mode') if k in metadata}
        try:
            with self.connect() as conn:
                conn.execute('INSERT OR REPLACE INTO quality_runs VALUES(?,?,?,?,?,?,?,?,?)',
                             (int(user_id),tid,turn_id,'ok' if status=='ok' else 'error',max(0,int(elapsed_ms)),None if first_content_ms is None else max(0,int(first_content_ms)),reason if reason in {'provider','incomplete','cancelled','format','citation','unknown'} else None,json.dumps(safe,ensure_ascii=False),datetime.now(timezone.utc).isoformat()))
            return True
        except sqlite3.Error:
            # Measurement failures must not turn a saved answer into a retry.
            return False

    def export_feedback(self, user_id, tid):
        with self.connect() as conn:
            rows=conn.execute('SELECT turn_id,kind,comment,updated_at FROM issue_feedback WHERE user_id=? AND thread_id=? ORDER BY updated_at LIMIT 1000',(int(user_id),tid)).fetchall()
        return [dict(x) for x in rows]

    def aggregate(self, days=30):
        if isinstance(days,bool) or not isinstance(days,int) or not 1 <= days <= 90:
            raise ValueError('统计天数需为1至90的整数')
        cutoff = datetime.now(timezone.utc).timestamp() - days*86400
        stamp = datetime.fromtimestamp(cutoff,timezone.utc).isoformat()
        with self.connect() as conn:
            rows=conn.execute('SELECT status,elapsed_ms,first_content_ms,metadata_json,reason FROM quality_runs WHERE created_at>=? ORDER BY created_at DESC LIMIT 10000',(stamp,)).fetchall()
            feedback={x['kind']:x['n'] for x in conn.execute('SELECT kind,COUNT(*) n FROM issue_feedback WHERE updated_at>=? GROUP BY kind',(stamp,))}
        def percentiles(values):
            values=sorted(values)
            return {'samples':len(values),'p50_ms':values[math.ceil(len(values)*.5)-1] if values else None,'p95_ms':values[math.ceil(len(values)*.95)-1] if values else None}
        completed=[x for x in rows if x['status']=='ok']
        statuses = []
        delivery_modes = {}
        input_known=output_known=calls=known_calls=0
        versions=set(); reasons={}
        for row in rows:
            try: meta=json.loads(row['metadata_json'])
            except (TypeError,ValueError): meta={}
            first = meta.get('first_status_ms')
            if isinstance(first, int) and 0 <= first: statuses.append(first)
            delivery = meta.get('delivery_mode')
            if delivery in {'verified_paragraph', 'buffered_review'}: delivery_modes[delivery] = delivery_modes.get(delivery, 0) + 1
            usage=meta.get('usage',{})
            c=usage.get('calls',0); calls+=c if isinstance(c,int) and not isinstance(c,bool) and 0<=c<=32 else 0
            if usage.get('usage_complete') is True:
                known_calls+=c
                input_known+=usage.get('input_tokens') or 0
                output_known+=usage.get('output_tokens') or 0
            if meta.get('pipeline_version'): versions.add(meta['pipeline_version'])
            if row['reason']: reasons[row['reason']]=reasons.get(row['reason'],0)+1
        return {'window_days':days,'sample_limit':10000,'runs':len(rows),'completed':len(completed),'errors':len(rows)-len(completed),
                'first_status':percentiles(statuses),'delivery_modes':delivery_modes,
                'latency':percentiles([x['elapsed_ms'] for x in completed]),
                'first_helpful_content':percentiles([x['first_content_ms'] for x in completed if x['first_content_ms'] is not None]),
                'feedback':{k:feedback.get(k,0) for k in sorted(KINDS)},'failure_reasons':reasons,
                'usage':{'calls':calls,'reported_calls':known_calls,'reported_input_tokens':input_known if known_calls else None,'reported_output_tokens':output_known if known_calls else None,'cost':None},
                'pipeline_versions':len(versions),'notice':'仅统计持久议题的已完成答复及其失败重试，临时对话、首次失败或取消不计；用户反馈为自愿样本。无供应商用量时保持未知，不估造费用；状态提示不计首个有效内容。最多最近10000次，不代表长期效果。'}
