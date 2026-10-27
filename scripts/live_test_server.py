#!/usr/bin/env python3
"""Isolated HTTP server for explicit live-provider browser acceptance tests.

Unlike ui_test_server.py, every model and web-search request uses the configured
real providers and may consume account credits. Credentials are loaded only by
the existing AdminConfigStore and are never included in status responses.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROVIDER = 'live-openai-compatible-and-firecrawl'


def safe_error(value):
    """Avoid persisting credential-shaped strings echoed by a remote service."""
    return re.sub(r'\b(?:sk|fc)-[A-Za-z0-9_-]+', '[redacted]', str(value))[:500]


def make_live_server(port=0, state_dir=None):
    from agent.admin_config import AdminConfigStore
    from agent.auth import AuthService
    from agent.auth_server import AuthHandler, AuthHTTPServer
    from agent.db import default_db_path
    from agent.runtime import Agent

    source_db = default_db_path().resolve()
    # Load through the normal secure credential mechanism before redirecting
    # database/session environment variables into the isolated test directory.
    cfg = AdminConfigStore(source_db).load_agent_config()
    state = Path(state_dir or tempfile.mkdtemp(prefix='mao-live-browser-')).resolve()
    state.mkdir(parents=True, exist_ok=True)
    state.chmod(0o700)
    db = state / 'agent.db'
    if db.resolve() == source_db:
        raise ValueError('Live test database must be separate from the project database')
    if db.exists():
        raise ValueError('Use a new empty --state-dir for each live browser test')
    with sqlite3.connect(source_db) as source, sqlite3.connect(db) as target:
        source.backup(target)
    db.chmod(0o600)
    with sqlite3.connect(db) as conn:
        conn.execute('PRAGMA foreign_keys=OFF')
        for table in ('auth_sessions', 'admin_sessions', 'captcha_challenges',
                      'auth_audit', 'auth_rate_limits', 'users', 'admins',
                      'citations', 'messages', 'llm_runs', 'sessions', 'web_sources', 'issue_state', 'issue_feedback', 'quality_runs'):
            if conn.execute('SELECT 1 FROM sqlite_master WHERE name=?', (table,)).fetchone():
                conn.execute('DELETE FROM ' + table)
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='app_settings'").fetchone():
            conn.execute('DELETE FROM app_settings WHERE is_secret=1')
    os.environ['MAO_AGENT_DB'] = str(db)
    os.environ['MAO_SESSION_DIR'] = str(state / 'sessions')
    os.environ['MAO_ADMIN_SECRET_FILE'] = str(state / 'provider-secrets')
    os.environ['MAO_AUTH_SECRET'] = secrets.token_urlsafe(48)
    os.environ.pop('MAO_ADMIN_USERNAME', None)
    os.environ.pop('MAO_ADMIN_PASSWORD', None)
    cfg.db_path = db
    cfg.session_dir = state / 'sessions'
    cfg.corpus_db_path = cfg.corpus_db_path.resolve()
    cfg.max_steps = min(6, max(1, cfg.max_steps))
    requests = []
    lock = threading.Lock()

    class ReadOnlyLiveConfigStore(AdminConfigStore):
        def _write(self, *args, **kwargs):
            raise RuntimeError('Configuration changes are disabled in live acceptance tests')
        def _set_secret(self, *args, **kwargs):
            raise RuntimeError('Credential changes are disabled in live acceptance tests')

    class TracedLiveAgent(Agent):
        """Observe the real Agent without replacing clients or tool handlers."""
        def stream(self, question, mode='research', as_of=None, history=None,
                   show_reasoning=False, network_enabled=True):
            started = time.monotonic()
            entry = {'mode': mode, 'as_of': as_of, 'network_enabled': network_enabled,
                     'history_count': len(history or []), 'tool_names': [],
                     'tool_results': [], 'answer_chars': 0, 'status': 'running'}
            with lock:
                requests.append(entry)
            try:
                for event in super().stream(question, mode, as_of, history,
                                            show_reasoning, network_enabled):
                    kind = event.get('type')
                    with lock:
                        if kind == 'tool':
                            name = event.get('name')
                            result = event.get('result') or {}
                            rows = result.get('results', []) if isinstance(result, dict) else []
                            entry['tool_names'].append(name)
                            entry['tool_results'].append({
                                'name': name,
                                'result_count': len(rows) if isinstance(rows, list) else 0,
                                'untrusted': bool(result.get('untrusted')) if isinstance(result, dict) else None,
                                'urls': [row.get('url') for row in rows if isinstance(row, dict) and row.get('url')][:5],
                                'error': safe_error(result.get('error')) if isinstance(result, dict) and result.get('error') else None,
                            })
                        elif kind == 'content':
                            entry['answer_chars'] += len(event.get('text', ''))
                        elif kind == 'error':
                            entry['status'] = 'error'
                            entry['error'] = safe_error(event.get('error', 'generation failed'))
                        elif kind == 'done':
                            entry['status'] = 'ok'
                            entry['steps'] = event.get('steps')
                    yield event
            finally:
                with lock:
                    entry['elapsed_s'] = round(time.monotonic() - started, 2)

    class LiveTestHandler(AuthHandler):
        def do_GET(self):
            if self.path == '/__live_test/status':
                with lock:
                    snapshot = json.loads(json.dumps(requests))
                self._json(200, {'provider': PROVIDER, 'model': cfg.llm.model,
                                 'max_steps': cfg.max_steps, 'reasoning': cfg.llm.reasoning,
                                 'reasoning_effort': cfg.llm.reasoning_effort,
                                 'requests': snapshot})
                return
            super().do_GET()

    service = AuthService(db)
    store = ReadOnlyLiveConfigStore(db)
    server = AuthHTTPServer(('127.0.0.1', int(port)), LiveTestHandler,
                            service, TracedLiveAgent(cfg), store)
    return server, state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--state-dir', type=Path)
    args = parser.parse_args()
    temporary = tempfile.TemporaryDirectory(prefix='mao-live-browser-') if args.state_dir is None else None
    server = None
    try:
        server, state = make_live_server(args.port, args.state_dir or temporary.name)
        print(json.dumps({'ready': True,
                          'base_url': 'http://127.0.0.1:%d' % server.server_address[1],
                          'provider': PROVIDER, 'state_dir': str(state)}), flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.server_close()
        if temporary:
            temporary.cleanup()


if __name__ == '__main__':
    main()
