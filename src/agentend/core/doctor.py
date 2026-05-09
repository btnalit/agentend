from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from agentend.config import load_config
from agentend.core.llm_router import LLMRouter
from agentend.db.models import MCPServer, SkillMarket
from agentend.db.session import init_database
from agentend.db.session import session_scope
from agentend.tools.browser import playwright_status


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    message: str
    fix_hint: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "fix_hint": self.fix_hint,
        }


def run_doctor(home: Path) -> list[DoctorCheck]:
    resolved = home.expanduser().resolve()
    checks = [
        DoctorCheck("python", "ok", sys.version.split()[0]),
        _check_imports(),
        _check_home(resolved),
        _check_sqlite(resolved),
        _check_writable_dir(resolved, "artifacts", load_config(resolved).data.artifact_dir),
        _check_writable_dir(resolved, "sandboxes", "./data/sandboxes"),
        _check_llm(resolved),
        _check_telegram(resolved),
        _check_mcp_servers(resolved),
        _check_search(resolved),
        _check_skill_markets(resolved),
        _check_browser(),
        _check_vision(resolved),
        _check_subprocess(),
    ]
    return checks


def doctor_json(home: Path) -> str:
    return json.dumps([check.to_dict() for check in run_doctor(home)], ensure_ascii=False, indent=2)


def _check_imports() -> DoctorCheck:
    missing = []
    for module in ["typer", "sqlalchemy", "yaml", "httpx"]:
        try:
            importlib.import_module(module)
        except Exception:
            missing.append(module)
    if missing:
        return DoctorCheck("dependencies", "error", f"missing: {', '.join(missing)}", "Install project dependencies.")
    return DoctorCheck("dependencies", "ok", "required imports are available")


def _check_home(home: Path) -> DoctorCheck:
    if not home.exists():
        return DoctorCheck("home", "error", f"{home} does not exist", "Run agentend init.")
    if not (home / "config.toml").exists():
        return DoctorCheck("home", "warning", "config.toml not found", "Run agentend init.")
    return DoctorCheck("home", "ok", str(home))


def _check_sqlite(home: Path) -> DoctorCheck:
    try:
        path = init_database(home)
        return DoctorCheck("sqlite", "ok", str(path))
    except Exception as exc:
        return DoctorCheck("sqlite", "error", str(exc), "Check data directory permissions.")


def _check_writable_dir(home: Path, name: str, configured_path: str) -> DoctorCheck:
    config = load_config(home)
    root = config.resolve_home_path(configured_path)
    probe = root / f".doctor-write-{uuid4().hex}.tmp"
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return DoctorCheck(name, "ok", str(root))
    except Exception as exc:
        return DoctorCheck(name, "error", f"{root}: {exc}", "Check data directory permissions.")


def _check_llm(home: Path) -> DoctorCheck:
    result = LLMRouter(load_config(home)).test()
    return DoctorCheck("llm", "ok" if result.ok else "warning", result.message, None if result.ok else "Set the provider API key.")


def _check_telegram(home: Path) -> DoctorCheck:
    load_config(home)
    token_env = "TELEGRAM_BOT_TOKEN"
    if os.environ.get(token_env):
        return DoctorCheck("telegram", "ok", f"{token_env} is set")
    return DoctorCheck("telegram", "warning", f"{token_env} is not set", f"Set {token_env} before running telegram serve.")


def _check_mcp_servers(home: Path) -> DoctorCheck:
    try:
        with session_scope(home) as session:
            servers = session.execute(select(MCPServer).where(MCPServer.enabled == "true").order_by(MCPServer.name)).scalars().all()
    except Exception as exc:
        return DoctorCheck("mcp_servers", "error", str(exc), "Run agentend db init.")
    if not servers:
        return DoctorCheck("mcp_servers", "ok", "no enabled MCP servers configured")
    unhealthy = [server for server in servers if server.status == "unhealthy"]
    unknown = [server for server in servers if server.status == "unknown"]
    if unhealthy:
        names = ", ".join(_server_status_message(server) for server in unhealthy)
        return DoctorCheck("mcp_servers", "warning", f"unhealthy: {names}", "Run agentend mcp refresh <name>.")
    if unknown:
        names = ", ".join(server.name for server in unknown)
        return DoctorCheck("mcp_servers", "warning", f"not refreshed: {names}", "Run agentend mcp refresh <name>.")
    return DoctorCheck("mcp_servers", "ok", f"{len(servers)} enabled MCP server(s) healthy")


def _check_search(home: Path) -> DoctorCheck:
    config = load_config(home)
    provider_name = config.search.provider
    provider = config.search.providers.get(provider_name)
    if provider is None:
        return DoctorCheck("search", "warning", f"unknown provider: {provider_name}", "Set [search].provider to fake or a configured provider.")
    if provider_name == "fake":
        return DoctorCheck("search", "ok", "fake provider is configured for offline search")
    if not provider.base_url:
        return DoctorCheck("search", "warning", f"{provider_name} has no base_url", "Set search provider base_url.")
    if not provider.api_key_env:
        return DoctorCheck("search", "warning", f"{provider_name} has no api_key_env", "Set search provider api_key_env.")
    if not os.environ.get(provider.api_key_env):
        return DoctorCheck("search", "warning", f"{provider_name} secret is not set: {provider.api_key_env}", f"Set {provider.api_key_env}.")
    return DoctorCheck("search", "ok", f"{provider_name} is configured")


def _check_skill_markets(home: Path) -> DoctorCheck:
    try:
        with session_scope(home) as session:
            markets = session.execute(select(SkillMarket).where(SkillMarket.enabled == "true").order_by(SkillMarket.name)).scalars().all()
    except Exception as exc:
        return DoctorCheck("skill_markets", "error", str(exc), "Run agentend db init.")
    if not markets:
        return DoctorCheck("skill_markets", "ok", "no enabled skill markets configured")
    missing: list[str] = []
    for market in markets:
        if market.backend == "git" and not Path(market.location).exists():
            continue
        path = Path(market.location)
        if not path.is_absolute():
            path = (home / path).resolve()
        if not path.exists():
            missing.append(f"{market.name}: {path}")
    if missing:
        return DoctorCheck("skill_markets", "warning", f"missing paths: {', '.join(missing)}", "Update or remove missing skill markets.")
    return DoctorCheck("skill_markets", "ok", f"{len(markets)} enabled skill market(s) reachable")


def _check_browser() -> DoctorCheck:
    result = playwright_status()
    if result.browser_available:
        return DoctorCheck("browser_playwright", "ok", result.message)
    if result.package_available:
        return DoctorCheck(
            "browser_playwright",
            "warning",
            result.message,
            "On Linux run: python -m playwright install --with-deps chromium",
        )
    return DoctorCheck(
        "browser_playwright",
        "warning",
        result.message,
        "Install the browser extra, then on Linux run: python -m playwright install --with-deps chromium",
    )


def _check_vision(home: Path) -> DoctorCheck:
    config = load_config(home)
    provider_name = config.vision.provider
    provider = config.vision.providers.get(provider_name)
    if provider is None:
        return DoctorCheck("vision", "warning", f"unknown provider: {provider_name}", "Set [vision].provider to fake, openai, or gemini.")
    if provider_name == "fake":
        return DoctorCheck("vision", "ok", "fake provider is configured for offline vision evals")
    if not provider.api_key_env:
        return DoctorCheck("vision", "warning", f"{provider_name} has no api_key_env", "Set vision provider api_key_env.")
    if not os.environ.get(provider.api_key_env):
        return DoctorCheck("vision", "warning", f"{provider_name} secret is not set: {provider.api_key_env}", f"Set {provider.api_key_env}.")
    return DoctorCheck("vision", "ok", f"{provider_name}/{provider.model} is configured")


def _check_subprocess() -> DoctorCheck:
    try:
        result = subprocess.run([sys.executable, "-c", "print('ok')"], capture_output=True, text=True, timeout=5)
    except Exception as exc:
        return DoctorCheck("local_subprocess", "error", str(exc), "Check Python executable.")
    if result.returncode != 0:
        return DoctorCheck("local_subprocess", "error", result.stderr.strip(), "Check Python executable.")
    return DoctorCheck("local_subprocess", "ok", result.stdout.strip())


def _server_status_message(server: MCPServer) -> str:
    if server.last_error:
        return f"{server.name} ({server.last_error})"
    return server.name
