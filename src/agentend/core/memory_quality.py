from __future__ import annotations

import json
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentend.core.memory_store import sync_memory_fts
from agentend.db.models import MemoryItem


def compile_project_memory_digest(
    session: Session,
    *,
    max_items: int = 12,
    max_chars: int = 1200,
) -> MemoryItem:
    rows = (
        session.execute(
            select(MemoryItem)
            .where(MemoryItem.status == "active")
            .where(MemoryItem.scope.in_(["project", "user"]))
            .order_by(MemoryItem.updated_at.desc())
        )
        .scalars()
        .all()
    )
    source_rows = [row for row in rows if "memory-digest" not in _json_list(row.tags_json)]
    bullets: list[str] = []
    for row in source_rows[:max_items]:
        bullet = "- " + " ".join(row.content.split())[:180]
        bullets.append(bullet)
    content = "Compiled project memory digest:\n" + "\n".join(bullets)
    if len(content) > max_chars:
        content = content[: max(0, max_chars - 3)].rstrip() + "..."
    digest = _existing_digest(session)
    tags = ["memory-digest", "compiled", "scope:project"]
    if digest is None:
        digest = MemoryItem(
            id=str(uuid4()),
            scope="project",
            content=content,
            source="agent_consolidator",
            confidence="0.8",
            tags_json=json.dumps(tags, ensure_ascii=False, sort_keys=True),
            status="active",
        )
        session.add(digest)
    else:
        digest.content = content
        digest.source = "agent_consolidator"
        digest.confidence = str(max(_confidence(digest.confidence), 0.8))
        digest.tags_json = json.dumps(sorted(set(_json_list(digest.tags_json) + tags)), ensure_ascii=False, sort_keys=True)
        digest.status = "active"
    sync_memory_fts(session, digest)
    return digest


def lint_memory_items(
    session: Session,
    *,
    max_content_chars: int = 1200,
) -> list[dict[str, str]]:
    rows = session.execute(select(MemoryItem)).scalars().all()
    issues: list[dict[str, str]] = []
    active = [row for row in rows if row.status == "active"]
    seen: dict[str, str] = {}
    for row in rows:
        tags = _json_list(row.tags_json)
        if row.status == "superseded":
            issues.append(_issue(row, "memory_superseded", "Memory has been superseded."))
        if row.status != "active":
            continue
        if len(row.content) > max_content_chars:
            issues.append(_issue(row, "memory_overlong", "Active memory content is too long."))
        if not tags:
            issues.append(_issue(row, "memory_untagged", "Active memory has no tags."))
        if _confidence(row.confidence) < 0.7:
            issues.append(_issue(row, "memory_low_confidence", "Active memory confidence is below promotion threshold."))
        if row.last_used_at is None and "memory-digest" not in tags:
            issues.append(_issue(row, "memory_stale", "Active memory has not been used yet."))
        normalized = " ".join(row.content.lower().split())
        if normalized in seen:
            issues.append(_issue(row, "memory_duplicate", f"Duplicate of memory {seen[normalized]}."))
        else:
            seen[normalized] = row.id
    if not active:
        issues.append({"memory_id": "", "issue": "memory_empty", "message": "No active memories found."})
    return issues


def _existing_digest(session: Session) -> MemoryItem | None:
    rows = session.execute(select(MemoryItem).where(MemoryItem.status == "active")).scalars().all()
    for row in rows:
        if "memory-digest" in _json_list(row.tags_json):
            return row
    return None


def _issue(row: MemoryItem, issue: str, message: str) -> dict[str, str]:
    return {"memory_id": row.id, "issue": issue, "message": message, "scope": row.scope}


def _json_list(raw_json: str) -> list[str]:
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in payload] if isinstance(payload, list) else []


def _confidence(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        return 0.0
