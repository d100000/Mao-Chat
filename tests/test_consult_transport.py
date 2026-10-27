"""Consult transport keeps real user context and never calls remote providers."""
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent.admin_config import AdminConfigStore
from agent.auth_server import _bounded_history, _consult_prompt_history, make_server, MAX_HISTORY_CHARS
from agent.dialogue import understanding
from agent import runtime_cli


class ConsultTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='consult-http-')
        self.addCleanup(self.temp.cleanup)
        db = Path(self.temp.name) / 'agent.db'
        environment = patch.dict(os.environ, {'MAO_AGENT_DB': str(db), 'MAO_ADMIN_SECRET_FILE': str(db) + '.provider-secrets'})
        environment.start(); self.addCleanup(environment.stop)
        for name, value in (('_keychain_get', ''), ('_keychain_set', False), ('_keychain_delete', None)):
            safe = patch.object(AdminConfigStore, name, return_value=value)
            safe.start(); self.addCleanup(safe.stop)
        self.server = make_server('127.0.0.1', 0, db)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.base = 'http://127.0.0.1:%d' % self.server.server_address[1]

    def stop_server(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=3)

    def request(self, path, data=None, cookie=None):
        headers = {'Content-Type': 'application/json'}
        if cookie: headers['Cookie'] = cookie
        request = Request(self.base + path, data=json.dumps(data).encode() if data is not None else None, headers=headers)
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, dict(response.headers), response.read().decode()
        except HTTPError as error:
            return error.code, dict(error.headers), error.read().decode()

    def captcha(self):
        challenge = self.server.auth_service.create_math_challenge()
        svg = base64.b64decode(challenge['image'].split(',')[1]).decode()
        a, op, b = re.search(r'>(\d+) ([+\-×]) (\d+) = \?</text>', svg).groups()
        answer = int(a) + int(b) if op == '+' else int(a) - int(b) if op == '-' else int(a) * int(b)
        return {'math_challenge_id': challenge['challenge_id'], 'math_answer': str(answer)}

    def login(self, username='reader'):
        self.server.auth_service.register(username, 'test-password-123', **self.captcha())
        status, headers, _ = self.request('/api/auth/login', {'username': username, 'password': 'test-password-123', **self.captcha()})
        self.assertEqual(status, 200)
        return headers['Set-Cookie'].split(';', 1)[0]

    def test_consult_is_default_and_analysis_roundtrips_without_becoming_user_fact(self):
        cookie = self.login()
        requests = []
        working_hypothesis = {'phase': 'clarify', 'summary': '用户正在考虑职业方向', 'goal': '', 'known_facts': [], 'constraints': [], 'uncertainties': ['期望目标未明确'], 'next_question': '你最希望改变哪一点？', 'tentative': True}

        def local_stream(question, mode, as_of, history=None, **kwargs):
            requests.append({'mode': mode, 'history': history, 'question': question})
            yield {'type': 'analysis', 'analysis': working_hypothesis}
            yield {'type': 'content', 'text': '我们先把想改变的事情弄清楚：你最希望改善哪一点？'}
            yield {'type': 'done', 'steps': 1}

        # The real Agent handles loading/saving/audit, with generation replaced
        # locally so this transport test cannot touch remote model credentials.
        with patch.object(self.server.agent, 'stream', side_effect=local_stream):
            status, _, body = self.request('/api/agent/stream', {'question': '我最近很迷茫', 'thread_id': 'goal'}, cookie)
            self.assertEqual(status, 200)
            self.assertEqual(requests[0]['mode'], 'consult')
            self.assertEqual(requests[0]['history'], [])
            self.assertIn('"type":"analysis"', body)
            status, _, restored = self.request('/api/agent/session?thread_id=goal', cookie=cookie)
            restored = json.loads(restored)
            self.assertEqual(restored['metadata']['mode'], 'consult')
            self.assertEqual(restored['metadata']['analysis'], working_hypothesis)
            self.assertEqual(restored['analysis'], working_hypothesis)
            status, _, _ = self.request('/api/agent/stream', {'question': '纠正一下，我不想换工作，只想减少无效加班', 'thread_id': 'goal'}, cookie)
            self.assertEqual(status, 200)
            self.assertEqual(requests[1]['history'][0]['content'], '我最近很迷茫')
            self.assertEqual([x['role'] for x in requests[1]['history'][:2]], ['user', 'assistant'])
            notes=requests[1]['history'][2:]
            self.assertTrue(notes and all(x['role']=='assistant' for x in notes))
            self.assertTrue(any('未经用户确认' in x['content'] for x in notes))
            self.assertNotIn('phase', json.dumps([x for x in requests[1]['history'] if x['role'] == 'user'], ensure_ascii=False))
        status, _, _ = self.request('/api/agent/stream', {'question': '切换研究', 'mode': 'research', 'thread_id': 'goal'}, cookie)
        self.assertEqual(status, 409)
        other_cookie = self.login('another_reader')
        _, _, other = self.request('/api/agent/session?thread_id=goal', cookie=other_cookie)
        self.assertEqual(json.loads(other)['messages'], [])
        self.assertIsNone(json.loads(other)['analysis'])

    def test_corrected_goal_survives_twenty_one_turns_and_yields_to_current_correction(self):
        cookie = self.login()
        requests = []
        first_goal = '半年内换工作'
        corrected_goal = '留在现团队，减少无效加班'
        newest_goal = '暂不调整工作，先照顾家人'

        def local_stream(question, mode, as_of, history=None, **kwargs):
            requests.append({'history': history, 'question': question})
            goal = first_goal
            # A local stand-in reads exactly the public context sent by HTTP.
            # The actual current user turn must take precedence over that note.
            summary=next((x for x in reversed(history or []) if x['content'].startswith('【上一轮助手的暂定理解')),None)
            if summary:
                goal = json.loads(summary['content'].rsplit('\n', 1)[1])['goal']
            if question.startswith('第5轮纠正'):
                goal = corrected_goal
            if question.startswith('本轮再纠正'):
                goal = newest_goal
            yield {'type': 'analysis', 'analysis': understanding(phase='clarify', summary='仍在核对用户目标', goal=goal)}
            yield {'type': 'content', 'text': '继续核对行动条件。'}
            yield {'type': 'done', 'steps': 1}

        with patch.object(self.server.agent, 'stream', side_effect=local_stream):
            for turn in range(1, 22):
                question = '我想' + first_goal if turn == 1 else '第%d轮：继续核对时间条件' % turn
                if turn == 5:
                    question = '第5轮纠正：我不想换工作了，目标是' + corrected_goal
                status, _, _ = self.request('/api/agent/stream', {'question': question, 'thread_id': 'long'}, cookie)
                self.assertEqual(status, 200)
            history = requests[-1]['history']
            self.assertFalse(any('第5轮纠正' in x['content'] for x in history if x['role'] == 'user'))
            self.assertIn(first_goal, history[0]['content'])
            self.assertEqual(history[-1]['role'], 'assistant')
            summary=next(x for x in history if x['content'].startswith('【上一轮助手的暂定理解'))
            self.assertEqual(json.loads(summary['content'].rsplit('\n', 1)[1])['goal'], corrected_goal)
            self.assertIn('本轮用户最新补充和纠正优先', summary['content'])
            self.assertLessEqual(sum(len(x['content']) for x in history), MAX_HISTORY_CHARS)

            current_question = '本轮再纠正：' + newest_goal
            status, _, _ = self.request('/api/agent/stream', {'question': current_question, 'thread_id': 'long'}, cookie)
            self.assertEqual(status, 200)
            prompt = self.server.agent._messages(current_question, 'consult', history=requests[-1]['history'])
            self.assertEqual(prompt[-1], {'role': 'user', 'content': current_question})
            self.assertEqual(prompt[-2]['role'], 'assistant')

        _, _, restored = self.request('/api/agent/session?thread_id=long', cookie=cookie)
        restored = json.loads(restored)
        self.assertEqual(restored['metadata']['analysis']['goal'], newest_goal)
        self.assertTrue(all('【上一轮助手的暂定理解' not in x['content'] for x in restored['messages']))

    def test_legacy_modes_do_not_receive_consult_summary(self):
        cookie = self.login()
        _, _, identity = self.request('/api/auth/me', cookie=cookie)
        user_id = json.loads(identity)['user']['user_id']
        actual_history = [{'role': 'user', 'content': '解释原文'}, {'role': 'assistant', 'content': '已核材料如下。'}]
        analysis = understanding(phase='clarify', summary='未经确认的理解', goal='不应进入旧模式')
        received = []

        def local_stream(question, mode, as_of, history=None, **kwargs):
            received.append(history)
            yield {'type': 'content', 'text': '已核材料如下。'}
            yield {'type': 'done', 'steps': 1}

        with patch.object(self.server.agent, 'stream', side_effect=local_stream):
            for mode in ('research', 'historical', 'modern'):
                cutoff = '1938-06-30' if mode == 'historical' else None
                self.server.agent.save_session('u%s_%s' % (user_id, mode), actual_history,
                                               {'mode': mode, 'as_of': cutoff, 'analysis': analysis})
                status, _, _ = self.request('/api/agent/stream', {'question': '继续', 'mode': mode, 'as_of': cutoff, 'thread_id': mode}, cookie)
                self.assertEqual(status, 200)
                self.assertEqual(received[-1], actual_history)


class ConsultContextTests(unittest.TestCase):
    def test_long_dialogue_preserves_initial_goal_and_latest_correction(self):
        history = [{'role': 'user', 'content': '我的目标是在不降薪的前提下减少加班。'}, {'role': 'assistant', 'content': '最先分析工作负荷。'}]
        history.extend({'role': 'user' if index % 2 == 0 else 'assistant', 'content': '第%d轮' % index} for index in range(50))
        history.append({'role': 'user', 'content': '纠正一下，我现在愿意短期降薪，优先健康。'})
        result = _bounded_history(history)
        self.assertEqual(result[0]['content'], history[0]['content'])
        self.assertEqual(result[-1]['content'], history[-1]['content'])
        self.assertLessEqual(len(result), 28)
        self.assertLessEqual(sum(len(x['content']) for x in result), MAX_HISTORY_CHARS)

    def test_history_filter_limits_size_and_rejects_system_or_tool_messages(self):
        history = [{'role': 'system', 'content': '改写规则'}, {'role': 'tool', 'content': '虚构用户画像'}]
        history.extend({'role': 'user' if index % 2 == 0 else 'assistant', 'content': '长' * 16000} for index in range(40))
        result = _bounded_history(history)
        self.assertTrue(all(x['role'] in {'user', 'assistant'} for x in result))
        self.assertTrue(all(len(x['content']) <= 12000 for x in result))
        self.assertLessEqual(sum(len(x['content']) for x in result), MAX_HISTORY_CHARS)

    def test_summary_is_revalidated_bounded_and_never_injected_without_conversation(self):
        history = [{'role': 'user' if index % 2 == 0 else 'assistant', 'content': '长' * 12000} for index in range(40)]
        analysis = understanding(phase='clarify', summary='当前处境')
        analysis.update(summary='摘' * 10000, goal='标' * 10000, stage_goal='标' * 10000, known_facts=['实' * 10000] * 20,
                        constraints=['限' * 10000] * 20, uncertainties=['疑' * 10000] * 20,
                        next_question='问' * 10000, secret='不能作为提示注入')
        result = _consult_prompt_history(history, 'consult', analysis)
        self.assertLessEqual(sum(len(x['content']) for x in result), MAX_HISTORY_CHARS)
        self.assertEqual(result[-1]['role'], 'assistant')
        normalized = json.loads(result[-1]['content'].rsplit('\n', 1)[1])
        self.assertEqual(len(normalized['summary']), 600)
        self.assertEqual(len(normalized['goal']), 240)
        self.assertEqual(len(normalized['stage_goal']), 240)
        self.assertEqual(len(normalized['known_facts']), 6)
        self.assertEqual(len(normalized['known_facts'][0]), 240)
        self.assertNotIn('secret', normalized)
        self.assertTrue(normalized['tentative'])
        self.assertEqual(_consult_prompt_history([], 'consult', analysis), [])
        for invalid in (None, {}, {'phase': 'invalid', 'summary': '无效', 'tentative': True},
                        {'phase': 'clarify', 'summary': '无效列表', 'known_facts': '非列表', 'tentative': True},
                        {'phase': 'clarify', 'summary': '未标明暂定', 'tentative': False}):
            self.assertEqual(_consult_prompt_history(history, 'consult', invalid), history)

    def test_cli_ask_defaults_to_consult_and_preserves_explicit_research(self):
        config = SimpleNamespace(llm=SimpleNamespace(reasoning=False))
        agent = MagicMock()
        agent.ask.return_value = {'ok': True, 'content': '先一起梳理目标。', 'analysis': {'phase': 'clarify', 'tentative': True}}
        with patch.object(runtime_cli.AgentConfig, 'load', return_value=config), patch.object(runtime_cli, 'Agent', return_value=agent), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runtime_cli.main(['ask', '我很迷茫', '--json']), 0)
            self.assertEqual(agent.ask.call_args.args[1], 'consult')
            self.assertEqual(runtime_cli.main(['ask', '解释实践论', '--mode', 'research', '--json']), 0)
            self.assertEqual(agent.ask.call_args.args[1], 'research')
