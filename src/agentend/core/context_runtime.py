from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session

from agentend.config import load_config
from agentend.core.context_policy import resolve_context_policy
from agentend.core.memory_store import memory_context_drop_reason, record_memory_retrievals, search_memory_candidates
from agentend.core.profile import load_agent_profile
from agentend.core.workflow_schema import WorkflowDefinition
from agentend.db.models import (
    Checkpoint,
    ContextDroppedItem,
    ContextLedger,
    ContextPackItem,
    ContextSummary,
    RunStep,
)
from agentend.tools.base import ToolResult


@dataclass(frozen=True)
class ContextItem:
    item_type: str
    source: str
    summary: str

    @property
    def token_estimate(self) -> int:
        return estimate_tokens(self.summary)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.summary.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DroppedContextItem:
    item: ContextItem
    reason: str


@dataclass(frozen=True)
class ContextPack:
    policy: dict
    selected: list[ContextItem]
    dropped: list[DroppedContextItem]


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def build_context_items(
    home: Path,
    *,
    workflow: WorkflowDefinition | None,
    user_input: str,
    prompt: str | None = None,
    step_policy: dict | None = None,
    session: Session | None = None,
) -> list[ContextItem]:
    return build_context_pack(
        home,
        workflow=workflow,
        user_input=user_input,
        prompt=prompt,
        step_policy=step_policy,
        session=session,
    ).selected


def build_context_pack(
    home: Path,
    *,
    workflow: WorkflowDefinition | None,
    user_input: str,
    prompt: str | None = None,
    step_policy: dict | None = None,
    session: Session | None = None,
) -> ContextPack:
    config = load_config(home)
    profile = load_agent_profile(config)
    workflow_id = workflow.id if workflow is not None else None
    skill_id = _skill_id_from_workflow(workflow_id)
    policy = resolve_context_policy(
        session,
        workflow_id=workflow_id,
        skill_id=skill_id,
        workflow_policy=workflow.context if workflow is not None else None,
        step_policy=step_policy,
    )
    items = [
        ContextItem("context_policy", "merged", json.dumps(policy, ensure_ascii=False, sort_keys=True)),
        ContextItem("fixed", str(profile.path), profile.content[:1000]),
        ContextItem("task", "user_input", user_input),
    ]
    if workflow is not None:
        items.append(ContextItem("workflow", workflow.id, workflow.name))
    if prompt:
        items.append(ContextItem("prompt", "workflow_step", prompt))
    dropped: list[DroppedContextItem] = []
    if session is not None and policy.get("include_memory", True):
        memory_scopes = set(policy.get("memory_scopes") or [])
        memories = search_memory_candidates(session, user_input, limit=int(policy.get("retrieve_top_k", 3)) * 4)
        selected_memories = []
        min_confidence = float(policy.get("min_memory_confidence", 0.5))
        trusted_sources = {str(source) for source in policy.get("trusted_memory_sources", ["manual"])}
        for memory in memories:
            if memory_scopes and memory.scope not in memory_scopes:
                dropped.append(_dropped_memory(memory.scope, memory.content, "memory_scope_not_allowed"))
                continue
            reason = memory_context_drop_reason(
                memory,
                scope=None,
                min_confidence=min_confidence,
                trusted_sources=trusted_sources,
            )
            if reason is not None:
                dropped.append(_dropped_memory(memory.scope, memory.content, reason))
                continue
            selected_memories.append(memory)
            items.append(ContextItem("memory", memory.scope, memory.content))
        record_memory_retrievals(session, selected_memories, query=user_input)
    selected, budget_dropped = _select_with_budget(items, policy)
    return ContextPack(policy=policy, selected=selected, dropped=dropped + budget_dropped)


def record_context_ledger(
    session: Session,
    home: Path,
    *,
    run_id: str,
    step_id: str | None,
    workflow: WorkflowDefinition | None,
    user_input: str,
    prompt: str,
    model_stage: str,
    model_provider: str,
    model_model: str,
    step_policy: dict | None = None,
) -> ContextLedger:
    pack = build_context_pack(home, workflow=workflow, user_input=user_input, prompt=prompt, step_policy=step_policy, session=session)
    items = pack.selected
    ledger = ContextLedger(
        id=str(uuid4()),
        run_id=run_id,
        workflow_step_id=step_id,
        model_stage=model_stage,
        model_provider=model_provider,
        model_model=model_model,
        estimated_input_tokens=sum(item.token_estimate for item in items),
    )
    session.add(ledger)
    for item in items:
        session.add(
            ContextPackItem(
                id=str(uuid4()),
                ledger_id=ledger.id,
                item_type=item.item_type,
                source=item.source,
                summary=item.summary,
                content_hash=item.content_hash,
                token_estimate=item.token_estimate,
            )
        )
    for dropped in pack.dropped:
        item = dropped.item
        session.add(
            ContextDroppedItem(
                id=str(uuid4()),
                ledger_id=ledger.id,
                item_type=item.item_type,
                source=item.source,
                summary=item.summary,
                content_hash=item.content_hash,
                token_estimate=item.token_estimate,
                reason=dropped.reason,
            )
        )
    return ledger


def _skill_id_from_workflow(workflow_id: str | None) -> str | None:
    if workflow_id and workflow_id.startswith("skill."):
        return workflow_id.removeprefix("skill.")
    return None


def _dropped_memory(scope: str, content: str, reason: str) -> DroppedContextItem:
    return DroppedContextItem(ContextItem("memory", scope, content), reason)


def _select_with_budget(items: list[ContextItem], policy: dict) -> tuple[list[ContextItem], list[DroppedContextItem]]:
    max_items = int(policy.get("max_items", 20))
    max_context_tokens = policy.get("max_context_tokens")
    token_budget = int(max_context_tokens) if max_context_tokens is not None else None
    selected: list[ContextItem] = []
    dropped: list[DroppedContextItem] = []
    used_tokens = 0
    for item in items:
        if len(selected) >= max_items:
            dropped.append(DroppedContextItem(item, "max_items_exceeded"))
            continue
        if token_budget is not None and used_tokens + item.token_estimate > token_budget:
            dropped.append(DroppedContextItem(item, "max_context_tokens_exceeded"))
            continue
        selected.append(item)
        used_tokens += item.token_estimate
    return selected, dropped


def compact_tool_result(
    session: Session,
    *,
    run_id: str,
    step_id: str | None,
    tool_name: str,
    result: ToolResult,
) -> ContextSummary:
    data_keys = ", ".join(sorted(result.data.keys())) or "no structured data"
    summary_text = f"{tool_name}: {result.content[:200]} ({data_keys})"
    row = ContextSummary(
        id=str(uuid4()),
        run_id=run_id,
        step_id=step_id,
        source_type="tool_result",
        source_id=tool_name,
        summary=summary_text,
        artifact_path=str(result.artifact_path) if result.artifact_path else None,
    )
    session.add(row)
    return row


def create_checkpoint(
    session: Session,
    *,
    run_id: str,
    step: RunStep,
    output: str,
) -> Checkpoint:
    checkpoint = Checkpoint(
        id=str(uuid4()),
        run_id=run_id,
        step_id=step.id,
        node_id=step.node_id,
        state_json=json.dumps({"node_id": step.node_id, "status": step.status}, ensure_ascii=False, sort_keys=True),
        context_summary_json=json.dumps({"output": output[:500]}, ensure_ascii=False, sort_keys=True),
    )
    session.add(checkpoint)
    return checkpoint
