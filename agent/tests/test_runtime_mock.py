import io
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent.firecrawl import FirecrawlClient, FirecrawlConfig
from agent.llm import LLMConfig, OpenAICompatibleClient, iter_sse
from agent.llm import LLMError
from agent.runtime import Agent, AgentConfig


class FakeResponse:
    def __init__(self, body, headers=None):
        self.body = body
        self.headers = headers or {}
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, n=-1):
        if hasattr(self.body, "read"):
            return self.body.read(n)
        return self.body
    def __iter__(self): return iter(self.body.splitlines(True))


class FakeClient:
    def __init__(self, responses): self.responses = list(responses); self.calls = []
    def chat(self, messages, tools=None, response_format=None):
        from agent.llm import LLMResponse
        self.calls.append({"messages": messages, "tools": tools, "response_format": response_format})
        value = self.responses.pop(0)
        return value if isinstance(value, LLMResponse) else LLMResponse(**value)


def retrieval_fixture(path):
    """Build a bounded synthetic corpus without copying private runtime state."""
    from agent.db import connect, ensure_schema
    from agent.index import build_indexes
    from agent.vector import build_vectors
    conn = connect(path)
    ensure_schema(conn)
    with conn:
        conn.execute("INSERT INTO sources(source_id,title) VALUES('test-book','合成检索测试来源')")
        page = conn.execute("INSERT INTO pages(source_id,pdf_page,anchor,text) VALUES('test-book',1,'test-book:p0001','实践论 毛泽东 合成检索样例')").lastrowid
        conn.executemany("INSERT INTO passages(page_id,ordinal,article_title,text) VALUES(?,?,?,?)",
                         [(page, i, '实践论 合成测试', '毛泽东 实践论 认识 实践 检验 合成段落 %d' % i) for i in range(40)])
    build_indexes(conn)
    conn.close()
    build_vectors(path)


class RuntimeTests(unittest.TestCase):
    def test_sse_reasoning_content_and_answer(self):
        stream = ("data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"先查证\"}}]}\n\n"
                  "data: {\"choices\":[{\"delta\":{\"content\":\"答案\"}}]}\n\n"
                  "data: [DONE]\n\n").encode("utf-8")
        events = list(iter_sse(stream.splitlines(True)))
        self.assertEqual([e["type"] for e in events], ["reasoning", "content", "done"])
        self.assertEqual(events[0]["text"], "先查证")

    def test_openai_payload_streams_and_does_not_log_secret(self):
        captured = {}
        def opener(req, timeout=None):
            captured["body"] = req.data
            captured["auth"] = req.get_header("Authorization")
            return FakeResponse(b"data: {\"choices\":[{\"delta\":{\"content\":\"ok\"}}]}\n\ndata: [DONE]\n")
        with patch.dict(os.environ, {"TEST_LLM_KEY": "secret-value"}, clear=False):
            config = LLMConfig(api_key_env="TEST_LLM_KEY")
            response = OpenAICompatibleClient(config, opener=opener).chat([{"role":"user","content":"hi"}])
        self.assertEqual(response.content, "ok")
        self.assertEqual(json.loads(captured["body"])["reasoning_effort"], "high")
        self.assertEqual(captured["auth"], "Bearer secret-value")
        self.assertNotIn("secret-value", json.dumps(response.raw, ensure_ascii=False))

    def test_provider_http_error_is_normalized(self):
        from urllib.error import HTTPError
        def opener(req, timeout=None):
            raise HTTPError(req.full_url, 401, "unauthorized", {}, io.BytesIO(b"bad key"))
        with patch.dict(os.environ, {"TEST_LLM_KEY": "secret-value"}, clear=False):
            with self.assertRaises(LLMError) as ctx:
                OpenAICompatibleClient(LLMConfig(api_key_env="TEST_LLM_KEY"), opener=opener).chat([{"role": "user", "content": "hi"}])
        self.assertIn("HTTP 401", str(ctx.exception))
        self.assertNotIn("secret-value", str(ctx.exception))

    def test_non_stream_invalid_json_is_normalized(self):
        def opener(req, timeout=None):
            return FakeResponse(b"not-json")
        config = LLMConfig(api_key_env="TEST_LLM_KEY", stream=False)
        with patch.dict(os.environ, {"TEST_LLM_KEY": "secret-value"}, clear=False):
            with self.assertRaises(LLMError) as ctx:
                OpenAICompatibleClient(config, opener=opener).chat([{"role": "user", "content": "hi"}])
        self.assertIn("不是有效 JSON", str(ctx.exception))

    def test_stream_provider_exception_becomes_error_event(self):
        class BrokenClient:
            def chat(self, messages, tools=None, response_format=None):
                raise RuntimeError("provider disconnected")

        class Knowledge:
            def search(self, **kwargs): return {"results": [], "untrusted": False}
            def trace_quote(self, **kwargs): return {"results": [], "untrusted": False}
            def trace_event(self, **kwargs): return {"results": [], "untrusted": False}
            def store_web_sources(self, result): pass

        with tempfile.TemporaryDirectory() as td:
            cfg = AgentConfig(LLMConfig(api_key_env="unused"), FirecrawlConfig(api_key_env="unused"), Path(td) / "db.sqlite", Path(td) / "corpus.sqlite", Path(td))
            agent = Agent(cfg, client=BrokenClient(), firecrawl=object(), knowledge=Knowledge())
            events = list(agent.stream("问题"))
        self.assertEqual(events[0]["type"], "error")
        self.assertIn("provider disconnected", events[0]["error"])

    def test_firecrawl_v2_payload_and_untrusted_marker(self):
        captured = {}
        def opener(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())
            return FakeResponse(json.dumps({"data": [{"url": "https://example.com", "title": "t", "markdown": "data"}]}).encode())
        with patch.dict(os.environ, {"TEST_FIRE_KEY": "fire-secret"}, clear=False):
            result = FirecrawlClient(FirecrawlConfig(api_key_env="TEST_FIRE_KEY"), opener=opener).search("毛泽东 论持久战", 2)
        self.assertEqual(captured["url"], "https://api.firecrawl.dev/v2/search")
        self.assertEqual(captured["body"]["scrapeOptions"]["formats"], [{"type": "markdown"}])
        self.assertTrue(result["untrusted"])
        self.assertTrue(result["results"][0]["untrusted"])

    def test_react_tool_loop_and_untrusted_result(self):
        from agent.llm import LLMResponse
        responses = [
            LLMResponse(tool_calls=[{"id": "c1", "type": "function", "function": {"name": "local_search", "arguments": '{"query":"实践论"}'}}]),
            LLMResponse(content="结论【VE04】")
        ]
        class Knowledge:
            def search(self, **kwargs): return {"results": [{"id": "VE04", "text": "理论以实践为基础"}], "untrusted": False}
            def trace_quote(self, **kwargs): return {"results": [], "untrusted": False}
            def trace_event(self, **kwargs): return {"results": [], "untrusted": False}
            def store_web_sources(self, result): pass
        with tempfile.TemporaryDirectory() as td:
            cfg = AgentConfig(LLMConfig(api_key_env="unused"), FirecrawlConfig(api_key_env="unused"), Path(td)/"db.sqlite", Path(td)/"corpus.sqlite", Path(td), max_steps=3)
            agent = Agent(cfg, client=FakeClient(responses), firecrawl=object(), knowledge=Knowledge())
            output = agent.ask("实践如何检验认识？")
        self.assertTrue(output["ok"])
        self.assertIn("VE04", output["content"])
        self.assertEqual(len(output["tools"]), 1)
        self.assertTrue(output["tools"][0]["result"]["_tool_data"])

    def test_agent_forwards_streaming_content_chunks(self):
        from agent.llm import LLMResponse
        class StreamingClient:
            def stream_chat(self, messages, tools=None, response_format=None):
                yield {"type": "reasoning", "text": "查证"}
                yield {"type": "content", "text": "第一段"}
                yield {"type": "content", "text": "第二段"}
                yield {"type": "done"}
        class Knowledge:
            def search(self, **kwargs): return {"results": [], "untrusted": False}
            def trace_quote(self, **kwargs): return {"results": [], "untrusted": False}
            def trace_event(self, **kwargs): return {"results": [], "untrusted": False}
            def store_web_sources(self, result): pass
        with tempfile.TemporaryDirectory() as td:
            cfg = AgentConfig(LLMConfig(api_key_env="unused"), FirecrawlConfig(api_key_env="unused"), Path(td)/"db.sqlite", Path(td)/"corpus.sqlite", Path(td))
            agent = Agent(cfg, client=StreamingClient(), firecrawl=object(), knowledge=Knowledge())
            events = list(agent.stream("问题", show_reasoning=True))
        self.assertEqual([e["text"] for e in events if e["type"] == "content"], ["第一段", "第二段"])
        self.assertEqual([e["text"] for e in events if e["type"] == "reasoning"], ["查证"])

    def test_historical_mode_is_fail_closed_for_future_cards(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "agent.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE evidence_cards (evidence_id TEXT, topic TEXT, article_title TEXT, summary TEXT, excerpt TEXT, reviewed INTEGER, earliest_content_date TEXT, known_to_character_from TEXT)")
            conn.execute("INSERT INTO evidence_cards VALUES ('VE','持久战','论持久战','敌强我弱但可持久','',1,'1938-06-03','1938-06-03')")
            conn.execute("INSERT INTO evidence_cards VALUES ('VL','人民内部矛盾','后期材料','未来内容','',1,'1957-06-19','1957-06-19')")
            conn.commit(); conn.close()
            from agent.runtime import LocalKnowledge
            result = LocalKnowledge(db, Path(td)/"missing.sqlite").search("持久战", "historical", "1938-06-30")
        self.assertEqual([x["evidence_id"] for x in result["results"]], ["VE"])

    def test_research_hybrid_search_keeps_provenance(self):
        with tempfile.TemporaryDirectory(prefix="mao-agent-hybrid-") as td:
            db = Path(td) / "agent.db"
            retrieval_fixture(db)
            from agent.runtime import LocalKnowledge
            result = LocalKnowledge(db, Path(td) / "missing.sqlite").search("实践论", "research", None, 5, True)
            self.assertEqual(result.get("retrieval_method"), "hybrid_rrf")
            self.assertTrue(result.get("results"))
            self.assertTrue(all("hybrid_score" in item for item in result["results"]))
            methods = {method for item in result["results"] for method in item.get("retrieval_methods", [])}
            self.assertIn("fts5", methods)
            self.assertIn("vector", methods)
            self.assertTrue(all(item.get("retrieval_provenance") for item in result["results"]))
            passage_hits = [x for x in result['results'] if x.get('page_id')]
            self.assertTrue(passage_hits)
            self.assertTrue(all(x.get('anchor') and x.get('source_id') and x.get('pdf_page') for x in passage_hits))

    def test_retrieval_limits_are_bounded_for_negative_and_large_values(self):
        from agent.runtime import LocalKnowledge
        from agent.vector import search_vectors

        with tempfile.TemporaryDirectory(prefix="mao-agent-limits-") as td:
            db = Path(td) / "agent.db"
            retrieval_fixture(db)
            knowledge = LocalKnowledge(db, Path(td) / "missing.sqlite")
            negative = knowledge.search("毛泽东", limit=-1, hybrid=False)
            oversized = knowledge.search("毛泽东", limit=10**9, hybrid=False)
            self.assertEqual(len(negative["results"]), 1)
            self.assertEqual(len(oversized["results"]), 30)

            vector_negative = search_vectors(db, "毛泽东", limit=-1)
            vector_oversized = search_vectors(db, "毛泽东", limit=10**9)
            self.assertEqual(len(vector_negative), 1)
            self.assertEqual(len(vector_oversized), 30)

    def test_retrieval_tool_schemas_bound_limit(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = AgentConfig(LLMConfig(api_key_env="unused"), FirecrawlConfig(api_key_env="unused"), Path(td) / "db.sqlite", Path(td) / "corpus.sqlite", Path(td))
            from agent.runtime import LocalKnowledge
            agent = Agent(cfg, client=FakeClient([]), firecrawl=object(), knowledge=LocalKnowledge(cfg.db_path, cfg.corpus_db_path))
            schemas = {item.name: item.schema()["function"]["parameters"] for item in agent.tools()}
        for name in ("local_search", "local_vector_search", "trace_quote", "trace_event"):
            limit = schemas[name]["properties"]["limit"]
            self.assertEqual(limit["minimum"], 1)
            self.assertEqual(limit["maximum"], 30)
        firecrawl_limit = schemas["firecrawl_search"]["properties"]["limit"]
        self.assertEqual(firecrawl_limit["minimum"], 1)
        self.assertEqual(firecrawl_limit["maximum"], 20)

    def test_structured_json_retries_once(self):
        from agent.llm import LLMResponse
        client = FakeClient([LLMResponse(content="不是JSON"), LLMResponse(content='{"answer":"答复","claims":[]}')])
        with tempfile.TemporaryDirectory() as td:
            cfg = AgentConfig(LLMConfig(api_key_env="unused"), FirecrawlConfig(api_key_env="unused"), Path(td)/"db.sqlite", Path(td)/"corpus.sqlite", Path(td))
            result = Agent(cfg, client=client, firecrawl=object(), knowledge=object()).structured_draft("问题")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["draft"]["answer"], "答复")
        self.assertEqual(len(client.calls), 2)

    def test_structured_draft_rejects_unknown_evidence_claims(self):
        from agent.llm import LLMResponse
        payload = '{"answer":"答复","claims":[{"text":"凭空主张","kind":"quote","evidence_ids":["UNKNOWN"]}]}'
        client = FakeClient([LLMResponse(content=payload), LLMResponse(content=payload)])
        with tempfile.TemporaryDirectory() as td:
            cfg = AgentConfig(LLMConfig(api_key_env="unused"), FirecrawlConfig(api_key_env="unused"), Path(td)/"db.sqlite", Path(td)/"corpus.sqlite", Path(td))
            with self.assertRaises(LLMError):
                Agent(cfg, client=client, firecrawl=object(), knowledge=object()).structured_draft("问题", evidence=[{"evidence_id": "KNOWN"}])

    def test_session_roundtrip_preserves_history_and_metadata(self):
        with tempfile.TemporaryDirectory(prefix="mao-agent-session-") as td:
            config = AgentConfig(LLMConfig(api_key_env="unused"), FirecrawlConfig(api_key_env="unused"), Path(td) / "agent.db", Path(td) / "corpus.db", Path(td) / "sessions")
            agent = Agent(config, client=FakeClient([]), firecrawl=object(), knowledge=object())
            agent.save_session("demo", [{"role": "user", "content": "问题"}, {"role": "assistant", "content": "回答"}], {"mode": "research", "as_of": None})
            agent.save_session("demo", [{"role": "user", "content": "新问题"}, {"role": "assistant", "content": "新回答"}], {"mode": "research", "as_of": None})
            self.assertEqual(len(agent.load_session("demo")), 2)
            with sqlite3.connect(config.db_path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", ("demo",)).fetchone()[0], 2)
            self.assertEqual(agent.session_metadata("demo")["mode"], "research")
            self.assertEqual((Path(td) / "sessions").stat().st_mode & 0o777, 0o700)
            self.assertEqual((Path(td) / "sessions" / "demo.json").stat().st_mode & 0o777, 0o600)

    def test_config_relative_paths_follow_explicit_config_directory(self):
        with tempfile.TemporaryDirectory(prefix="mao-agent-config-") as td:
            root = Path(td)
            config = root / "nested" / "config.json"
            config.parent.mkdir()
            config.write_text(json.dumps({"db_path": "db/agent.db", "corpus_db_path": "corpus.sqlite", "session_dir": "sessions"}), encoding="utf-8")
            loaded = AgentConfig.load(config)
            self.assertEqual(loaded.db_path, (config.parent / "db/agent.db").resolve())
            self.assertEqual(loaded.corpus_db_path, (config.parent / "corpus.sqlite").resolve())
            self.assertEqual(loaded.session_dir, (config.parent / "sessions").resolve())

    def test_candidate_quote_is_labeled_before_confirmation(self):
        content, gate = Agent._apply_evidence_gate(
            "这句话是毛泽东的原话。",
            [{"name": "trace_quote", "result": {"status": "candidate_page_match", "results": []}}],
            "research", None,
        )
        self.assertTrue(gate["modified"])
        self.assertTrue(content.startswith("候选/待核"))

    def test_session_and_llm_run_are_persisted_and_path_is_safe(self):
        import sqlite3
        with tempfile.TemporaryDirectory(prefix="mao-agent-audit-") as td:
            db = Path(td) / "agent.db"
            config = AgentConfig(LLMConfig(api_key_env="unused"), FirecrawlConfig(api_key_env="unused"), db, Path(td) / "corpus.db", Path(td) / "sessions")
            agent = Agent(config, client=FakeClient([]), firecrawl=object(), knowledge=object())
            agent.save_session("audit-demo", [{"role": "user", "content": "问题"}, {"role": "assistant", "content": "答案【VE01】", "evidence_ids": ["VE01"]}], {"mode": "research"})
            agent._persist_run("问题", "research", None, "答案【VE01】", [{"name": "local_search", "result": {"results": [{"evidence_id": "VE01"}]}}], 0.01, session_id="audit-demo")
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)
                self.assertGreaterEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
                self.assertGreaterEqual(conn.execute("SELECT COUNT(*) FROM llm_runs").fetchone()[0], 1)
                self.assertGreaterEqual(conn.execute("SELECT COUNT(*) FROM citations").fetchone()[0], 1)
            with self.assertRaises(ValueError):
                agent.save_session("../escape", [], {})

    def test_failed_llm_run_persists_error_detail(self):
        import sqlite3
        with tempfile.TemporaryDirectory(prefix="mao-agent-error-audit-") as td:
            db = Path(td) / "agent.db"
            config = AgentConfig(LLMConfig(api_key_env="unused"), FirecrawlConfig(api_key_env="unused"), db, Path(td) / "corpus.db", Path(td) / "sessions")
            agent = Agent(config, client=FakeClient([]), firecrawl=object(), knowledge=object())
            agent._persist_run("问题", "research", None, "", [], 0.01, "error", error="provider disconnected")
            with sqlite3.connect(db) as conn:
                self.assertEqual(conn.execute("SELECT status FROM llm_runs").fetchone()[0], "error")
                self.assertEqual(conn.execute("SELECT error FROM llm_runs").fetchone()[0], "provider disconnected")


if __name__ == "__main__":
    unittest.main()
