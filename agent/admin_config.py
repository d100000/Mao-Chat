"""Admin-managed Agent configuration with secret redaction.

Non-secret provider settings live in the existing SQLite database. API keys
are written to macOS Keychain when available, with a 0600 sidecar fallback;
they are never returned by the admin API or written to normal settings rows.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Optional

from .db import connect, ensure_schema
from .runtime import AgentConfig


ALLOWED_REASONING = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}


class AdminConfigError(ValueError):
    pass


class AdminConfigStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.secret_path = Path(os.environ.get("MAO_ADMIN_SECRET_FILE", str(self.db_path) + ".provider-secrets"))
        conn = connect(self.db_path)
        ensure_schema(conn)
        conn.close()

    def _read_settings(self) -> Dict[str, Any]:
        conn = connect(self.db_path)
        try:
            rows = conn.execute("SELECT setting_key,value_json FROM app_settings WHERE is_secret=0").fetchall()
            out = {}
            for row in rows:
                try:
                    out[row["setting_key"]] = json.loads(row["value_json"])
                except json.JSONDecodeError:
                    continue
            return out
        finally:
            conn.close()

    def _write(self, values: Dict[str, Any], admin_id: Optional[int] = None) -> None:
        conn = connect(self.db_path)
        try:
            with conn:
                for key, value in values.items():
                    conn.execute("INSERT INTO app_settings(setting_key,value_json,is_secret,updated_by) VALUES(?,?,0,?) ON CONFLICT(setting_key) DO UPDATE SET value_json=excluded.value_json,is_secret=0,updated_by=excluded.updated_by,updated_at=CURRENT_TIMESTAMP", (key, json.dumps(value, ensure_ascii=False, sort_keys=True), admin_id))
        finally:
            conn.close()

    def _read_secrets(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        try:
            if self.secret_path.exists():
                value = json.loads(self.secret_path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    out = {str(k): str(v) for k, v in value.items() if isinstance(v, str)}
        except (OSError, json.JSONDecodeError):
            pass
        return out

    def _keychain_get(self, service: str) -> str:
        if not shutil.which("security"):
            return ""
        try:
            p = subprocess.run(["security", "find-generic-password", "-s", service, "-a", "api", "-w"], capture_output=True, text=True, timeout=5)
            return p.stdout.strip() if p.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    def _keychain_set(self, service: str, value: str) -> bool:
        if not shutil.which("security"):
            return False
        try:
            p = subprocess.run(["security", "add-generic-password", "-U", "-s", service, "-a", "api", "-w", value], capture_output=True, text=True, timeout=5)
            return p.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _keychain_delete(self, service: str) -> None:
        if not shutil.which("security"):
            return
        try:
            subprocess.run(["security", "delete-generic-password", "-s", service, "-a", "api"], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass

    def _set_secret(self, key: str, value: str, service: str) -> None:
        if not isinstance(value, str) or len(value) > 512:
            raise AdminConfigError("密钥格式无效")
        if value and self._keychain_set(service, value):
            # Remove any older plaintext fallback.  Otherwise a later
            # Keychain outage could silently resurrect the previous key.
            secrets = self._read_secrets()
            if key in secrets:
                secrets.pop(key, None)
                if secrets:
                    self.secret_path.write_text(json.dumps(secrets, ensure_ascii=False), encoding="utf-8")
                    try:
                        os.chmod(self.secret_path, 0o600)
                    except OSError:
                        pass
                else:
                    try:
                        self.secret_path.unlink()
                    except FileNotFoundError:
                        pass
            return
        if not value:
            self._keychain_delete(service)
        secrets = self._read_secrets()
        if value:
            secrets[key] = value
        else:
            secrets.pop(key, None)
        self.secret_path.parent.mkdir(parents=True, exist_ok=True)
        self.secret_path.write_text(json.dumps(secrets, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(self.secret_path, 0o600)
        except OSError:
            pass

    def _secret_value(self, key: str, service: str, env_name: str) -> str:
        return self._keychain_get(service) or self._read_secrets().get(key, "") or os.environ.get(env_name, "")

    def public(self) -> Dict[str, Any]:
        defaults = AgentConfig.load()
        settings = self._read_settings()
        llm = {k: v for k, v in asdict(defaults.llm).items() if k not in {"keychain_service"}}
        llm.update(settings.get("llm", {}))
        fire = {k: v for k, v in asdict(defaults.firecrawl).items()}
        fire.update(settings.get("firecrawl", {}))
        agent = {k: v for k, v in asdict(defaults).items() if k in {"max_steps", "local_limit", "web_limit"}}
        agent.update(settings.get("agent", {}))
        llm_set = bool(self._secret_value("llm_api_key", llm.get("keychain_service", "mao-agent-deepseek"), llm.get("api_key_env", "MAO_LLM_API_KEY")))
        fire_set = bool(self._secret_value("firecrawl_api_key", fire.get("keychain_service", "mao-agent-firecrawl"), fire.get("api_key_env", "FIRECRAWL_API_KEY")))
        return {"llm": {**llm, "api_key_set": llm_set, "configured": llm_set}, "firecrawl": {**fire, "api_key_set": fire_set, "configured": fire_set}, "agent": agent}

    def load_agent_config(self) -> AgentConfig:
        cfg = AgentConfig.load()
        values = self._read_settings()
        llm = values.get("llm", {}); fire = values.get("firecrawl", {})
        for key in ("provider", "base_url", "model", "api_key_env", "temperature", "max_tokens", "timeout_s", "stream", "reasoning", "reasoning_effort", "show_reasoning", "max_steps", "keychain_service"):
            if key in llm and hasattr(cfg.llm, key): setattr(cfg.llm, key, llm[key])
        for key in ("base_url", "timeout_s", "limit", "keychain_service"):
            if key in fire and hasattr(cfg.firecrawl, key): setattr(cfg.firecrawl, key, fire[key])
        llm_secret = self._secret_value("llm_api_key", cfg.llm.keychain_service, cfg.llm.api_key_env)
        if llm_secret:
            os.environ[cfg.llm.api_key_env] = llm_secret
        for key in ("api_key_env",):
            if key in fire and hasattr(cfg.firecrawl, key):
                setattr(cfg.firecrawl, key, fire[key])
        fire_secret = self._secret_value("firecrawl_api_key", cfg.firecrawl.keychain_service, cfg.firecrawl.api_key_env)
        if fire_secret:
            os.environ[cfg.firecrawl.api_key_env] = fire_secret
        agent_values = values.get("agent", {})
        for key in ("max_steps", "local_limit", "web_limit"):
            if key in agent_values and hasattr(cfg, key):
                setattr(cfg, key, int(agent_values[key]))
        return cfg

    def update(self, payload: Dict[str, Any], admin_id: Optional[int] = None) -> Dict[str, Any]:
        values = self._validated_values(payload)
        llm = payload.get("llm", {})
        fire = payload.get("firecrawl", {})
        if isinstance(llm, dict) and isinstance(llm.get("api_key"), str):
            self._set_secret("llm_api_key", llm["api_key"], values.get("llm", {}).get("keychain_service", "mao-agent-deepseek"))
        if isinstance(fire, dict) and isinstance(fire.get("api_key"), str):
            self._set_secret("firecrawl_api_key", fire["api_key"], values.get("firecrawl", {}).get("keychain_service", "mao-agent-firecrawl"))
        if values:
            self._write(values, admin_id)
        return self.public()

    def validate(self, payload: Dict[str, Any]) -> None:
        """Validate a proposed config without persisting or contacting providers."""
        self._validated_values(payload)

    @staticmethod
    def _validated_values(payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict): raise AdminConfigError("配置必须是对象")
        values: Dict[str, Any] = {}
        llm = payload.get("llm", {})
        if llm:
            if not isinstance(llm, dict): raise AdminConfigError("LLM配置无效")
            allowed = {"provider", "base_url", "model", "api_key_env", "temperature", "max_tokens", "timeout_s", "stream", "reasoning", "reasoning_effort", "show_reasoning", "max_steps", "keychain_service"}
            out = {k: llm[k] for k in allowed if k in llm}
            if "base_url" in out and (not isinstance(out["base_url"], str) or not out["base_url"].startswith(("https://", "http://"))): raise AdminConfigError("LLM地址必须是HTTP(S) URL")
            if "temperature" in out and not 0 <= float(out["temperature"]) <= 2: raise AdminConfigError("temperature范围是0至2")
            if "max_tokens" in out and not 128 <= int(out["max_tokens"]) <= 32768: raise AdminConfigError("max_tokens范围无效")
            if "timeout_s" in out and not 5 <= int(out["timeout_s"]) <= 600: raise AdminConfigError("timeout范围无效")
            if "reasoning_effort" in out and out["reasoning_effort"] not in ALLOWED_REASONING: raise AdminConfigError("reasoning_effort无效")
            values["llm"] = out
        fire = payload.get("firecrawl", {})
        if fire:
            if not isinstance(fire, dict): raise AdminConfigError("Firecrawl配置无效")
            allowed = {"base_url", "timeout_s", "limit", "keychain_service"}; out = {k: fire[k] for k in allowed if k in fire}
            if "base_url" in out and (not isinstance(out["base_url"], str) or not out["base_url"].startswith(("https://", "http://"))): raise AdminConfigError("Firecrawl地址必须是HTTP(S) URL")
            if "timeout_s" in out and not 5 <= int(out["timeout_s"]) <= 600: raise AdminConfigError("Firecrawl timeout范围无效")
            if "limit" in out and not 1 <= int(out["limit"]) <= 20: raise AdminConfigError("Firecrawl limit范围无效")
            values["firecrawl"] = out
        agent = payload.get("agent", {})
        if agent:
            if not isinstance(agent, dict): raise AdminConfigError("Agent配置无效")
            out = {k: agent[k] for k in ("max_steps", "local_limit", "web_limit") if k in agent}
            for key, low, high in (("max_steps", 1, 20), ("local_limit", 1, 30), ("web_limit", 1, 20)):
                if key in out and not low <= int(out[key]) <= high: raise AdminConfigError(key + "范围无效")
            values["agent"] = {k: int(v) for k, v in out.items()}
        return values
