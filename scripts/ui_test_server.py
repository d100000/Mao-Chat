#!/usr/bin/env python3
"""Isolated browser-test server. Uses real HTTP/auth/corpus and fake providers.

No remote model/search requests or system Keychain operations are performed.
The temporary database is removed on exit unless --state-dir is supplied.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def make_test_server(port=8876, state_dir=None):
    from agent import auth_server
    from agent.admin_config import AdminConfigStore
    from agent.auth import AuthService
    from agent.runtime import Agent, AgentConfig
    from agent.llm import LLMError

    state = Path(state_dir or tempfile.mkdtemp(prefix='mao-ui-e2e-')).resolve()
    state.mkdir(parents=True, exist_ok=True)
    db = state / 'agent.db'
    if not db.exists():
        # Build only the repository's public evidence. Never copy production
        # account, wallet, credential, challenge or conversation tables.
        from scripts.bootstrap_library import bootstrap
        bootstrap(db)
        # The application state stays independent of the real project/user.
        with sqlite3.connect(db) as conn:
            conn.execute('PRAGMA foreign_keys=OFF')
            for table in ('auth_sessions', 'admin_sessions', 'captcha_challenges', 'auth_audit', 'auth_rate_limits', 'users', 'admins', 'app_settings', 'citations', 'messages', 'llm_runs', 'sessions', 'web_sources', 'issue_state', 'issue_feedback', 'quality_runs'):
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                    conn.execute('DELETE FROM ' + table)
    os.environ['MAO_AGENT_DB'] = str(db)
    os.environ['MAO_SESSION_DIR'] = str(state / 'sessions')
    os.environ['MAO_ADMIN_SECRET_FILE'] = str(state / 'provider-secrets')
    os.environ['MAO_AUTH_SECRET'] = 'isolated-browser-test-secret-only-32bytes'
    os.environ.pop('MAO_ADMIN_USERNAME', None)
    os.environ.pop('MAO_ADMIN_PASSWORD', None)

    requests = []

    class SafeConfigStore(AdminConfigStore):
        def _keychain_get(self, service): return ''
        def _keychain_set(self, service, value): return False
        def _keychain_delete(self, service): return None
        def _secret_value(self, key, service, env_name): return 'test-provider-placeholder'

    class DeterministicClient:
        def __init__(self, config):
            self.config = config

        def stream_chat(self, messages, tools=None, response_format=None):
            from agent.billing_runtime import before_model_request
            from agent.telemetry import record_model_call
            before_model_request({'messages': messages, 'tools': tools or [], 'max_tokens': self.config.max_tokens}, self.config)
            status = 'error'
            try:
                yield from self._events(messages, tools, response_format)
                status = 'ok'
            finally:
                record_model_call({'prompt_tokens': 1000, 'completion_tokens': 150}, 1, status)

        def _events(self, messages, tools=None, response_format=None):
            latest_user = max(i for i, m in enumerate(messages) if m.get('role') == 'user')
            question = str(messages[latest_user].get('content', ''))
            if 'e2e-error' in question:
                raise LLMError('测试替身：模拟模型服务暂时不可用，请重试')
            tail = messages[latest_user + 1:]
            consultation = any(t['function']['name'] == 'understand_user' for t in tools or [])
            if consultation and not any(m.get('role') == 'tool' and m.get('name') == 'understand_user' for m in tail):
                listening = '只想' in question or '不要给建议' in question
                analysis = {
                    'phase': 'listen' if listening else 'analyze',
                    'intent': 'listen' if listening else 'advice',
                    'summary': '这是隔离浏览器测试的公开理解，供用户核对。',
                    'long_term_direction': '',
                    'stage_goal': '六周内服务好现有客户' if not listening else '',
                    'goal': '六周内服务好现有客户' if not listening else '',
                    'turn_request': '先听我说说' if listening else '确定本周可以尝试的一步',
                    'known_facts': ['用户希望讨论当前问题'],
                    'constraints': ['没有额外预算'] if '没有额外预算' in question else [], 'uncertainties': ['具体情况待用户补充'],
                    'next_question': '' if listening else '现有客户最在意哪类问题？',
                }
                if not listening:
                    analysis['proposed_action'] = {
                        'title': '核对现有客户的稳定性问题',
                        'steps': ['记录一条已有问题', '向客户核对实际影响'],
                        'continue_signal': '客户能指认一个具体问题',
                        'adjust_signal': '尚未获得具体反馈',
                        'review_hint': '有反馈时再一起复盘',
                    }
                analysis['statement_sources'] = []
                for field, quote in [('stage_goal','六周内留住现有客户'),('constraints:0','没有额外预算')]:
                    if quote in question: analysis['statement_sources'].append({'field':field,'quote':quote})
                calls = [{'index': 0, 'id': 'test-understanding', 'type': 'function', 'function': {'name': 'understand_user', 'arguments': json.dumps(analysis, ensure_ascii=False)}}]
                if not listening:
                    calls.append({'index': 1, 'id': 'test-method', 'type': 'function', 'function': {'name': 'method_search', 'arguments': '{"query":"调查研究 人民利益","limit":4}'}})
                yield {'type': 'tool_call_delta', 'tool_calls': calls}
                return
            if consultation or (tools is None and any(m.get('name') == 'understand_user' for m in tail)):
                time.sleep(3 if 'e2e-slow' in question else .1)
                listening = '只想' in question or '不要给建议' in question
                answer = ('【浏览器测试替身】可以先说说，我会先听你讲，暂时不安排下一步。'
                          if listening else '【浏览器测试替身】先核对现有客户的实际问题，再决定下一步。调查方法可参考【VE08】。现代建议：本周记录一个具体问题，并向客户核对；反馈不同就调整。')
                yield {'type': 'content', 'text': answer}
                return
            if not any(m.get('role') == 'tool' for m in tail):
                calls = [{'index': 0, 'id': 'test-local', 'type': 'function', 'function': {'name': 'local_search', 'arguments': '{"query":"实践论","limit":3}'}}]
                if any(t['function']['name'] == 'firecrawl_search' for t in tools or []):
                    calls.append({'index': 1, 'id': 'test-web', 'type': 'function', 'function': {'name': 'firecrawl_search', 'arguments': '{"query":"测试网页候选","limit":1}'}})
                yield {'type': 'tool_call_delta', 'tool_calls': calls}
                return
            if 'e2e-slow' in question:
                time.sleep(3)
            else:
                time.sleep(.25)
            answer = '【浏览器测试替身】已完成本地史料检索。实践与认识互相联系，请结合原文核对具体主张。'
            if latest_user > 1:
                answer += '\n已读取本会话的上轮问题。'
            for text in (answer[:20], answer[20:]):
                yield {'type': 'content', 'text': text}

    class FakeFirecrawl:
        def search(self, query, limit=None):
            return {'query': query, 'untrusted': True, 'results': [{'title': '浏览器测试网页候选', 'url': 'https://example.test/evidence', 'description': '仅用于测试候选标签，不是历史证据。', 'markdown': '这是测试替身的网页内容。', 'untrusted': True}]}

    class TestAgent(Agent):
        def __init__(self, config):
            super().__init__(config, client=DeterministicClient(config.llm), firecrawl=FakeFirecrawl())
        def stream(self, question, mode='research', as_of=None, history=None, show_reasoning=False, network_enabled=True):
            requests.append({'question': question, 'mode': mode, 'as_of': as_of, 'network_enabled': network_enabled, 'history_count': len(history or [])})
            yield from super().stream(question, mode, as_of, history, show_reasoning, network_enabled)

    class TestHandler(auth_server.AuthHandler):
        def do_GET(self):
            if self.path == '/__test/status':
                self._json(200, {'provider': 'deterministic-test-double', 'requests': requests})
                return
            super().do_GET()

    auth_server.Agent = TestAgent
    service = AuthService(db)
    service.bootstrap_admin('ui_admin', 'ui-admin-password-123')
    store = SafeConfigStore(db)
    config = store.load_agent_config()
    config.db_path = db
    config.corpus_db_path = ROOT / 'corpus/search.sqlite'
    config.session_dir = state / 'sessions'
    server = auth_server.AuthHTTPServer(('127.0.0.1', int(port)), TestHandler, service, TestAgent(config), store, security_test_mode=True)
    server.points.publish_price({'provider': config.llm.provider, 'gateway': config.llm.base_url, 'model': config.llm.model,
                                 'rates': {key: '0.001' for key in ('input_uncached','cache_read','cache_write','billable_output')},
                                 'adapter': 'openai', 'cache_read_supported': False, 'cache_write_supported': False})
    server.points.configure({'daily_cash_limit_cny': '100'})
    server.search_cash_bound = '0.01'
    return server, state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8876)
    parser.add_argument('--state-dir', type=Path)
    args = parser.parse_args()
    temporary = tempfile.TemporaryDirectory(prefix='mao-ui-e2e-') if args.state_dir is None else None
    server, state = make_test_server(args.port, args.state_dir or temporary.name)
    print(json.dumps({'ready': True, 'base_url': 'http://127.0.0.1:%d' % server.server_address[1], 'provider': 'deterministic-test-double', 'state_dir': str(state)}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if temporary:
            temporary.cleanup()


if __name__ == '__main__':
    main()
