from __future__ import annotations

import os
import re
from pathlib import Path

from sqlalchemy.orm import Session

from agentend.config import load_config
from agentend.db.models import SecretRef

TOKEN_LIKE_RE = re.compile(r"\b(sk-[A-Za-z0-9_-]{6,}|[A-Za-z0-9_\-]{24,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,})\b")


def configured_secret_names(home: Path) -> list[str]:
    config = load_config(home)
    names = {config.llm.provider_config.api_key_env, "TELEGRAM_BOT_TOKEN"}
    for provider in config.search.providers.values():
        if provider.api_key_env:
            names.add(provider.api_key_env)
    for provider in config.vision.providers.values():
        if provider.api_key_env:
            names.add(provider.api_key_env)
    for path in [config.home / ".env", config.home / ".env.example"]:
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            names.add(line.split("=", 1)[0].strip())
    for key in os.environ:
        upper = key.upper()
        if upper.startswith("AGENTEND_") or upper.endswith(("_API_KEY", "_TOKEN", "_SECRET")):
            names.add(key)
    return sorted(names)


def upsert_secret_ref(session: Session, name: str, *, present: bool) -> SecretRef:
    row = session.get(SecretRef, name)
    if row is None:
        row = SecretRef(name=name, source="env", present="true" if present else "false")
        session.add(row)
    else:
        row.present = "true" if present else "false"
    return row


def redact_text(home: Path, text: str) -> str:
    redacted = text
    for name in configured_secret_names(home):
        value = os.environ.get(name)
        if value and len(value) >= 4:
            redacted = redacted.replace(value, "[REDACTED]")
    return TOKEN_LIKE_RE.sub("[REDACTED]", redacted)
