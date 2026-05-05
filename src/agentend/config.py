from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentend.core.init import DEFAULT_CONFIG


@dataclass(frozen=True)
class LLMProviderConfig:
    api_key_env: str
    base_url: str


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    model: str
    temperature: float
    max_tokens: int
    provider_config: LLMProviderConfig


@dataclass(frozen=True)
class DataConfig:
    db_path: str
    artifact_dir: str
    log_dir: str
    agent_profile_path: str
    workflow_dir: str


@dataclass(frozen=True)
class AppConfig:
    home: Path
    llm: LLMConfig
    data: DataConfig

    def resolve_home_path(self, value: str) -> Path:
        path = Path(value)
        if path.is_absolute():
            return path
        return (self.home / path).resolve()


def load_config(home: Path) -> AppConfig:
    resolved_home = home.expanduser().resolve()
    config_path = resolved_home / "config.toml"
    if config_path.exists():
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    else:
        raw = tomllib.loads(DEFAULT_CONFIG)

    llm_section = raw.get("llm", {})
    provider = str(llm_section.get("provider", "openai"))
    providers = llm_section.get("providers", {})
    provider_raw = providers.get(provider, {})
    provider_config = LLMProviderConfig(
        api_key_env=str(provider_raw.get("api_key_env", f"{provider.upper()}_API_KEY")),
        base_url=str(provider_raw.get("base_url", "https://api.openai.com/v1")),
    )
    llm = LLMConfig(
        provider=provider,
        model=str(llm_section.get("model", "gpt-4.1")),
        temperature=float(llm_section.get("temperature", 0.2)),
        max_tokens=int(llm_section.get("max_tokens", 4096)),
        provider_config=provider_config,
    )
    data_section = raw.get("data", {})
    data = DataConfig(
        db_path=str(data_section.get("db_path", "./data/agentend.sqlite")),
        artifact_dir=str(data_section.get("artifact_dir", "./data/artifacts")),
        log_dir=str(data_section.get("log_dir", "./data/logs")),
        agent_profile_path=str(data_section.get("agent_profile_path", "./agent.md")),
        workflow_dir=str(data_section.get("workflow_dir", "./workflows/definitions")),
    )
    _load_env_file(resolved_home / ".env")
    return AppConfig(home=resolved_home, llm=llm, data=data)


def set_llm_config(home: Path, provider: str, model: str) -> AppConfig:
    config = load_config(home)
    config_path = config.home / "config.toml"
    provider_config = config.llm.provider_config
    raw = {
        "llm": {
            "provider": provider,
            "model": model,
            "temperature": config.llm.temperature,
            "max_tokens": config.llm.max_tokens,
            "providers": {
                provider: {
                    "api_key_env": provider_config.api_key_env
                    if provider == config.llm.provider
                    else f"{provider.upper()}_API_KEY",
                    "base_url": provider_config.base_url
                    if provider == config.llm.provider
                    else "https://api.openai.com/v1",
                }
            },
        },
        "data": {
            "db_path": config.data.db_path,
            "artifact_dir": config.data.artifact_dir,
            "log_dir": config.data.log_dir,
            "agent_profile_path": config.data.agent_profile_path,
            "workflow_dir": config.data.workflow_dir,
        },
    }
    config_path.write_text(_dump_toml(raw), encoding="utf-8")
    return load_config(home)


def _dump_toml(raw: dict[str, Any]) -> str:
    llm = raw["llm"]
    data = raw["data"]
    provider = llm["provider"]
    provider_config = llm["providers"][provider]
    return (
        "[llm]\n"
        f'provider = "{provider}"\n'
        f'model = "{llm["model"]}"\n'
        f"temperature = {llm['temperature']}\n"
        f"max_tokens = {llm['max_tokens']}\n\n"
        f"[llm.providers.{provider}]\n"
        f'api_key_env = "{provider_config["api_key_env"]}"\n'
        f'base_url = "{provider_config["base_url"]}"\n\n'
        "[telegram]\n"
        "enabled = false\n"
        'bot_token_env = "TELEGRAM_BOT_TOKEN"\n\n'
        "[data]\n"
        f'db_path = "{data["db_path"]}"\n'
        f'artifact_dir = "{data["artifact_dir"]}"\n'
        f'log_dir = "{data["log_dir"]}"\n'
        f'agent_profile_path = "{data["agent_profile_path"]}"\n'
        f'workflow_dir = "{data["workflow_dir"]}"\n'
    )


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
