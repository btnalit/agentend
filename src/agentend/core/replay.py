from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentend.core.events import record_event
from agentend.core.tool_contracts import snapshot_tool_contracts, sync_tool_manifests
from agentend.core.tool_registry import ToolRegistry
from agentend.db.models import Conversation, Run, RunStep, ToolCall, ToolContractSnapshot


BLOCKED_REPLAY_SIDE_EFFECTS = {"network_write", "external_write"}


@dataclass(frozen=True)
class ReplayExecution:
    run_id: str
    status: str
    output: str
    report: dict[str, Any]


def build_replay_plan(home: Path, session: Session, source_run_id: str) -> dict[str, Any]:
    source = session.get(Run, source_run_id)
    if source is None:
        raise ValueError(f"Unknown run: {source_run_id}")
    if source.workflow_id is None:
        raise ValueError("Run has no workflow_id and cannot be replayed")

    current_contracts = {contract.name: contract.to_json_dict() for contract in ToolRegistry(home).manifests()}
    source_snapshots = {
        row.tool_name: _json_or_empty(row.contract_json)
        for row in session.execute(
            select(ToolContractSnapshot).where(ToolContractSnapshot.run_id == source_run_id)
        ).scalars()
    }
    source_steps = (
        session.execute(select(RunStep).where(RunStep.run_id == source_run_id).order_by(RunStep.created_at))
        .scalars()
        .all()
    )
    tool_calls_by_step: dict[str, ToolCall] = {
        call.step_id: call
        for call in session.execute(select(ToolCall).where(ToolCall.run_id == source_run_id).order_by(ToolCall.created_at)).scalars()
        if call.step_id is not None
    }

    steps: list[dict[str, Any]] = []
    for step in source_steps:
        tool_call = tool_calls_by_step.get(step.id)
        if tool_call is None:
            steps.append(_plan_non_tool_step(step))
            continue
        steps.append(_plan_tool_step(step, tool_call, source_snapshots.get(tool_call.tool_name), current_contracts.get(tool_call.tool_name)))

    status = "ready"
    failure_reason = ""
    for step in steps:
        if step["strategy"] in {"block", "skip"}:
            status = "blocked"
            failure_reason = str(step.get("skip_reason") or "replay step cannot be reused")
            break

    result_payload = _json_or_empty(source.result_json)
    return {
        "source_run_id": source_run_id,
        "workflow_id": source.workflow_id,
        "status": status,
        "failure_reason": failure_reason,
        "dry_run": False,
        "steps": steps,
        "result": result_payload,
        "output": str(result_payload.get("content", "")),
    }


def execute_replay_plan(home: Path, session: Session, source_run_id: str) -> ReplayExecution:
    plan = build_replay_plan(home, session, source_run_id)
    source = session.get(Run, source_run_id)
    if source is None:
        raise ValueError(f"Unknown run: {source_run_id}")
    contracts = ToolRegistry(home).manifests()
    sync_tool_manifests(session, contracts)

    conversation = Conversation(
        id=str(uuid4()),
        channel="replay",
        external_user_id="workflow",
        title=f"Replay {source_run_id}",
    )
    run = Run(
        id=str(uuid4()),
        conversation_id=conversation.id,
        workflow_id=source.workflow_id,
        status="running",
        input_json=source.input_json,
        result_json="{}",
        agent_profile_path=source.agent_profile_path,
        agent_profile_hash=source.agent_profile_hash,
        llm_provider=source.llm_provider,
        llm_model=source.llm_model,
    )
    session.add(conversation)
    session.add(run)
    snapshot_tool_contracts(session, run.id, contracts)
    record_event(session, "run.replay_planned", {"source_run_id": source_run_id, "status": plan["status"]}, run_id=run.id)

    if plan["status"] != "ready":
        run.status = "failed"
        run.error = str(plan["failure_reason"])
        run.result_json = json.dumps({"error": run.error, "replay_report": plan}, ensure_ascii=False, sort_keys=True)
        record_event(session, "run.replay_blocked", {"source_run_id": source_run_id, "reason": run.error}, run_id=run.id)
        return ReplayExecution(run_id=run.id, status="failed", output=run.error or "", report=plan)

    _copy_reused_steps(session, replay_run_id=run.id, plan=plan)
    output = str(plan.get("output", ""))
    run.status = "completed"
    run.result_json = json.dumps({"content": output, "replay_report": plan}, ensure_ascii=False, sort_keys=True)
    record_event(session, "run.replay_completed", {"source_run_id": source_run_id}, run_id=run.id)
    return ReplayExecution(run_id=run.id, status="completed", output=output, report=plan)


def _plan_non_tool_step(step: RunStep) -> dict[str, Any]:
    if step.status != "completed":
        return _step_plan(step, strategy="skip", skip_reason=f"source step status is {step.status}")
    return _step_plan(step, strategy="reuse_output")


def _plan_tool_step(
    step: RunStep,
    tool_call: ToolCall,
    source_contract: dict[str, Any] | None,
    current_contract: dict[str, Any] | None,
) -> dict[str, Any]:
    base = _step_plan(
        step,
        strategy="reuse_output",
        tool_name=tool_call.tool_name,
        source_tool_call_id=tool_call.id,
        source_tool_call_status=tool_call.status,
    )
    if tool_call.status != "completed":
        return base | {"strategy": "skip", "skip_reason": f"source tool call status is {tool_call.status}"}
    if source_contract is None:
        return base | {
            "strategy": "skip",
            "skip_reason": f"tool contract snapshot missing for {tool_call.tool_name}",
            "contract_drift": True,
            "contract_diff": ["missing_snapshot"],
        }
    if current_contract is None:
        return base | {
            "strategy": "skip",
            "skip_reason": f"current tool contract missing for {tool_call.tool_name}",
            "contract_drift": True,
            "contract_diff": ["missing_current_contract"],
        }

    diff = _contract_diff(source_contract, current_contract)
    if diff:
        return base | {
            "strategy": "skip",
            "skip_reason": f"contract drift detected for {tool_call.tool_name}",
            "contract_drift": True,
            "contract_diff": diff,
        }

    side_effect = str(source_contract.get("side_effect") or current_contract.get("side_effect") or "none")
    if side_effect in BLOCKED_REPLAY_SIDE_EFFECTS:
        return base | {
            "strategy": "block",
            "skip_reason": f"{side_effect} is blocked during replay",
            "side_effect": side_effect,
            "contract_drift": False,
            "contract_diff": [],
        }
    return base | {"side_effect": side_effect, "contract_drift": False, "contract_diff": []}


def _step_plan(
    step: RunStep,
    *,
    strategy: str,
    skip_reason: str = "",
    tool_name: str | None = None,
    source_tool_call_id: str = "",
    source_tool_call_status: str = "",
) -> dict[str, Any]:
    return {
        "source_step_id": step.id,
        "node_id": step.node_id,
        "source_step_status": step.status,
        "strategy": strategy,
        "skip_reason": skip_reason,
        "tool_name": tool_name,
        "source_tool_call_id": source_tool_call_id,
        "source_tool_call_status": source_tool_call_status,
        "contract_drift": False,
        "contract_diff": [],
    }


def _copy_reused_steps(session: Session, *, replay_run_id: str, plan: dict[str, Any]) -> None:
    for step_plan in plan["steps"]:
        source_step = session.get(RunStep, step_plan["source_step_id"])
        if source_step is None:
            continue
        replay_step = RunStep(
            id=str(uuid4()),
            run_id=replay_run_id,
            node_id=source_step.node_id,
            status="completed",
            input_json=source_step.input_json,
            output_json=source_step.output_json,
        )
        session.add(replay_step)
        if not step_plan.get("source_tool_call_id"):
            continue
        source_tool_call = session.get(ToolCall, step_plan["source_tool_call_id"])
        if source_tool_call is None:
            continue
        session.add(
            ToolCall(
                id=str(uuid4()),
                run_id=replay_run_id,
                step_id=replay_step.id,
                tool_name=source_tool_call.tool_name,
                input_json=source_tool_call.input_json,
                output_json=source_tool_call.output_json,
                status="reused",
                latency_ms=0,
            )
        )


def _contract_diff(source_contract: dict[str, Any], current_contract: dict[str, Any]) -> list[str]:
    keys = sorted(set(source_contract) | set(current_contract))
    return [key for key in keys if source_contract.get(key) != current_contract.get(key)]


def _json_or_empty(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}
