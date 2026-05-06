from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentend.db.models import ContextPolicy


DEFAULT_CONTEXT_POLICY = {
    "include_memory": True,
    "redact_secrets": True,
    "max_items": 20,
    "memory_scopes": ["session", "task", "project", "episode", "skill", "user"],
    "retrieve_top_k": 3,
    "min_memory_confidence": 0.5,
    "trusted_memory_sources": ["manual"],
}


def resolve_context_policy(
    session: Session | None,
    *,
    workflow_id: str | None = None,
    skill_id: str | None = None,
    workflow_policy: dict[str, Any] | None = None,
    step_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy = dict(DEFAULT_CONTEXT_POLICY)
    for layer in _db_policy_layers(session, workflow_id=workflow_id, skill_id=skill_id):
        policy = merge_context_policy(policy, layer)
    if workflow_policy:
        policy = merge_context_policy(policy, workflow_policy)
    if step_policy:
        policy = merge_context_policy(policy, step_policy)
    return policy


def merge_context_policy(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if key in {"max_items", "max_context_tokens", "retrieve_top_k"}:
            if key in merged and merged[key] is not None:
                merged[key] = min(int(merged[key]), int(value))
            else:
                merged[key] = int(value)
        elif key == "min_memory_confidence":
            if key in merged and merged[key] is not None:
                merged[key] = max(float(merged[key]), float(value))
            else:
                merged[key] = float(value)
        elif key == "redact_secrets":
            merged[key] = bool(merged.get(key, False)) or bool(value)
        elif key == "include_memory":
            merged[key] = bool(merged.get(key, True)) and bool(value)
        elif key == "memory_scopes":
            existing = set(merged.get(key) or [])
            incoming = set(value or [])
            merged[key] = sorted(existing & incoming) if existing else sorted(incoming)
        elif key == "trusted_memory_sources":
            existing = set(merged.get(key) or [])
            incoming = set(value or [])
            merged[key] = sorted(existing & incoming) if existing else sorted(incoming)
        else:
            merged[key] = value
    return merged


def upsert_context_policy(session: Session, scope: str, target: str, policy: dict[str, Any]) -> ContextPolicy:
    _validate_context_policy_scope(scope)
    row_id = f"{scope}:{target}"
    row = session.get(ContextPolicy, row_id)
    if row is None:
        row = session.execute(
            select(ContextPolicy)
            .where(ContextPolicy.scope == scope)
            .where(ContextPolicy.target == target)
            .order_by(ContextPolicy.updated_at.desc())
        ).scalars().first()
    if row is None:
        row = ContextPolicy(id=row_id, scope=scope, target=target)
        session.add(row)
    row.policy_json = json.dumps(policy, ensure_ascii=False, sort_keys=True)
    return row


def get_context_policy(session: Session, scope: str, target: str) -> ContextPolicy | None:
    _validate_context_policy_scope(scope)
    row = session.get(ContextPolicy, f"{scope}:{target}")
    if row is not None:
        return row
    return (
        session.execute(
            select(ContextPolicy)
            .where(ContextPolicy.scope == scope)
            .where(ContextPolicy.target == target)
            .order_by(ContextPolicy.updated_at.desc())
        )
        .scalars()
        .first()
    )


def _validate_context_policy_scope(scope: str) -> None:
    if scope not in {"global", "project", "skill"}:
        raise ValueError("context policy scope must be global, project, or skill")


def _db_policy_layers(session: Session | None, *, workflow_id: str | None, skill_id: str | None) -> list[dict[str, Any]]:
    if session is None:
        return []
    wanted = [
        ("global", "default"),
        ("project", "default"),
    ]
    if workflow_id:
        wanted.append(("project", workflow_id))
    if skill_id:
        wanted.append(("skill", skill_id))
    layers: list[dict[str, Any]] = []
    for scope, target in wanted:
        row = session.execute(
            select(ContextPolicy)
            .where(ContextPolicy.scope == scope)
            .where(ContextPolicy.target == target)
            .order_by(ContextPolicy.updated_at.desc())
        ).scalars().first()
        if row is None:
            continue
        try:
            layers.append(json.loads(row.policy_json))
        except json.JSONDecodeError:
            continue
    return layers
