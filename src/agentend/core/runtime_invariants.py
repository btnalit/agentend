from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentend.core.runtime_states import PENDING_CLARIFICATION_STATUSES, TERMINAL_AGENT_ITERATION_STATUSES
from agentend.db.models import (
    ActionPolicyDecision,
    AgentIteration,
    AgentRun,
    ClarificationRequest,
    ContextLedger,
    CostUsage,
    ToolCall,
)


@dataclass(frozen=True)
class InvariantIssue:
    code: str
    message: str
    severity: str = "error"
    run_id: str | None = None
    agent_run_id: str | None = None
    iteration_id: str | None = None
    tool_call_id: str | None = None
    cost_usage_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "details": self.details,
        }
        for key in ("run_id", "agent_run_id", "iteration_id", "tool_call_id", "cost_usage_id"):
            value = getattr(self, key)
            if value:
                payload[key] = value
        return payload


def check_run_invariants(
    session: Session,
    *,
    run_id: str | None = None,
    agent_run_id: str | None = None,
) -> list[InvariantIssue]:
    """Check the runtime audit links that make a run replayable and debuggable."""
    issues: list[InvariantIssue] = []
    agent_runs = _agent_runs(session, agent_run_id)
    run_ids = set([run_id] if run_id else [])

    for agent_run in agent_runs:
        iterations = _agent_iterations(session, agent_run.id)
        _check_agent_run_state(agent_run, iterations, issues)
        linked_run_ids = _linked_run_ids(agent_run, iterations)
        run_ids.update(linked_run_ids)
        _check_waiting_input_clarification(session, agent_run, linked_run_ids, issues)

    for checked_run_id in sorted(run_ids):
        _check_tool_call_policy_links(session, checked_run_id, issues)
        _check_llm_context_links(session, checked_run_id, issues)

    return issues


def _agent_runs(session: Session, agent_run_id: str | None) -> list[AgentRun]:
    if agent_run_id:
        row = session.get(AgentRun, agent_run_id)
        return [row] if row is not None else []
    return []


def _agent_iterations(session: Session, agent_run_id: str) -> list[AgentIteration]:
    return (
        session.execute(
            select(AgentIteration)
            .where(AgentIteration.agent_run_id == agent_run_id)
            .order_by(AgentIteration.iteration_index)
        )
        .scalars()
        .all()
    )


def _check_agent_run_state(agent_run: AgentRun, iterations: list[AgentIteration], issues: list[InvariantIssue]) -> None:
    if agent_run.status != "completed":
        return
    active_iterations = [row for row in iterations if row.status not in TERMINAL_AGENT_ITERATION_STATUSES]
    for iteration in active_iterations:
        issues.append(
            InvariantIssue(
                code="completed_agent_run_has_active_iteration",
                message="Completed agent runs must not retain active iterations.",
                agent_run_id=agent_run.id,
                iteration_id=iteration.id,
                details={"iteration_status": iteration.status, "iteration_index": iteration.iteration_index},
            )
        )


def _check_waiting_input_clarification(
    session: Session,
    agent_run: AgentRun,
    linked_run_ids: set[str],
    issues: list[InvariantIssue],
) -> None:
    if agent_run.status != "waiting_input":
        return
    if not linked_run_ids:
        issues.append(
            InvariantIssue(
                code="waiting_input_missing_clarification",
                message="waiting_input agent runs must be linked to a pending clarification request.",
                agent_run_id=agent_run.id,
                details={"reason": "missing_linked_run"},
            )
        )
        return
    pending = (
        session.execute(
            select(ClarificationRequest)
            .where(ClarificationRequest.run_id.in_(sorted(linked_run_ids)))
            .where(ClarificationRequest.status.in_(PENDING_CLARIFICATION_STATUSES))
        )
        .scalars()
        .first()
    )
    if pending is None:
        issues.append(
            InvariantIssue(
                code="waiting_input_missing_clarification",
                message="waiting_input agent runs must have an active clarification request.",
                agent_run_id=agent_run.id,
                details={"linked_run_ids": sorted(linked_run_ids)},
            )
        )


def _check_tool_call_policy_links(session: Session, run_id: str, issues: list[InvariantIssue]) -> None:
    tool_calls = (
        session.execute(select(ToolCall).where(ToolCall.run_id == run_id).order_by(ToolCall.created_at))
        .scalars()
        .all()
    )
    grouped_calls: dict[tuple[str | None, str], list[ToolCall]] = {}
    for tool_call in tool_calls:
        grouped_calls.setdefault((tool_call.step_id, tool_call.tool_name), []).append(tool_call)
    for (step_id, tool_name), calls in grouped_calls.items():
        query = (
            select(ActionPolicyDecision)
            .where(ActionPolicyDecision.run_id == run_id)
            .where(ActionPolicyDecision.tool_name == tool_name)
        )
        if step_id is not None:
            query = query.where(ActionPolicyDecision.step_id == step_id)
        decision_count = len(session.execute(query).scalars().all())
        if decision_count >= len(calls):
            continue
        for tool_call in calls[decision_count:]:
            issues.append(
                InvariantIssue(
                    code="tool_call_missing_policy_decision",
                    message="Every tool call must have a matching ActionPolicy decision.",
                    run_id=run_id,
                    tool_call_id=tool_call.id,
                    details={
                        "tool_name": tool_name,
                        "step_id": step_id,
                        "tool_call_count": len(calls),
                        "policy_decision_count": decision_count,
                    },
                )
            )


def _check_llm_context_links(session: Session, run_id: str, issues: list[InvariantIssue]) -> None:
    usages = (
        session.execute(select(CostUsage).where(CostUsage.run_id == run_id).order_by(CostUsage.created_at))
        .scalars()
        .all()
    )
    grouped_usages: dict[tuple[str | None, str], list[CostUsage]] = {}
    for usage in usages:
        grouped_usages.setdefault((usage.step_id, usage.model_stage), []).append(usage)
    for (step_id, model_stage), usage_rows in grouped_usages.items():
        query = (
            select(ContextLedger)
            .where(ContextLedger.run_id == run_id)
            .where(ContextLedger.model_stage == model_stage)
        )
        if step_id is not None:
            query = query.where(ContextLedger.workflow_step_id == step_id)
        ledger_count = len(session.execute(query).scalars().all())
        if ledger_count >= len(usage_rows):
            continue
        for usage in usage_rows[ledger_count:]:
            issues.append(
                InvariantIssue(
                    code="llm_call_missing_context_ledger",
                    message="Every persisted LLM usage row must have a matching ContextLedger.",
                    run_id=run_id,
                    cost_usage_id=usage.id,
                    details={
                        "model_stage": model_stage,
                        "step_id": step_id,
                        "cost_usage_count": len(usage_rows),
                        "context_ledger_count": ledger_count,
                    },
                )
            )


def _linked_run_ids(agent_run: AgentRun, iterations: list[AgentIteration]) -> set[str]:
    linked_run_ids = {row.linked_run_id for row in iterations if row.linked_run_id}
    payload = _json_object(agent_run.final_result_json)
    for key in ("linked_run_id", "run_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            linked_run_ids.add(value)
    values = payload.get("linked_run_ids")
    if isinstance(values, list):
        linked_run_ids.update(value for value in values if isinstance(value, str) and value)
    return linked_run_ids


def _json_object(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}
