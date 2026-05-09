from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from agentend.core.events import record_event
from agentend.db.models import ActionPolicyDecision


@dataclass(frozen=True)
class ActionDecision:
    decision: str
    reason: str
    reason_code: str
    risk_level: str
    actor: str
    channel: str
    target: str
    data_class: str
    operation: str
    idempotency: str
    visibility: str
    reversibility: str
    requires_preview: bool = False
    requires_user_confirmation: bool = False
    redactions: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "reason_code": self.reason_code,
            "risk_level": self.risk_level,
            "actor": self.actor,
            "channel": self.channel,
            "target": self.target,
            "data_class": self.data_class,
            "operation": self.operation,
            "idempotency": self.idempotency,
            "visibility": self.visibility,
            "reversibility": self.reversibility,
            "requires_preview": self.requires_preview,
            "requires_user_confirmation": self.requires_user_confirmation,
            "redactions": list(self.redactions),
        }


def decide_action(
    *,
    side_effect: str,
    run_mode: str = "normal",
    channel: str | None = None,
    input_data: dict[str, Any] | None = None,
    target: str | None = None,
    data_class: str = "internal",
    operation: str | None = None,
    idempotency: str | None = None,
    visibility: str | None = None,
    reversibility: str | None = None,
) -> ActionDecision:
    payload = input_data or {}
    actor = _actor_for_run_mode(run_mode)
    resolved_channel = channel or ("scheduler" if run_mode == "scheduler" else "replay" if run_mode == "replay" else "cli")
    resolved_target = target or _target_for_side_effect(side_effect)
    resolved_operation = operation or _operation_for_side_effect(side_effect)
    resolved_idempotency = idempotency or _idempotency_for_side_effect(side_effect)
    resolved_visibility = visibility or _visibility_for_side_effect(side_effect)
    resolved_reversibility = reversibility or _reversibility_for_side_effect(side_effect)

    if run_mode == "replay" and side_effect in {"local_write", "local_execute", "network_write", "external_write"}:
        return _decision(
            decision="block",
            reason=f"{side_effect} is blocked during {run_mode}.",
            reason_code=f"replay_{side_effect}_blocked",
            risk_level="high",
            actor=actor,
            channel=resolved_channel,
            target=resolved_target,
            data_class=data_class,
            operation=resolved_operation,
            idempotency=resolved_idempotency,
            visibility=resolved_visibility,
            reversibility=resolved_reversibility,
        )
    if run_mode == "scheduler" and side_effect in {"local_execute", "network_write", "external_write"}:
        return _decision(
            decision="block",
            reason=f"{side_effect} is blocked during {run_mode}.",
            reason_code=f"scheduler_{side_effect}_blocked",
            risk_level="high",
            actor=actor,
            channel=resolved_channel,
            target=resolved_target,
            data_class=data_class,
            operation=resolved_operation,
            idempotency=resolved_idempotency,
            visibility=resolved_visibility,
            reversibility=resolved_reversibility,
        )
    if data_class == "secret" and side_effect == "external_write":
        return _decision(
            decision="block",
            reason="secret data cannot be sent to an external target.",
            reason_code="secret_external_write_blocked",
            risk_level="critical",
            actor=actor,
            channel=resolved_channel,
            target=resolved_target,
            data_class=data_class,
            operation=resolved_operation,
            idempotency=resolved_idempotency,
            visibility=resolved_visibility,
            reversibility=resolved_reversibility,
            redactions=("secret",),
        )
    if side_effect == "external_write" and bool(payload.get("dry_run")):
        return _decision(
            decision="allow",
            reason="external_write dry-run is allowed without sending data.",
            reason_code="external_write_dry_run_allowed",
            risk_level="low",
            actor=actor,
            channel=resolved_channel,
            target=resolved_target,
            data_class=data_class,
            operation=resolved_operation,
            idempotency=resolved_idempotency,
            visibility=resolved_visibility,
            reversibility=resolved_reversibility,
        )
    if side_effect == "external_write":
        return _decision(
            decision="require_clarification",
            reason="external_write requires preview and user confirmation before execution.",
            reason_code="external_write_requires_confirmation",
            risk_level="high",
            actor=actor,
            channel=resolved_channel,
            target=resolved_target,
            data_class=data_class,
            operation=resolved_operation,
            idempotency=resolved_idempotency,
            visibility=resolved_visibility,
            reversibility=resolved_reversibility,
            requires_preview=True,
            requires_user_confirmation=True,
        )
    return _decision(
        decision="allow",
        reason=f"{side_effect} is allowed for {run_mode} runs.",
        reason_code=f"{run_mode}_{side_effect}_allowed",
        risk_level=_risk_for_side_effect(side_effect),
        actor=actor,
        channel=resolved_channel,
        target=resolved_target,
        data_class=data_class,
        operation=resolved_operation,
        idempotency=resolved_idempotency,
        visibility=resolved_visibility,
        reversibility=resolved_reversibility,
    )


def record_action_decision(
    session: Session,
    *,
    run_id: str,
    step_id: str | None,
    tool_name: str,
    side_effect: str,
    run_mode: str = "normal",
    channel: str | None = None,
    input_data: dict[str, Any] | None = None,
    target: str | None = None,
    data_class: str = "internal",
    operation: str | None = None,
    idempotency: str | None = None,
    visibility: str | None = None,
    reversibility: str | None = None,
) -> ActionDecision:
    decision = decide_action(
        side_effect=side_effect,
        run_mode=run_mode,
        channel=channel,
        input_data=input_data,
        target=target,
        data_class=data_class,
        operation=operation,
        idempotency=idempotency,
        visibility=visibility,
        reversibility=reversibility,
    )
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
    record_event(
        session,
        "policy.decided.v2",
        {"tool_name": tool_name, "side_effect": side_effect, "run_mode": run_mode} | decision.to_payload(),
        run_id=run_id,
    )
    if decision.decision != "allow":
        raise PermissionError(decision.reason)
    return decision


def _decision(**kwargs: Any) -> ActionDecision:
    return ActionDecision(**kwargs)


def _actor_for_run_mode(run_mode: str) -> str:
    if run_mode in {"scheduler", "replay"}:
        return run_mode
    return "user"


def _target_for_side_effect(side_effect: str) -> str:
    if side_effect in {"network_read", "network_write", "external_write"}:
        return "external"
    if side_effect == "local_write":
        return "workspace"
    if side_effect == "local_execute":
        return "local"
    return "local"


def _operation_for_side_effect(side_effect: str) -> str:
    if side_effect in {"local_read", "network_read", "none"}:
        return "read"
    if side_effect == "local_execute":
        return "execute"
    if side_effect == "external_write":
        return "publish"
    return "update"


def _idempotency_for_side_effect(side_effect: str) -> str:
    if side_effect in {"none", "local_read", "network_read"}:
        return "idempotent"
    if side_effect in {"local_execute", "network_write", "external_write"}:
        return "non_idempotent"
    return "unknown"


def _visibility_for_side_effect(side_effect: str) -> str:
    if side_effect in {"network_read", "network_write", "external_write"}:
        return "external"
    if side_effect == "local_write":
        return "project"
    return "local"


def _reversibility_for_side_effect(side_effect: str) -> str:
    if side_effect in {"none", "local_read", "network_read"}:
        return "reversible"
    return "unknown"


def _risk_for_side_effect(side_effect: str) -> str:
    if side_effect in {"local_execute", "network_write", "external_write"}:
        return "high"
    if side_effect == "local_write":
        return "medium"
    return "low"
