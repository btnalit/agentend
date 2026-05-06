from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentend.core.secrets import redact_text
from agentend.db.models import EvidenceLink, SourceRecord, ToolCall
from agentend.tools.base import ToolContext, ToolResult


def record_web_fetch_evidence(
    session: Session,
    home: Path,
    *,
    run_id: str,
    url: str,
    title: str | None,
    text: str,
) -> SourceRecord:
    source = _add_source(
        session,
        home,
        run_id=run_id,
        source_type="web",
        url=url,
        title=title,
        quote=text[:500],
        content_hash=_hash_payload({"url": url, "title": title, "text": text}),
    )
    return source


def record_web_search_evidence(
    session: Session,
    home: Path,
    *,
    run_id: str,
    query: str,
    provider: str,
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    recorded: list[dict[str, Any]] = []
    for result in results:
        title = str(result.get("title", ""))
        url = str(result.get("url", ""))
        snippet = str(result.get("snippet", ""))
        source = _add_source(
            session,
            home,
            run_id=run_id,
            source_type="web_search",
            url=url,
            title=title,
            quote=snippet[:500],
            content_hash=_hash_payload(
                {
                    "provider": provider,
                    "query": query,
                    "title": title,
                    "url": url,
                    "snippet": snippet,
                }
            ),
        )
        recorded.append(result | {"source_id": source.id})
    return recorded


def rehydrate_cached_evidence(session: Session, context: ToolContext, tool_name: str, result: ToolResult) -> ToolResult:
    if tool_name == "web.fetch":
        text = str(result.data.get("text") or result.content)
        source = record_web_fetch_evidence(
            session,
            context.home,
            run_id=context.run_id,
            url=str(result.data.get("url", "")),
            title=result.data.get("title") if result.data.get("title") is not None else None,
            text=text,
        )
        data = dict(result.data) | {"source_id": source.id, "cache_hit": True}
        return ToolResult(content=result.content, data=data, artifact_path=result.artifact_path)

    if tool_name == "web.search":
        results = [
            {key: value for key, value in dict(item).items() if key != "source_id"}
            for item in result.data.get("results", [])
            if isinstance(item, dict)
        ]
        recorded = record_web_search_evidence(
            session,
            context.home,
            run_id=context.run_id,
            query=str(result.data.get("query", "")),
            provider=str(result.data.get("provider", "unknown")),
            results=results,
        )
        data = dict(result.data) | {"results": recorded, "cache_hit": True}
        return ToolResult(content=json.dumps(recorded, ensure_ascii=False, indent=2), data=data, artifact_path=result.artifact_path)

    return result


def evidence_manifest_for_run(session: Session, home: Path, run_id: str) -> dict[str, Any]:
    sources = session.execute(select(SourceRecord).where(SourceRecord.used_by_run_id == run_id).order_by(SourceRecord.fetched_at)).scalars().all()
    links = session.execute(select(EvidenceLink).where(EvidenceLink.run_id == run_id).order_by(EvidenceLink.created_at)).scalars().all()
    tool_calls = session.execute(select(ToolCall).where(ToolCall.run_id == run_id).order_by(ToolCall.created_at)).scalars().all()
    usage = _source_usage(tool_calls)
    return {
        "run_id": run_id,
        "sources": [
            {
                "id": source.id,
                "source_type": source.source_type,
                "url": redact_text(home, source.url or ""),
                "path": redact_text(home, source.path or ""),
                "title": redact_text(home, source.title or ""),
                "quote": redact_text(home, source.quote),
                "content_hash": source.content_hash or "",
                "fetched_at": source.fetched_at.isoformat(),
                "query": usage.get(source.id, {}).get("query", ""),
                "used_by_run_id": source.used_by_run_id or "",
                "tool_call_id": usage.get(source.id, {}).get("tool_call_id", ""),
            }
            for source in sources
        ],
        "links": [
            {
                "id": link.id,
                "source_id": link.source_id,
                "run_id": link.run_id or "",
                "artifact_id": link.artifact_id or "",
                "relation": link.relation,
            }
            for link in links
        ],
    }


def _add_source(
    session: Session,
    home: Path,
    *,
    run_id: str,
    source_type: str,
    url: str | None,
    title: str | None,
    quote: str,
    content_hash: str,
) -> SourceRecord:
    source = SourceRecord(
        id=str(uuid4()),
        used_by_run_id=run_id,
        source_type=source_type,
        url=redact_text(home, url or ""),
        title=redact_text(home, title or ""),
        content_hash=content_hash,
        quote=redact_text(home, quote),
    )
    session.add(source)
    session.add(EvidenceLink(id=str(uuid4()), source_id=source.id, run_id=run_id, relation="used_by_tool"))
    return source


def _source_usage(tool_calls: list[ToolCall]) -> dict[str, dict[str, str]]:
    usage: dict[str, dict[str, str]] = {}
    for call in tool_calls:
        input_payload = _load_json(call.input_json)
        output_payload = _load_json(call.output_json)
        query = str(output_payload.get("query") or input_payload.get("query") or "")
        source_ids = _source_ids(output_payload)
        for source_id in source_ids:
            usage[source_id] = {"tool_call_id": call.id, "query": query}
    return usage


def _source_ids(value: Any) -> list[str]:
    if isinstance(value, dict):
        ids = [str(value["source_id"])] if value.get("source_id") else []
        for item in value.values():
            ids.extend(_source_ids(item))
        return ids
    if isinstance(value, list):
        ids: list[str] = []
        for item in value:
            ids.extend(_source_ids(item))
        return ids
    return []


def _load_json(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _hash_payload(payload: dict[str, Any]) -> str:
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
