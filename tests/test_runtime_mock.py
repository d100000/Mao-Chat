import io
import json
import os
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

    def test_structured_json_retries_once(self):
        from agent.llm import LLMResponse
        client = FakeClient([LLMResponse(content="不是JSON"), LLMResponse(content='{"answer":"答复","claims":[]}')])
        with tempfile.TemporaryDirectory() as td:
            cfg = AgentConfig(LLMConfig(api_key_env="unused"), FirecrawlConfig(api_key_env="unused"), Path(td)/"db.sqlite", Path(td)/"corpus.sqlite", Path(td))
            result = Agent(cfg, client=client, firecrawl=object(), knowledge=object()).structured_draft("问题")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["draft"]["answer"], "答复")
        self.assertEqual(len(client.calls), 2)

    def test_historical_mode_requires_a_valid_cutoff_at_all_entry_points(self):
        """A missing/invalid cutoff must never fall back to unrestricted search."""
        import sqlite3
        from agent.runtime import LocalKnowledge

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "agent.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE evidence_cards (evidence_id TEXT, topic TEXT, article_title TEXT, summary TEXT, excerpt TEXT, reviewed INTEGER, earliest_content_date TEXT, known_to_character_from TEXT)")
            conn.commit(); conn.close()
            knowledge = LocalKnowledge(db, Path(td) / "missing.sqlite")

            for cutoff in (None, "not-a-date"):
                try:
                    result = knowledge.search("实践论", "historical", cutoff)
                except (TypeError, ValueError):
                    continue
                self.assertTrue(result.get("blocked") or result.get("error"), result)
                self.assertFalse(result.get("results"), result)

            class Knowledge:
                def search(self, **kwargs):
                    raise AssertionError("historical policy should reject before retrieval")
                def trace_quote(self, **kwargs): return {"results": [], "untrusted": False}
                def trace_event(self, **kwargs): return {"results": [], "untrusted": False}
                def store_web_sources(self, result): pass

            for cutoff in (None, "not-a-date"):
                client = FakeClient([])
                cfg = AgentConfig(LLMConfig(api_key_env="unused"), FirecrawlConfig(api_key_env="unused"), db, Path(td) / "corpus.sqlite", Path(td))
                agent = Agent(cfg, client=client, firecrawl=object(), knowledge=Knowledge())
                try:
                    events = list(agent.stream("问题", mode="historical", as_of=cutoff))
                except (TypeError, ValueError):
                    events = []
                self.assertFalse(client.calls, cutoff)
                if events:
                    self.assertTrue(any(event.get("type") == "error" for event in events), events)

    def test_historical_event_search_excludes_missing_and_future_dates(self):
        import sqlite3
        from agent.runtime import LocalKnowledge

        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "agent.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE events (event_id TEXT, verification_status TEXT, start_date TEXT, end_date TEXT, title TEXT, description TEXT, location TEXT)")
            conn.execute("CREATE TABLE quote_event_links (relation_type TEXT, confidence REAL, verification_status TEXT, quote_id TEXT, event_id TEXT)")
            conn.execute("CREATE TABLE quotes (quote_id TEXT, text TEXT)")
            conn.executemany("INSERT INTO events VALUES (?, 'reviewed', ?, ?, ?, '', '')", [
                ("E_NULL", None, None, "无日期事件"),
                ("E_CUTOFF", "1949-10-01", None, "截止日事件"),
                ("E_FUTURE", "1949-10-02", None, "未来事件"),
                ("E_SPAN", "1949-09-01", "1949-10-02", "跨截止日事件"),
            ])
            conn.commit(); conn.close()
            result = LocalKnowledge(db, Path(td) / "missing.sqlite").trace_event_historical("事件", "1949-10-01")
            self.assertEqual([row["event_id"] for row in result["results"]], ["E_CUTOFF"])

    def test_historical_tools_do_not_expose_or_execute_firecrawl(self):
        from agent.llm import LLMResponse

        class FirecrawlSpy:
            def __init__(self): self.calls = 0
            def search(self, *args, **kwargs):
                self.calls += 1
                return {"query": "x", "results": [], "untrusted": True}

        class Knowledge:
            def search(self, **kwargs): return {"results": [], "untrusted": False}
            def trace_quote(self, **kwargs): return {"results": [], "untrusted": False}
            def trace_event(self, **kwargs): return {"results": [], "untrusted": False}
            def store_web_sources(self, result): pass

        with tempfile.TemporaryDirectory() as td:
            spy = FirecrawlSpy()
            client = FakeClient([
                LLMResponse(tool_calls=[{"id": "web1", "type": "function", "function": {"name": "firecrawl_search", "arguments": '{"query":"毛泽东"}'}}]),
                LLMResponse(content="回答"),
            ])
            cfg = AgentConfig(LLMConfig(api_key_env="unused"), FirecrawlConfig(api_key_env="unused"), Path(td) / "agent.db", Path(td) / "corpus.sqlite", Path(td), max_steps=3)
            agent = Agent(cfg, client=client, firecrawl=spy, knowledge=Knowledge())
            output = agent.ask("问题", mode="historical", as_of="1949-10-01")
            self.assertEqual(spy.calls, 0)
            self.assertNotIn("firecrawl_search", [tool["function"]["name"] for tool in client.calls[0]["tools"]])
            self.assertTrue(any(item["result"].get("blocked") or item["result"].get("error") for item in output["tools"]))

    def test_network_disabled_removes_and_blocks_firecrawl(self):
        from agent.llm import LLMResponse

        class FirecrawlSpy:
            def __init__(self): self.calls = 0
            def search(self, *args, **kwargs):
                self.calls += 1
                return {"query": "x", "results": [], "untrusted": True}

        class Knowledge:
            def search(self, **kwargs): return {"results": [], "untrusted": False}
            def trace_quote(self, **kwargs): return {"results": [], "untrusted": False}
            def trace_event(self, **kwargs): return {"results": [], "untrusted": False}
            def store_web_sources(self, result): pass

        with tempfile.TemporaryDirectory() as td:
            spy = FirecrawlSpy()
            client = FakeClient([
                LLMResponse(tool_calls=[{"id": "web1", "type": "function", "function": {"name": "firecrawl_search", "arguments": '{"query":"毛泽东"}'}}]),
                LLMResponse(content="回答"),
            ])
            cfg = AgentConfig(LLMConfig(api_key_env="unused"), FirecrawlConfig(api_key_env="unused"), Path(td) / "agent.db", Path(td) / "corpus.sqlite", Path(td), max_steps=3)
            agent = Agent(cfg, client=client, firecrawl=spy, knowledge=Knowledge())
            output = agent.ask("问题", network_enabled=False)
            self.assertEqual(spy.calls, 0)
            self.assertNotIn("firecrawl_search", [tool["function"]["name"] for tool in client.calls[0]["tools"]])
            self.assertTrue(any(item["result"].get("blocked") or item["result"].get("error") for item in output["tools"]))

    def test_evidence_gate_checks_namespaced_markers_with_colons(self):
        content, gate = Agent._apply_evidence_gate(
            "依据【quote:Q1】；未知【event:E9】。",
            [{"name": "trace_quote", "result": {"results": [{"quote_id": "Q1"}, {"event_id": "E1"}]}}],
            "research", None,
        )
        self.assertIn("event:E9", gate["unknown_markers"])
        self.assertNotIn("quote:Q1", gate["unknown_markers"])
        self.assertIn("证据门提示", content)

    def test_stream_does_not_publish_unlabeled_text_before_candidate_gate(self):
        from agent.llm import LLMResponse

        class Knowledge:
            def search(self, **kwargs): return {"results": [], "untrusted": False}
            def trace_quote(self, **kwargs): return {"results": [], "status": "candidate_page_match", "untrusted": True}
            def trace_event(self, **kwargs): return {"results": [], "untrusted": False}
            def store_web_sources(self, result): pass

        with tempfile.TemporaryDirectory() as td:
            client = FakeClient([
                LLMResponse(content="这是一句未经核验的原话。", tool_calls=[{"id": "q1", "type": "function", "function": {"name": "trace_quote", "arguments": '{"text":"原话"}'}}]),
                LLMResponse(content="最终回答。"),
            ])
            cfg = AgentConfig(LLMConfig(api_key_env="unused"), FirecrawlConfig(api_key_env="unused"), Path(td) / "agent.db", Path(td) / "corpus.sqlite", Path(td), max_steps=3)
            events = list(Agent(cfg, client=client, firecrawl=object(), knowledge=Knowledge()).stream("问题"))
            contents = [event["text"] for event in events if event.get("type") == "content" and event.get("text")]
            self.assertTrue(contents)
            self.assertTrue(contents[0].lstrip().startswith(("候选", "待核", "未核", "无法确认")), contents)


if __name__ == "__main__":
    unittest.main()
