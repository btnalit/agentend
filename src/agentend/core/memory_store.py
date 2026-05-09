from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from agentend.core.events import record_event
from agentend.core.memory_gate import (
    LONG_TERM_SCOPES,
    TRUSTED_SOURCES,
    UNTRUSTED_ALLOWED_SCOPES,
    decide_memory_read,
    decide_memory_write,
)
from agentend.core.secrets import redact_text
from agentend.db.models import MemoryItem, MemoryRetrieval, utc_now


def write_memory_item(
    session: Session,
    home: Path,
    *,
    content: str,
    scope: str,
    source: str = "manual",
    confidence: str = "1.0",
    ttl: str | None = None,
    tags: list[str] | None = None,
) -> MemoryItem:
    gate = decide_memory_write(scope=scope, source=source, confidence=confidence)
    if gate.decision == "reject":
        record_event(session, "memory.write_gate_decided", gate.to_payload())
        raise PermissionError(_memory_write_error(gate.reason_code, scope=scope, source=source))
    memory = MemoryItem(
        id=str(uuid4()),
        scope=scope,
        content=redact_text(home, content),
        source=source,
        confidence=confidence,
        ttl=ttl,
        tags_json=json.dumps(tags or [], ensure_ascii=False, sort_keys=True),
        status="active",
    )
    session.add(memory)
    sync_memory_fts(session, memory)
    record_event(session, "memory.write_gate_decided", {**gate.to_payload(), "memory_id": memory.id})
    record_event(session, "memory.created", {"memory_id": memory.id, "scope": scope, "source": source})
    return memory


def edit_memory_item(session: Session, home: Path, memory: MemoryItem, content: str) -> None:
    memory.content = redact_text(home, content)
    sync_memory_fts(session, memory)
    record_event(session, "memory.updated", {"memory_id": memory.id})


def forget_memory_item(session: Session, memory: MemoryItem) -> None:
    memory.status = "forgotten"
    remove_memory_fts(session, memory.id)
    record_event(session, "memory.forgotten", {"memory_id": memory.id})


def search_memory_items(session: Session, query: str, *, scope: str | None = None, limit: int = 10) -> list[MemoryItem]:
    rows = search_memory_candidates(session, query, scope=scope, limit=limit * 3)
    rows = [row for row in rows if _memory_visible(row, scope=scope)]
    rows.sort(key=lambda row: (_confidence(row), row.created_at), reverse=True)
    result = rows[:limit]
    record_memory_retrievals(session, result, query=query)
    return result


def search_memory_candidates(session: Session, query: str, *, scope: str | None = None, limit: int = 10) -> list[MemoryItem]:
    rows = _search_memory_fts(session, query, scope=scope, limit=limit)
    if rows is None or not rows:
        fallback_rows = _search_memory_fallback(session, query, scope=scope)
        if rows is None or fallback_rows:
            rows = fallback_rows
    rows.sort(key=lambda row: (_confidence(row), row.created_at), reverse=True)
    return rows[:limit]


def record_memory_retrievals(session: Session, memories: list[MemoryItem], *, query: str) -> None:
    now = utc_now()
    for row in memories:
        row.last_used_at = now
        session.add(MemoryRetrieval(id=str(uuid4()), memory_id=row.id, query=query))
    if memories:
        record_event(session, "memory.retrieved", {"count": len(memories), "query": query})


def memory_context_drop_reason(
    memory: MemoryItem,
    *,
    scope: str | None,
    min_confidence: float,
    trusted_sources: set[str],
) -> str | None:
    gate = decide_memory_read(memory, scope=scope, min_confidence=min_confidence, trusted_sources=trusted_sources)
    return gate.reason_code if gate.decision == "drop" else None


def sync_memory_fts(session: Session, memory: MemoryItem) -> None:
    if not _ensure_memory_fts(session):
        return
    tags = memory.tags_json
    try:
        session.execute(text("DELETE FROM memory_items_fts WHERE memory_id = :memory_id"), {"memory_id": memory.id})
        if memory.status == "active":
            session.execute(
                text(
                    "INSERT INTO memory_items_fts(memory_id, scope, content, tags) "
                    "VALUES (:memory_id, :scope, :content, :tags)"
                ),
                {"memory_id": memory.id, "scope": memory.scope, "content": memory.content, "tags": tags},
            )
    except SQLAlchemyError:
        return


def remove_memory_fts(session: Session, memory_id: str) -> None:
    if not _ensure_memory_fts(session):
        return
    try:
        session.execute(text("DELETE FROM memory_items_fts WHERE memory_id = :memory_id"), {"memory_id": memory_id})
    except SQLAlchemyError:
        return


def _memory_write_error(reason_code: str, *, scope: str, source: str) -> str:
    if reason_code == "memory_write_untrusted_long_term":
        return f"untrusted source '{source}' cannot write {scope} memory"
    if reason_code == "memory_write_untrusted_scope":
        return f"untrusted source '{source}' can only write session/task/episode memory"
    return f"memory write rejected by policy: {reason_code}"


def _ensure_memory_fts(session: Session) -> bool:
    try:
        session.execute(
            text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memory_items_fts "
                "USING fts5(memory_id UNINDEXED, scope UNINDEXED, content, tags UNINDEXED)"
            )
        )
        return True
    except SQLAlchemyError:
        return False


def _search_memory_fts(session: Session, query: str, *, scope: str | None, limit: int) -> list[MemoryItem] | None:
    if not _ensure_memory_fts(session):
        return None
    fts_query = _sanitize_fts_query(query)
    if not fts_query:
        return None
    try:
        sql = (
            "SELECT memory_id FROM memory_items_fts WHERE memory_items_fts MATCH :query "
            "LIMIT :limit"
        )
        params = {"query": fts_query, "limit": limit}
        ids = [row[0] for row in session.execute(text(sql), params).all()]
    except SQLAlchemyError:
        return None
    if not ids:
        return []
    stmt = select(MemoryItem).where(MemoryItem.id.in_(ids))
    if scope:
        stmt = stmt.where(MemoryItem.scope == scope)
    rows = session.execute(stmt).scalars().all()
    return rows


def _search_memory_fallback(session: Session, query: str, *, scope: str | None) -> list[MemoryItem]:
    stmt = select(MemoryItem).where(MemoryItem.status == "active")
    if scope:
        stmt = stmt.where(MemoryItem.scope == scope)
    rows = session.execute(stmt).scalars().all()
    terms = _memory_query_terms(query)
    if not terms:
        return []
    return [row for row in rows if all(term in row.content.lower() for term in terms)]


def _memory_visible(memory: MemoryItem, *, scope: str | None) -> bool:
    if memory.status != "active":
        return False
    if scope and memory.scope != scope:
        return False
    if memory.ttl:
        try:
            expires_at = datetime.fromisoformat(memory.ttl)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= datetime.now(timezone.utc):
                return False
        except ValueError:
            pass
    return True


def _confidence(memory: MemoryItem) -> float:
    try:
        return float(memory.confidence)
    except ValueError:
        return 0.0


def _sanitize_fts_query(query: str) -> str:
    return " ".join(_memory_query_terms(query))


def _memory_query_terms(query: str) -> list[str]:
    stopwords = {
        "a",
        "an",
        "and",
        "are",
        "for",
        "in",
        "is",
        "me",
        "of",
        "or",
        "please",
        "the",
        "to",
        "with",
    }
    return [term for term in re.findall(r"\w+", query.lower()) if term not in stopwords]
