from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from agentend.config import load_config
from agentend.core.llm_router import LLMRouter
from agentend.db.session import init_database
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
        _check_llm(resolved),
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


def _check_llm(home: Path) -> DoctorCheck:
    result = LLMRouter(load_config(home)).test()
    return DoctorCheck("llm", "ok" if result.ok else "warning", result.message, None if result.ok else "Set the provider API key.")


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
        "Install Playwright, then on Linux run: python -m playwright install --with-deps chromium",
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
