#!/usr/bin/env python3
"""Small dependency-free OpenAI-compatible streaming client.

The client deliberately keeps secrets out of configuration files and logs.  It
accepts the OpenAI chat-completions wire format used by DeepSeek, vLLM, LM
Studio and other compatible gateways.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional
from urllib import error, request
from .telemetry import record_model_call
from .billing_runtime import before_model_request


class LLMError(RuntimeError):
    """Provider or response error."""


@dataclass
class LLMConfig:
    provider: str = "openai_compatible"
    base_url: str = "https://api.bob-api.com/v1"
    model: str = "deepseek-v4.1-flash"
    api_key_env: str = "MAO_LLM_API_KEY"
    temperature: float = 0.2
    max_tokens: int = 2500
    timeout_s: int = 120
    stream: bool = True
    reasoning: bool = True
    reasoning_effort: str = "high"
    show_reasoning: bool = False
    max_steps: int = 6
    keychain_service: str = "mao-agent-deepseek"

    @classmethod
    def from_dict(cls, value: Optional[Dict[str, Any]] = None) -> "LLMConfig":
        value = dict(value or {})
        env_map = {
            "base_url": "MAO_LLM_BASE_URL",
            "model": "MAO_LLM_MODEL",
            "api_key_env": "MAO_LLM_API_KEY_ENV",
            "provider": "MAO_LLM_PROVIDER",
            "reasoning_effort": "MAO_LLM_REASONING_EFFORT",
        }
        for key, env_name in env_map.items():
            if os.getenv(env_name):
                value[key] = os.environ[env_name]
        for key in ("temperature", "max_tokens", "timeout_s", "max_steps"):
            env_name = "MAO_LLM_" + key.upper()
            if os.getenv(env_name):
                raw = os.environ[env_name]
                value[key] = float(raw) if key == "temperature" else int(raw)
        for key in ("stream", "reasoning", "show_reasoning"):
            env_name = "MAO_LLM_" + key.upper()
            if os.getenv(env_name) is not None:
                value[key] = os.environ[env_name].lower() in {"1", "true", "yes", "on"}
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in value.items() if k in allowed})

    def api_key(self) -> str:
        value = os.getenv(self.api_key_env, "")
        if not value and self.keychain_service:
            try:
                proc = subprocess.run(
                    ["security", "find-generic-password", "-s", self.keychain_service, "-a", "api", "-w"],
                    capture_output=True, text=True, timeout=5,
                )
                if proc.returncode == 0:
                    value = proc.stdout.strip()
            except (OSError, subprocess.SubprocessError):
                pass
        if not value:
            raise LLMError("未找到大模型密钥，请设置环境变量 " + self.api_key_env)
        return value

    def endpoint(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"


def _json_line(line: bytes) -> Optional[Dict[str, Any]]:
    text = line.decode("utf-8", "replace").strip()
    if not text or text.startswith(":"):
        return None
    if text.startswith("data:"):
        text = text[5:].strip()
    if text == "[DONE]":
        return {"__done__": True}
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {"__raw__": text}
    return value if isinstance(value, dict) else {"data": value}


def iter_sse(stream: Iterable[bytes]) -> Iterator[Dict[str, Any]]:
    """Yield normalized events from an SSE byte stream."""
    for line in stream:
        event = _json_line(line)
        if event is None:
            continue
        if event.get("__done__"):
            yield {"type": "done"}
            return
        if "__raw__" in event:
            yield {"type": "raw", "data": event["__raw__"]}
            continue
        choices = event.get("choices") or []
        if not choices:
            yield {"type": "raw", "data": event}
            continue
        delta = choices[0].get("delta") or {}
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if reasoning:
            yield {"type": "reasoning", "text": str(reasoning), "raw": event}
        content = delta.get("content")
        if content:
            yield {"type": "content", "text": str(content), "raw": event}
        tool_calls = delta.get("tool_calls") or []
        if tool_calls:
            yield {"type": "tool_call_delta", "tool_calls": tool_calls, "raw": event}
        finish = choices[0].get("finish_reason")
        if finish:
            yield {"type": "finish", "reason": finish, "raw": event}


@dataclass
class LLMResponse:
    content: str = ""
    reasoning: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)
    latency_s: float = 0.0


class OpenAICompatibleClient:
    def __init__(self, config: LLMConfig, opener=None):
        self.config = config
        self.opener = opener or request.urlopen

    def _payload(self, messages, tools=None, response_format=None, stream=None):
        payload: Dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": self.config.stream if stream is None else stream,
        }
        if self.config.reasoning and self.config.reasoning_effort:
            payload["reasoning_effort"] = self.config.reasoning_effort
        if self.config.reasoning:
            # DeepSeek-compatible gateways accept this flag; gateways that
            # ignore it still receive the portable reasoning_effort field.
            payload["thinking"] = {"type": "enabled"}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if response_format:
            payload["response_format"] = response_format
        if payload['stream']:
            payload['stream_options'] = {'include_usage': True}
        return payload

    def _request(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(self.config.endpoint(), data=body, method="POST")
        credential = self.config.api_key()
        req.add_header("Authorization", "Bearer " + credential)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "text/event-stream" if payload.get("stream") else "application/json")
        before_model_request(payload, self.config)
        try:
            return self.opener(req, timeout=self.config.timeout_s)
        except error.HTTPError as exc:
            detail = exc.read(2000).decode("utf-8", "replace").replace(credential, "[redacted]")
            detail = re.sub(r"\b(?:sk|fc)-[A-Za-z0-9_-]+", "[redacted]", detail)
            raise LLMError("LLM HTTP %s: %s" % (exc.code, detail)) from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            raise LLMError("LLM 网络请求失败: " + str(exc)) from exc

    def stream_chat(self, messages, tools=None, response_format=None) -> Iterator[Dict[str, Any]]:
        started = time.monotonic()
        usage, status, provider_id = None, 'error', None
        try:
            with self._request(self._payload(messages, tools, response_format, stream=True)) as response:
                for event in iter_sse(response):
                    raw = event.get('raw') or event.get('data') or {}
                    if isinstance(raw, dict) and isinstance(raw.get('usage'), dict):
                        usage = raw['usage']
                    if isinstance(raw, dict) and isinstance(raw.get('id'), str):
                        provider_id = raw['id']
                    event.setdefault("elapsed_s", time.monotonic() - started)
                    yield event
            status = 'ok'
        finally:
            record_model_call(usage, (time.monotonic() - started) * 1000, status, provider_request_id=provider_id)

    def chat(self, messages, tools=None, response_format=None) -> LLMResponse:
        started = time.monotonic()
        if self.config.stream:
            content, reasoning, tool_deltas, raw = [], [], [], {}
            for event in self.stream_chat(messages, tools, response_format):
                if event["type"] == "content":
                    content.append(event["text"])
                elif event["type"] == "reasoning":
                    reasoning.append(event["text"])
                elif event["type"] == "tool_call_delta":
                    tool_deltas.extend(event.get("tool_calls", []))
                elif event["type"] == "raw":
                    raw = event.get("data", {}) if isinstance(event.get("data"), dict) else raw
            return LLMResponse("".join(content), "".join(reasoning), self._merge_tool_deltas(tool_deltas), raw, time.monotonic() - started)
        data, status = {}, 'error'
        try:
            with self._request(self._payload(messages, tools, response_format, stream=False)) as response:
                try:
                    data = json.loads(response.read().decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise LLMError("LLM 返回不是有效 JSON") from exc
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            status = 'ok'
            return LLMResponse(message.get("content") or "", message.get("reasoning_content") or message.get("reasoning") or "", message.get("tool_calls") or [], data, time.monotonic() - started)
        finally:
            record_model_call(data.get('usage'), (time.monotonic() - started) * 1000, status, provider_request_id=data.get('id'))

    @staticmethod
    def _merge_tool_deltas(deltas):
        merged: Dict[int, Dict[str, Any]] = {}
        for delta in deltas:
            idx = int(delta.get("index", len(merged)))
            item = merged.setdefault(idx, {"id": delta.get("id"), "type": "function", "function": {"name": "", "arguments": ""}})
            fn = delta.get("function") or {}
            if fn.get("name"):
                item["function"]["name"] += fn["name"]
            if fn.get("arguments"):
                item["function"]["arguments"] += fn["arguments"]
            if delta.get("id"):
                item["id"] = delta["id"]
        return list(merged.values())


def parse_json_object(text: str) -> Dict[str, Any]:
    """Parse a JSON object even if a compatible model wrapped it in a code fence."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            value = json.loads(text[start:end + 1])
        else:
            raise LLMError("模型没有返回有效JSON: " + str(exc)) from exc
    if not isinstance(value, dict):
        raise LLMError("模型JSON顶层必须是对象")
    return value
