from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any

from sqlalchemy.orm import Session

from agentend.config import load_config
from agentend.core.events import record_event
from agentend.db.models import ResultCache, utc_now
from agentend.tools.base import ToolContext, ToolResult


CACHEABLE_TOOLS = {"web.fetch", "web.search", "http.request"}


def cache_key_for_tool(context: ToolContext, tool_name: str, input_data: dict[str, Any]) -> tuple[str, str]:
    normalized = _normalized_input(input_data)
    input_hash = sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    config = load_config(context.home)
    config_hash = sha256(
        json.dumps(
            {
                "llm": asdict(config.llm),
                "search": asdict(config.search),
                "tool": tool_name,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return sha256(f"{tool_name}:{input_hash}:{config_hash}".encode("utf-8")).hexdigest(), input_hash


def get_cached_result(
    session: Session,
    context: ToolContext,
    tool_name: str,
    input_data: dict[str, Any],
) -> ToolResult | None:
    if tool_name not in CACHEABLE_TOOLS:
        return None
    cache_key, _ = cache_key_for_tool(context, tool_name, input_data)
    row = session.get(ResultCache, cache_key)
    if row is None or row.status != "active":
        record_event(session, "cache.miss", {"tool_name": tool_name}, run_id=context.run_id)
        return None
    expires_at = row.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at is not None and expires_at <= datetime.now(timezone.utc):
        record_event(session, "cache.stale", {"tool_name": tool_name}, run_id=context.run_id)
        return None
    row.hit_count += 1
    row.last_hit_at = utc_now()
    data = json.loads(row.data_json)
    data["cache_hit"] = True
    record_event(session, "cache.hit", {"tool_name": tool_name}, run_id=context.run_id)
    return ToolResult(content=row.content, data=data)


def store_cached_result(
    session: Session,
    context: ToolContext,
    tool_name: str,
    input_data: dict[str, Any],
    result: ToolResult,
) -> None:
    if tool_name not in CACHEABLE_TOOLS:
        return
    cache_key, input_hash = cache_key_for_tool(context, tool_name, input_data)
    ttl_seconds = int(input_data.get("cache_ttl_seconds", 3600))
    row = session.get(ResultCache, cache_key)
    if row is None:
        row = ResultCache(cache_key=cache_key)
        session.add(row)
    row.tool_name = tool_name
    row.input_hash = input_hash
    row.content = result.content
    row.data_json = json.dumps(result.data, ensure_ascii=False, sort_keys=True)
    row.status = "active"
    row.expires_at = utc_now() + timedelta(seconds=ttl_seconds)
    record_event(session, "cache.miss", {"tool_name": tool_name, "stored": True}, run_id=context.run_id)


def _normalized_input(input_data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in input_data.items() if key not in {"cache_ttl_seconds"}}
