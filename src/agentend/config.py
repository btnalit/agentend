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
    providers: dict[str, LLMProviderConfig]


@dataclass(frozen=True)
class DataConfig:
    db_path: str
    artifact_dir: str
    log_dir: str
    agent_profile_path: str
    workflow_dir: str


@dataclass(frozen=True)
class SearchProviderConfig:
    api_key_env: str
    base_url: str


@dataclass(frozen=True)
class SearchConfig:
    provider: str
    providers: dict[str, SearchProviderConfig]


@dataclass(frozen=True)
class VisionProviderConfig:
    api_key_env: str
    base_url: str
    model: str


@dataclass(frozen=True)
class VisionConfig:
    provider: str
    providers: dict[str, VisionProviderConfig]


@dataclass(frozen=True)
class AppConfig:
    home: Path
    llm: LLMConfig
    data: DataConfig
    search: SearchConfig
    vision: VisionConfig

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
    provider_configs = {
        str(name): LLMProviderConfig(
            api_key_env=str(provider_raw.get("api_key_env", f"{str(name).upper()}_API_KEY")),
            base_url=str(provider_raw.get("base_url", "https://api.openai.com/v1")),
        )
        for name, provider_raw in providers.items()
        if isinstance(provider_raw, dict)
    }
    provider_configs.setdefault("fake", LLMProviderConfig(api_key_env="", base_url=""))
    provider_configs.setdefault("openai", LLMProviderConfig(api_key_env="OPENAI_API_KEY", base_url="https://api.openai.com/v1"))
    provider_config = provider_configs.get(
        provider,
        LLMProviderConfig(api_key_env=f"{provider.upper()}_API_KEY", base_url="https://api.openai.com/v1"),
    )
    llm = LLMConfig(
        provider=provider,
        model=str(llm_section.get("model", "gpt-4.1")),
        temperature=float(llm_section.get("temperature", 0.2)),
        max_tokens=int(llm_section.get("max_tokens", 4096)),
        provider_config=provider_config,
        providers=provider_configs,
    )
    data_section = raw.get("data", {})
    data = DataConfig(
        db_path=str(data_section.get("db_path", "./data/agentend.sqlite")),
        artifact_dir=str(data_section.get("artifact_dir", "./data/artifacts")),
        log_dir=str(data_section.get("log_dir", "./data/logs")),
        agent_profile_path=str(data_section.get("agent_profile_path", "./agent.md")),
        workflow_dir=str(data_section.get("workflow_dir", "./workflows/definitions")),
    )
    search_section = raw.get("search", {})
    search_providers_raw = search_section.get("providers", {})
    search_providers = {
        str(name): SearchProviderConfig(
            api_key_env=str(provider_raw.get("api_key_env", "")),
            base_url=str(provider_raw.get("base_url", "")),
        )
        for name, provider_raw in search_providers_raw.items()
        if isinstance(provider_raw, dict)
    }
    search_providers.setdefault("fake", SearchProviderConfig(api_key_env="", base_url=""))
    search_providers.setdefault(
        "brave",
        SearchProviderConfig(
            api_key_env="BRAVE_SEARCH_API_KEY",
            base_url="https://api.search.brave.com/res/v1/web/search",
        ),
    )
    search = SearchConfig(provider=str(search_section.get("provider", "fake")), providers=search_providers)
    vision_section = raw.get("vision", {})
    vision_providers_raw = vision_section.get("providers", {})
    vision_providers = {
        str(name): VisionProviderConfig(
            api_key_env=str(provider_raw.get("api_key_env", "")),
            base_url=str(provider_raw.get("base_url", "")),
            model=str(provider_raw.get("model", "")),
        )
        for name, provider_raw in vision_providers_raw.items()
        if isinstance(provider_raw, dict)
    }
    vision_providers.setdefault("fake", VisionProviderConfig(api_key_env="", base_url="", model="fake-vision"))
    vision_providers.setdefault(
        "openai",
        VisionProviderConfig(
            api_key_env="OPENAI_API_KEY",
            base_url="https://api.openai.com/v1",
            model="gpt-4.1",
        ),
    )
    vision_providers.setdefault(
        "gemini",
        VisionProviderConfig(
            api_key_env="GEMINI_API_KEY",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            model="gemini-2.5-flash",
        ),
    )
    vision = VisionConfig(provider=str(vision_section.get("provider", "fake")), providers=vision_providers)
    _load_env_file(resolved_home / ".env")
    return AppConfig(home=resolved_home, llm=llm, data=data, search=search, vision=vision)


def set_llm_config(home: Path, provider: str, model: str) -> AppConfig:
    config = load_config(home)
    config_path = config.home / "config.toml"
    provider_config = config.llm.provider_config
    if provider == "fake":
        next_provider_config = {"api_key_env": "", "base_url": ""}
    elif provider == config.llm.provider:
        next_provider_config = {
            "api_key_env": provider_config.api_key_env,
            "base_url": provider_config.base_url,
        }
    else:
        next_provider_config = {
            "api_key_env": f"{provider.upper()}_API_KEY",
            "base_url": "https://api.openai.com/v1",
        }
    providers = {
        name: {"api_key_env": item.api_key_env, "base_url": item.base_url}
        for name, item in config.llm.providers.items()
    }
    providers[provider] = next_provider_config
    raw = {
        "llm": {
            "provider": provider,
            "model": model,
            "temperature": config.llm.temperature,
            "max_tokens": config.llm.max_tokens,
            "providers": providers,
        },
        "data": {
            "db_path": config.data.db_path,
            "artifact_dir": config.data.artifact_dir,
            "log_dir": config.data.log_dir,
            "agent_profile_path": config.data.agent_profile_path,
            "workflow_dir": config.data.workflow_dir,
        },
        "search": {
            "provider": config.search.provider,
            "providers": {
                name: {"api_key_env": provider.api_key_env, "base_url": provider.base_url}
                for name, provider in config.search.providers.items()
            },
        },
        "vision": {
            "provider": config.vision.provider,
            "providers": {
                name: {
                    "api_key_env": provider.api_key_env,
                    "base_url": provider.base_url,
                    "model": provider.model,
                }
                for name, provider in config.vision.providers.items()
            },
        },
    }
    config_path.write_text(_dump_toml(raw), encoding="utf-8")
    return load_config(home)


def _dump_toml(raw: dict[str, Any]) -> str:
    llm = raw["llm"]
    data = raw["data"]
    provider = llm["provider"]
    provider_configs = dict(llm.get("providers", {}))
    llm_provider_lines: list[str] = []
    for name, provider_config in provider_configs.items():
        llm_provider_lines.extend(
            [
                f"[llm.providers.{name}]",
                f'api_key_env = "{provider_config.get("api_key_env", "")}"',
                f'base_url = "{provider_config.get("base_url", "")}"',
                "",
            ]
        )
    search = raw.get("search", {"provider": "fake", "providers": {"fake": {"api_key_env": "", "base_url": ""}}})
    search_lines = ["[search]", f'provider = "{search.get("provider", "fake")}"', ""]
    for name, search_provider in dict(search.get("providers", {})).items():
        search_lines.extend(
            [
                f"[search.providers.{name}]",
                f'api_key_env = "{search_provider.get("api_key_env", "")}"',
                f'base_url = "{search_provider.get("base_url", "")}"',
                "",
            ]
        )
    vision = raw.get("vision", {"provider": "fake", "providers": {"fake": {"api_key_env": "", "base_url": "", "model": "fake-vision"}}})
    vision_lines = ["[vision]", f'provider = "{vision.get("provider", "fake")}"', ""]
    for name, vision_provider in dict(vision.get("providers", {})).items():
        vision_lines.extend(
            [
                f"[vision.providers.{name}]",
                f'api_key_env = "{vision_provider.get("api_key_env", "")}"',
                f'base_url = "{vision_provider.get("base_url", "")}"',
                f'model = "{vision_provider.get("model", "")}"',
                "",
            ]
        )
    return (
        "[llm]\n"
        f'provider = "{provider}"\n'
        f'model = "{llm["model"]}"\n'
        f"temperature = {llm['temperature']}\n"
        f"max_tokens = {llm['max_tokens']}\n\n"
        + "\n".join(llm_provider_lines)
        + "\n"
        "[telegram]\n"
        "enabled = false\n"
        'bot_token_env = "TELEGRAM_BOT_TOKEN"\n\n'
        + "\n".join(search_lines)
        + "\n"
        + "\n".join(vision_lines)
        + "\n"
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
