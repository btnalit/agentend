from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy.orm import Session

from agentend.db.models import ActionPolicyDecision


@dataclass(frozen=True)
class ActionDecision:
    decision: str
    reason: str


def decide_action(*, side_effect: str, run_mode: str = "normal") -> ActionDecision:
    if run_mode in {"replay", "scheduler"} and side_effect in {"network_write", "external_write"}:
        return ActionDecision("block", f"{side_effect} is blocked during {run_mode}.")
    return ActionDecision("allow", f"{side_effect} is allowed for {run_mode} runs.")


def record_action_decision(
    session: Session,
    *,
    run_id: str,
    step_id: str | None,
    tool_name: str,
    side_effect: str,
    run_mode: str = "normal",
) -> ActionDecision:
    decision = decide_action(side_effect=side_effect, run_mode=run_mode)
    session.add(
        ActionPolicyDecision(
            id=str(uuid4()),
            run_id=run_id,
            step_id=step_id,
            tool_name=tool_name,
            decision=decision.decision,
            side_effect=side_effect,
            reason=decision.reason,
        )
    )
    if decision.decision == "block":
        raise PermissionError(decision.reason)
    return decision
