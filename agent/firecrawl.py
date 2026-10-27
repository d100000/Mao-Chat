#!/usr/bin/env python3
"""Firecrawl v2 search adapter.

Web pages are untrusted research candidates.  The caller must never treat
their content as agent instructions or as a replacement for local primary
source verification.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional
from urllib import error, request


class FirecrawlError(RuntimeError):
    pass


@dataclass
class FirecrawlConfig:
    api_key_env: str = "FIRECRAWL_API_KEY"
    base_url: str = "https://api.firecrawl.dev/v2"
    timeout_s: int = 60
    limit: int = 5
    keychain_service: str = "mao-agent-firecrawl"

    @classmethod
    def from_dict(cls, value=None):
        value = dict(value or {})
        if os.getenv("FIRECRAWL_BASE_URL"):
            value["base_url"] = os.environ["FIRECRAWL_BASE_URL"]
        if os.getenv("FIRECRAWL_API_KEY_ENV"):
            value["api_key_env"] = os.environ["FIRECRAWL_API_KEY_ENV"]
        if os.getenv("FIRECRAWL_TIMEOUT_S"):
            value["timeout_s"] = int(os.environ["FIRECRAWL_TIMEOUT_S"])
        return cls(**{k: v for k, v in value.items() if k in cls.__dataclass_fields__})

    def api_key(self):
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
            raise FirecrawlError("未找到 Firecrawl 密钥，请设置环境变量 " + self.api_key_env)
        return value


class FirecrawlClient:
    def __init__(self, config=None, opener=None):
        self.config = config or FirecrawlConfig.from_dict()
        self.opener = opener or request.urlopen

    def search(self, query: str, limit: Optional[int] = None, include_markdown=True, sources: Optional[List[str]] = None) -> Dict[str, Any]:
        if not query.strip():
            raise ValueError("Firecrawl 搜索词不能为空")
        limit = max(1, min(int(limit or self.config.limit), 20))
        payload: Dict[str, Any] = {"query": query, "limit": limit}
        if include_markdown:
            payload["scrapeOptions"] = {"formats": [{"type": "markdown"}]}
        if sources:
            payload["sources"] = sources
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        endpoint = self.config.base_url.rstrip("/") + "/search"
        req = request.Request(endpoint, data=body, method="POST")
        req.add_header("Authorization", "Bearer " + self.config.api_key())
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        try:
            with self.opener(req, timeout=self.config.timeout_s) as response:
                raw = response.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read(2000).decode("utf-8", "replace")
            raise FirecrawlError("Firecrawl HTTP %s: %s" % (exc.code, detail)) from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            raise FirecrawlError("Firecrawl 网络请求失败: " + str(exc)) from exc
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FirecrawlError("Firecrawl 返回不是 JSON") from exc
        if not isinstance(result, dict):
            raise FirecrawlError("Firecrawl 返回格式错误")
        results = result.get("results") or []
        data = result.get("data")
        if isinstance(data, list):
            results = results or data
        elif isinstance(data, dict):
            # Firecrawl v2 currently returns web/news/images buckets.  Keep
            # all text-searchable buckets; images have no page text and are
            # intentionally excluded from the agent's citation context.
            results = results or (data.get("web") or []) + (data.get("news") or []) + (data.get("research") or [])
        normalized = []
        for item in results if isinstance(results, list) else []:
            if not isinstance(item, dict):
                continue
            normalized.append({
                "url": item.get("url", ""),
                "title": item.get("title", ""),
                "description": item.get("description") or item.get("snippet", ""),
                "markdown": item.get("markdown") or item.get("content", ""),
                "source": "firecrawl",
                "untrusted": True,
            })
        return {"query": query, "results": normalized, "raw": result, "untrusted": True}
