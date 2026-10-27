import json
from pathlib import Path
import tempfile
import unittest

from agent.db import connect
from agent.firecrawl import FirecrawlConfig
from agent.llm import LLMConfig, LLMResponse
from agent.runtime import Agent, AgentConfig
from agent.telemetry import measurements, record_model_call, telemetry_scope


class CliTelemetryTests(unittest.TestCase):
    def agent(self):
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)

        class Client:
            def chat(self, messages, **kwargs):
                record_model_call({'prompt_tokens': 13, 'completion_tokens': 7}, 9)
                return LLMResponse(content='这是不含历史主张的测试答复。')

        class Knowledge:
            def search(self, *args, **kwargs): return {'results': []}
            def trace_quote(self, **kwargs): return {'results': []}
            def trace_event(self, **kwargs): return {'results': []}
            def store_web_sources(self, result): pass

        cfg = AgentConfig(LLMConfig(api_key_env='unused', keychain_service=''), FirecrawlConfig(api_key_env='unused'),
                          Path(state.name)/'db', Path(state.name)/'missing', Path(state.name))
        return Agent(cfg, client=Client(), knowledge=Knowledge(), firecrawl=object())

    def row(self, agent):
        with connect(agent.config.db_path) as conn:
            return conn.execute('SELECT * FROM llm_runs ORDER BY run_id DESC LIMIT 1').fetchone()

    def test_ask_creates_its_own_scope_and_persists_actual_client_usage(self):
        agent = self.agent()
        self.assertTrue(agent.ask('测试', mode='research')['ok'])
        row = self.row(agent)
        self.assertEqual((row['input_tokens'], row['output_tokens']), (13, 7))
        meta = json.loads(row['metadata_json'])
        self.assertEqual(meta['usage']['calls'], 1)
        self.assertTrue(meta['usage']['usage_complete'])
        self.assertEqual(measurements()['usage']['calls'], 0)

    def test_ask_reuses_existing_scope_without_resetting_or_double_counting(self):
        agent = self.agent()
        with telemetry_scope() as existing:
            record_model_call({'prompt_tokens': 2, 'completion_tokens': 1}, 3)
            self.assertTrue(agent.ask('测试', mode='research')['ok'])
            self.assertEqual(len(existing['calls']), 2)
            self.assertEqual(measurements()['usage']['calls'], 2)
            row = self.row(agent)
            self.assertEqual((row['input_tokens'], row['output_tokens']), (15, 8))
            self.assertEqual(json.loads(row['metadata_json'])['usage']['calls'], 2)
        self.assertEqual(measurements()['usage']['calls'], 0)


if __name__ == '__main__': unittest.main()
