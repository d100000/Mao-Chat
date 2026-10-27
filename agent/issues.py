"""Account-scoped dialogue state and explicit user-controlled memory.

Only tombstone identifiers survive deletion. Model suggestions can create
proposals, but only an authenticated user update can advance action status.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import hmac
import os
from pathlib import Path
import re
import sqlite3
import threading
import uuid

from .dialogue import UNDERSTANDING_PARAMETERS
from .statements import (add_turn, effective_analysis, prompt_statement_context,
                         public_changes, public_statements, statement_sources,
                         update_statement)


ISSUE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS issue_state (
  user_id INTEGER NOT NULL,
  thread_id TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  archived INTEGER NOT NULL DEFAULT 0,
  remember INTEGER NOT NULL DEFAULT 0,
  deleted INTEGER NOT NULL DEFAULT 0,
  revision INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  state_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (user_id, thread_id)
);
CREATE INDEX IF NOT EXISTS idx_issue_owner_updated ON issue_state(user_id, updated_at);
'''
MAX_ACTIONS = 12
MAX_UNDERSTANDING_HISTORY = 10
PUBLIC_ROW_KEYS = {
    'record_type', 'reviewed', 'verification_status', 'quote_eligible', 'quote_verified',
    'id', 'evidence_id', 'quote_id', 'event_id', 'rule_id', 'principle_id', 'source_id',
    'title', 'topic', 'article_title', 'label', 'summary', 'excerpt', 'description',
    'decision_rule', 'conditions', 'notice', 'source_locator', 'source_role',
    'original_date_display', 'earliest_content_date', 'earliest_wording_date',
    'start_date', 'end_date', 'pdf_page', 'pdf_pages', 'anchor', 'anchors', 'url',
    'source_urls', 'quotation_scope', 'review_scope', 'content_qualification',
    'evidence_ids', 'dimension', 'author_claim', 'research_interpretation',
    'modern_application', 'historical_conditions', 'unsupported_extensions',
    'case_id', 'facts', 'outcome_evidence', 'unknowns', 'modern_analogy',
    'non_analogies', 'source_ids', 'completion', 'supporting_evidence_ids',
    'source_fingerprint', 'source_version', 'problem_context', 'resource_conditions',
    'untrusted', 'status', 'text', 'markdown',
}


def _public_value(value, depth=0):
    if depth > 4: return None
    if isinstance(value, str): return value[:2400]
    if value is None or isinstance(value, (bool, int, float)): return value
    if isinstance(value, list): return [_public_value(x, depth + 1) for x in value[:16]]
    if isinstance(value, dict):
        return {k: _public_value(v, depth + 1) for k, v in value.items()
                if k in PUBLIC_ROW_KEYS | {'supports', 'marker', 'record_id'}}
    return None


def public_tools(value):
    tools = []
    for item in value[:24] if isinstance(value, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not isinstance(item.get('result'), dict):
            continue
        result = {}
        for key, data in item['result'].items():
            if key in {'results','method_notes','principles','cases','checks'} and isinstance(data, list):
                result[key] = [_public_value(x) for x in data[:8] if isinstance(x, dict)]
            elif key in {'query','status','notice','history_gap','grounding_notice','untrusted','error'}:
                result[key] = _public_value(data)
        tools.append({'name': item['name'][:80], 'result': result})
    return tools


class IssueError(ValueError):
    def __init__(self, message, status=400, *, deleted=False, cancelled=False):
        super().__init__(message)
        self.status = status
        self.deleted = deleted
        self.cancelled = cancelled


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def thread_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,64}', value):
        raise IssueError('会话标识无效')
    return value


def _text(value, limit, name, *, empty=True):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise IssueError(name + '格式或长度无效')
    return value.strip()


def proposal(value):
    """Copy only proposal fields. Model-supplied action status is ignored."""
    if not isinstance(value, dict) or not isinstance(value.get('title'), str) or not value['title'].strip():
        return None
    steps = value.get('steps', [])
    if not isinstance(steps, list) or not all(isinstance(x, str) for x in steps):
        return None
    result = {'title': value['title'].strip()[:140], 'steps': [x.strip()[:300] for x in steps[:5] if x.strip()]}
    for key in ('continue_signal', 'adjust_signal', 'review_hint'):
        text = value.get(key, '')
        if not isinstance(text, str):
            return None
        result[key] = text.strip()[:400]
    return result


def public_analysis(value):
    if not isinstance(value, dict):
        return None
    allowed = set(UNDERSTANDING_PARAMETERS['properties']) | {'tentative', 'basis', 'statement_sources'}
    output = {}
    for key in allowed:
        if key not in value:
            continue
        item = value[key]
        if key == 'proposed_action':
            output[key] = proposal(item)
        elif key == 'statement_sources':
            output[key] = statement_sources(item)
        elif key == 'tentative':
            output[key] = True
        elif isinstance(item, str):
            output[key] = item[:600 if key == 'summary' else 240]
        elif isinstance(item, list):
            output[key] = [x[:240] for x in item[:6] if isinstance(x, str)]
        elif item is None:
            output[key] = None
    return output


def public_claim_checks(value):
    if not isinstance(value, dict):
        return None
    output = {}
    for key in ('claims', 'quote_checks'):
        rows = []
        for item in value.get(key, [])[:24] if isinstance(value.get(key), list) else []:
            if not isinstance(item, dict):
                continue
            row = {field: item[field][:600 if field in {'text','quote'} else 240]
                   for field in ('text','kind','quote','status','notice') if isinstance(item.get(field), str)}
            row['evidence_ids'] = [x[:160] for x in item.get('evidence_ids', [])[:6] if isinstance(x, str)] if isinstance(item.get('evidence_ids'), list) else []
            rows.append(row)
        output[key] = rows
    if isinstance(value.get('scope'), str):
        output['scope'] = value['scope'][:600]
    if isinstance(value.get('coverage'), str):
        output['coverage'] = value['coverage'][:600]
    output['passed'] = value.get('passed') is True
    return output


def public_metadata(value):
    value = value if isinstance(value, dict) else {}
    output = {'mode': value.get('mode', 'consult'), 'as_of': value.get('as_of')}
    analysis = public_analysis(value.get('analysis'))
    if analysis is not None:
        output['analysis'] = analysis
    for key in ('title', 'updated_at'):
        if isinstance(value.get(key), str):
            output[key] = value[key][:240]
    if isinstance(value.get('latest_turn_id'), str):
        output['latest_turn_id'] = value['latest_turn_id'][:64]
    output['statements'] = public_statements(value.get('statements'))
    output['goal_history'] = public_changes(value.get('goal_history'), 'goal')
    output['review_history'] = public_changes(value.get('review_history'), 'review')
    checks = public_claim_checks(value.get('claim_checks'))
    if checks is not None:
        output['claim_checks'] = checks
    output['archived'] = value.get('archived') is True
    output['remember'] = value.get('remember') is True
    actions = []
    for item in value.get('actions', []) if isinstance(value.get('actions'), list) else []:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str):
            continue
        data = proposal(item)
        if data is None or item.get('status') not in {'proposed', 'accepted', 'tried', 'reviewed', 'stopped'}:
            continue
        data.update(id=item['id'][:64], status=item['status'])
        for key, limit in (('feedback', 2000), ('created_at', 80), ('updated_at', 80), ('source_turn_id', 64)):
            if isinstance(item.get(key), str):
                data[key] = item[key][:limit]
        actions.append(data)
    output['actions'] = actions[-MAX_ACTIONS:]
    output['used_evidence_ids'] = [x[:160] for x in value.get('used_evidence_ids', [])[:100] if isinstance(x, str)] if isinstance(value.get('used_evidence_ids'), list) else []
    output['understanding_history'] = []
    for item in value.get('understanding_history', [])[-MAX_UNDERSTANDING_HISTORY:] if isinstance(value.get('understanding_history'), list) else []:
        if not isinstance(item, dict):
            continue
        analysis = public_analysis(item.get('analysis'))
        if analysis is not None:
            output['understanding_history'].append({
                'analysis': analysis, 'source_turn_id': str(item.get('source_turn_id', ''))[:64],
                'question': str(item.get('question', ''))[:4000], 'updated_at': str(item.get('updated_at', ''))[:80],
                'tentative': True,
            })
    output['used_memories'] = []
    for item in value.get('used_memories', [])[:3] if isinstance(value.get('used_memories'), list) else []:
        if isinstance(item, dict) and isinstance(item.get('thread_id'), str):
            output['used_memories'].append({'thread_id': item['thread_id'][:65], 'title': str(item.get('title', ''))[:100]})
    if 'analysis' in output:
        output['analysis'] = effective_analysis(output)
    return output


class IssueStore:
    def __init__(self, db_path, session_dir):
        self.db_path = Path(db_path)
        self.session_dir = Path(session_dir)
        self.lock = threading.RLock()
        self.active_streams = {}
        with self._connect() as conn:
            conn.executescript(ISSUE_SCHEMA)

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        return conn

    @staticmethod
    def session_id(user_id, tid):
        return 'u%s_%s' % (int(user_id), thread_id(tid))

    def _path(self, user_id, tid):
        path = self.session_dir / (self.session_id(user_id, tid) + '.json')
        if path.is_symlink():
            raise IssueError('会话不可用', 404)
        return path

    def _row(self, conn, user_id, tid):
        return conn.execute('SELECT * FROM issue_state WHERE user_id=? AND thread_id=?', (int(user_id), tid)).fetchone()

    @staticmethod
    def _state(row):
        try:
            value = json.loads(row['state_json'])
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError):
            return {}

    def _write(self, conn, user_id, tid, meta):
        meta = public_metadata(meta)
        timestamp = now()
        meta['updated_at'] = timestamp
        conn.execute('''INSERT INTO issue_state(user_id,thread_id,title,archived,remember,updated_at,state_json)
          VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,thread_id) DO UPDATE SET
          title=excluded.title,archived=excluded.archived,remember=excluded.remember,
          revision=issue_state.revision+1,updated_at=excluded.updated_at,state_json=excluded.state_json''',
          (int(user_id), tid, meta.get('title', ''), int(meta['archived']), int(meta['remember']), timestamp, json.dumps(meta, ensure_ascii=False)))
        return meta

    def load(self, user_id, tid, agent):
        tid = thread_id(tid)
        with self.lock, self._connect() as conn:
            row = self._row(conn, user_id, tid)
            if row is not None and row['deleted']:
                raise IssueError('议题已删除', 404, deleted=True)
            self._path(user_id, tid)
            sid = self.session_id(user_id, tid)
            messages = agent.load_session(sid)
            raw = agent.session_metadata(sid) if hasattr(agent, 'session_metadata') else {}
            raw = raw if isinstance(raw, dict) else {}
            meta = public_metadata(raw)
            if row is not None:
                meta.update(public_metadata(self._state(row)))
            exists = bool(messages) or bool(raw) or (row is not None and bool(row['title']))
            # Discover legacy account-prefixed snapshots without importing
            # any other user's record or arbitrary paths.
            if row is None and exists:
                if not meta.get('title'):
                    meta['title'] = next((str(x.get('content', ''))[:60] for x in messages if isinstance(x, dict) and x.get('role') == 'user'), '未命名议题')
                meta = self._write(conn, user_id, tid, meta)
            return {'messages': messages if isinstance(messages, list) else [], 'metadata': meta,
                    'tools': public_tools(raw.get('tools')), 'exists': exists}

    def begin_stream(self, user_id, tid, agent):
        key = (int(user_id), thread_id(tid))
        with self.lock, self._connect() as conn:
            row = self._row(conn, *key)
            if row is not None and row['deleted']:
                raise IssueError('议题已删除，请新建对话', 410, deleted=True)
            if key in self.active_streams:
                raise IssueError('这个议题正在生成，请等待或新建对话', 409)
            snapshot = self.load(*key, agent)
            if row is None:
                conn.execute('INSERT OR IGNORE INTO issue_state(user_id,thread_id,updated_at) VALUES(?,?,?)', (*key, now()))
            token = uuid.uuid4().hex
            self.active_streams[key] = token
            snapshot['stream_token'] = token
            return snapshot

    def begin_temporary_stream(self, user_id, tid):
        """Register only an in-memory cancellation token, never private state."""
        key = (int(user_id), thread_id(tid))
        with self.lock:
            if key in self.active_streams:
                raise IssueError('这个议题正在生成，请等待或新建对话', 409)
            token = uuid.uuid4().hex
            self.active_streams[key] = token
            return token

    def stop_stream(self, user_id, tid, token):
        key = (int(user_id), thread_id(tid))
        if not isinstance(token, str) or not re.fullmatch(r'[a-fA-F0-9]{32}', token):
            raise IssueError('生成标识无效')
        with self.lock:
            current = self.active_streams.get(key)
            if current is not None and hmac.compare_digest(current, token):
                self.active_streams.pop(key)
                return True
            return False

    def assert_active(self, user_id, tid, token):
        key = (int(user_id), thread_id(tid))
        with self.lock:
            if not token or self.active_streams.get(key) != token:
                # Deletion revokes the in-memory lease immediately. Only a
                # mismatching lease needs this control-state lookup; ordinary
                # streamed deltas never read the database.
                with self._connect() as conn:
                    row = self._row(conn, *key)
                if row is not None and row['deleted']:
                    raise IssueError('议题已删除，本轮结果未保存', 410, deleted=True)
                raise IssueError('本轮已停止，未保存草稿', 409, cancelled=True)

    def end_stream(self, user_id, tid, token=None):
        key = (int(user_id), tid)
        with self.lock:
            if token is not None and self.active_streams.get(key) == token:
                self.active_streams.pop(key)

    def complete_stream(self, user_id, tid, agent, messages, metadata, question, audit, *, stream_token=None):
        """Serialize deletion and the complete save/audit operation."""
        with self.lock, self._connect() as conn:
            row = self._row(conn, user_id, tid)
            if row is not None and row['deleted']:
                raise IssueError('议题已删除，本轮结果未保存', 410, deleted=True)
            self.assert_active(user_id, tid, stream_token)
            current = self._state(row) if row is not None else {}
            # User-controlled title/archive/memory/actions win over the
            # snapshot captured before a potentially long model call.
            merged = public_metadata(current)
            for key in ('mode', 'as_of', 'used_evidence_ids', 'used_memories', 'claim_checks'):
                if key in metadata:
                    merged[key] = metadata[key]
            if not merged.get('title'):
                merged['title'] = question[:60]
            turn = uuid.uuid4().hex
            merged['latest_turn_id'] = turn
            analysis = public_analysis(metadata.get('analysis'))
            if analysis is not None:
                analysis = add_turn(merged, analysis, question, turn, now())
            else:
                merged['analysis'] = None
            if (len(messages) >= 2 and messages[-2].get('role') == 'user'
                    and messages[-2].get('content') == question and messages[-1].get('role') == 'assistant'):
                messages[-2]['turn_id'] = turn
                messages[-1]['turn_id'] = turn
            elif messages and messages[-1].get('role') == 'user' and messages[-1].get('content') == question:
                messages[-1]['turn_id'] = turn
            history = merged.get('understanding_history', [])
            if analysis is not None and (not history or history[-1].get('analysis') != analysis):
                history.append({'analysis': analysis, 'source_turn_id': turn, 'question': question[:4000], 'updated_at': now(), 'tentative': True})
            merged['understanding_history'] = history[-MAX_UNDERSTANDING_HISTORY:]
            suggested = proposal(analysis.get('proposed_action')) if analysis else None
            actions = merged.get('actions', [])
            if suggested is not None and not any(all(item.get(k) == v for k, v in suggested.items()) for item in actions):
                if len(actions) >= MAX_ACTIONS:
                    removable = next((i for i, x in enumerate(actions) if x.get('status') in {'proposed', 'reviewed', 'stopped'}), None)
                    if removable is not None:
                        actions.pop(removable)
                if len(actions) < MAX_ACTIONS:
                    actions.append({**suggested, 'id': uuid.uuid4().hex, 'status': 'proposed', 'feedback': '',
                                    'source_turn_id': turn, 'created_at': now(), 'updated_at': now()})
            merged['actions'] = actions
            # Save the transcript before updating the index. Agent's own DB
            # writes use a separate transaction, so do not hold a write lock
            # from this connection while invoking it.
            saved = {**merged, 'tools': metadata.get('tools', []), 'user_id': int(user_id)}
            agent.save_session(self.session_id(user_id, tid), messages, saved)
            audit()
            merged = self._write(conn, user_id, tid, merged)
            return merged

    def list(self, user_id, agent, *, include_archived=False, limit=50, offset=0):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise IssueError('limit需为1至100的整数')
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 100000:
            raise IssueError('offset需为0至100000的整数')
        with self.lock, self._connect() as conn:
            # Gradually index old snapshots by the authenticated owner's
            # literal prefix. A wildcard can never expand to another account.
            prefix = 'u%s_' % int(user_id)
            ids = [r[0][len(prefix):] for r in conn.execute('SELECT session_id FROM sessions WHERE session_id GLOB ? LIMIT 10000', (prefix + '*',))]
            if self.session_dir.is_dir():
                ids += [p.stem[len(prefix):] for p in self.session_dir.glob(prefix + '*.json')][:10000]
            known = {r[0] for r in conn.execute('SELECT thread_id FROM issue_state WHERE user_id=?', (int(user_id),))}
            for tid in set(ids) - known:
                try: self.load(user_id, tid, agent)
                except IssueError: continue
            query = 'SELECT * FROM issue_state WHERE user_id=? AND deleted=0 AND title<>?'
            params = [int(user_id), '']
            if not include_archived:
                query += ' AND archived=0'
            total = conn.execute('SELECT COUNT(*) FROM (' + query + ')', params).fetchone()[0]
            rows = conn.execute(query + ' ORDER BY updated_at DESC, thread_id LIMIT ? OFFSET ?', (*params, limit, offset)).fetchall()
            sessions = []
            for row in rows:
                meta = public_metadata(self._state(row))
                sessions.append({'thread_id': row['thread_id'], **meta})
            return {'sessions': sessions, 'total': total, 'limit': limit, 'offset': offset}

    def update(self, user_id, tid, agent, changes):
        allowed = {'title', 'archived', 'remember', 'action_id', 'action_status', 'action_feedback',
                   'statement_id', 'statement_status', 'corrected_value'}
        if not isinstance(changes, dict) or set(changes) - allowed or not changes:
            raise IssueError('议题更新字段无效')
        with self.lock:
            snapshot = self.load(user_id, tid, agent)
            if not snapshot['exists']:
                raise IssueError('议题不存在', 404)
            meta = snapshot['metadata']
            if 'title' in changes:
                meta['title'] = _text(changes['title'], 100, '标题', empty=False)
            for key in ('archived', 'remember'):
                if key in changes:
                    if not isinstance(changes[key], bool): raise IssueError(key + '必须为布尔值')
                    meta[key] = changes[key]
            if any(key in changes for key in ('statement_id', 'statement_status', 'corrected_value')):
                identity = _text(changes.get('statement_id'), 64, '理解记录ID', empty=False)
                if 'statement_status' not in changes and 'corrected_value' not in changes:
                    raise IssueError('请选择确认、停用或更正理解记录')
                try:
                    update_statement(meta, identity, changes.get('statement_status'), changes.get('corrected_value'), timestamp=now())
                except ValueError as exc:
                    raise IssueError(str(exc), 404 if str(exc) == '理解记录不存在' else 400) from exc
            if any(key.startswith('action_') for key in changes):
                aid = _text(changes.get('action_id'), 64, '行动ID', empty=False)
                action = next((x for x in meta['actions'] if x['id'] == aid), None)
                if action is None: raise IssueError('行动不存在', 404)
                if 'action_status' in changes:
                    target = changes['action_status']
                    if not isinstance(target, str) or target not in {'accepted','tried','reviewed','stopped'}:
                        raise IssueError('行动状态无效')
                    transitions = {'proposed': {'accepted','stopped'}, 'accepted': {'tried','stopped'},
                                   'tried': {'reviewed','stopped'}, 'reviewed': set(), 'stopped': set()}
                    if target != action['status'] and target not in transitions[action['status']]:
                        raise IssueError('行动状态转换无效')
                    action['status'] = target
                if 'action_feedback' in changes:
                    action['feedback'] = _text(changes['action_feedback'], 2000, '行动反馈')
                action['updated_at'] = now()
                if 'action_feedback' in changes and action.get('feedback'):
                    reviews = public_changes(meta.get('review_history'), 'review')
                    reviews.append({'review_id': uuid.uuid4().hex, 'kind': 'action_feedback',
                                    'source_turn_id': meta.get('latest_turn_id', ''),
                                    'recorded_at': now(), 'previous_summary': '', 'current_summary': '',
                                    'feedback': action['feedback'], 'action_id': action['id'],
                                    'action_status': action['status'],
                                    'notice': '用户手动报告的反馈；行动状态仅反映用户本次选择。'})
                    meta['review_history'] = reviews[-12:]
            # Metadata edits must not run save_session's transcript replace:
            # replacing messages would discard their citation audit rows.
            path = self._path(user_id, tid)
            if path.is_file():
                payload = {'session_id': self.session_id(user_id, tid), 'messages': snapshot['messages'],
                           'metadata': {**meta, 'tools': snapshot['tools'], 'user_id': int(user_id)}}
                path.parent.mkdir(parents=True, exist_ok=True)
                os.chmod(path.parent, 0o700)
                temporary_path = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
                try:
                    fd = os.open(str(temporary_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                        json.dump(payload, handle, ensure_ascii=False, indent=2)
                        handle.write('\n')
                    temporary_path.replace(path)
                finally:
                    temporary_path.unlink(missing_ok=True)
            with self._connect() as conn:
                meta = self._write(conn, user_id, tid, meta)
                conn.execute('UPDATE sessions SET metadata_json=?,updated_at=CURRENT_TIMESTAMP WHERE session_id=?',
                             (json.dumps({**meta, 'user_id': int(user_id)}, ensure_ascii=False), self.session_id(user_id, tid)))
            return meta

    def delete(self, user_id, tid, agent):
        tid = thread_id(tid)
        with self.lock:
            snapshot = self.load(user_id, tid, agent)
            with self._connect() as conn:
                row = self._row(conn, user_id, tid)
                if not snapshot['exists'] and (row is None or (int(user_id), tid) not in self.active_streams):
                    raise IssueError('议题不存在', 404)
                sid = self.session_id(user_id, tid)
                # Retain only the identifier tombstone to prevent an already
                # running generation from recreating deleted conversation data.
                conn.execute('''INSERT INTO issue_state(user_id,thread_id,deleted,updated_at) VALUES(?,?,1,?)
                    ON CONFLICT(user_id,thread_id) DO UPDATE SET deleted=1,title='',remember=0,archived=0,
                    revision=issue_state.revision+1,updated_at=excluded.updated_at,state_json='{}' ''', (int(user_id), tid, now()))
                conn.execute('DELETE FROM citations WHERE message_id IN (SELECT message_id FROM messages WHERE session_id=?)', (sid,))
                conn.execute('DELETE FROM messages WHERE session_id=?', (sid,))
                conn.execute('DELETE FROM llm_runs WHERE session_id=?', (sid,))
                conn.execute('DELETE FROM sessions WHERE session_id=?', (sid,))
                for table in ('issue_feedback', 'quality_runs'):
                    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                        conn.execute('DELETE FROM ' + table + ' WHERE user_id=? AND thread_id=?', (int(user_id), tid))
            self.active_streams.pop((int(user_id), tid), None)
            self._path(user_id, tid).unlink(missing_ok=True)

    def export(self, user_id, tid, agent):
        snapshot = self.load(user_id, tid, agent)
        if not snapshot['exists']:
            raise IssueError('议题不存在', 404)
        messages = []
        for item in snapshot['messages']:
            if isinstance(item, dict) and item.get('role') in {'user','assistant'}:
                exported = {'role': item['role'], 'content': str(item.get('content', ''))[:12000]}
                if isinstance(item.get('turn_id'), str):
                    exported['turn_id'] = item['turn_id'][:64]
                messages.append(exported)
        return {'thread_id': tid, 'exported_at': now(), 'messages': messages, 'metadata': snapshot['metadata'], 'tools': snapshot['tools']}

    def memory(self, user_id, ids, agent):
        if ids is None: return []
        if not isinstance(ids, list) or len(ids) > 3 or not all(isinstance(x, str) for x in ids) or len(set(ids)) != len(ids):
            raise IssueError('最多选择3个不同的已记住议题')
        result = []
        for tid in ids:
            snapshot = self.load(user_id, tid, agent)
            meta = snapshot['metadata']
            if not snapshot['exists'] or meta.get('remember') is not True:
                raise IssueError('记忆议题不存在或未授权', 404)
            result.append({'thread_id': tid, 'title': meta.get('title',''), 'analysis': effective_analysis(meta),
                           'actions': meta.get('actions', []), 'statements': prompt_statement_context(meta)})
        return result
