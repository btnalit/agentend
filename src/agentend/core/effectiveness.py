from __future__ import annotations

import json
from collections import Counter
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentend.db.models import CapabilityEffectiveness, CapabilityEffectivenessEvent, utc_now


VALID_EFFECTIVENESS_STATUSES = {"success", "failure", "blocked"}


def effectiveness_key(capability_type: str, capability_id: str, goal_type: str = "general") -> str:
    return f"{capability_type}:{capability_id}:{goal_type or 'general'}"


def record_effectiveness_event(
    session: Session,
    *,
    capability_type: str,
    capability_id: str,
    status: str,
    goal_type: str = "general",
    agent_run_id: str | None = None,
    iteration_id: str | None = None,
    error_code: str | None = None,
    duration_ms: int = 0,
    output_artifact_count: int = 0,
    iteration_count: int = 1,
) -> CapabilityEffectivenessEvent:
    normalized_status = status if status in VALID_EFFECTIVENESS_STATUSES else "failure"
    event = CapabilityEffectivenessEvent(
        id=str(uuid4()),
        agent_run_id=agent_run_id,
        iteration_id=iteration_id,
        capability_type=capability_type,
        capability_id=capability_id,
        goal_type=goal_type or "general",
        status=normalized_status,
        error_code=error_code,
        duration_ms=max(0, int(duration_ms)),
        output_artifact_count=max(0, int(output_artifact_count)),
        iteration_count=max(1, int(iteration_count)),
    )
    session.add(event)

    row_id = effectiveness_key(capability_type, capability_id, goal_type or "general")
    aggregate = session.get(CapabilityEffectiveness, row_id)
    if aggregate is None:
        aggregate = CapabilityEffectiveness(
            id=row_id,
            capability_type=capability_type,
            capability_id=capability_id,
            goal_type=goal_type or "general",
            attempts=0,
            successes=0,
            failures=0,
            blocked=0,
            avg_duration_ms=0,
            avg_iterations=0,
            common_error_json="{}",
        )
        session.add(aggregate)

    previous_attempts = aggregate.attempts
    aggregate.attempts += 1
    if normalized_status == "success":
        aggregate.successes += 1
        aggregate.last_success_at = utc_now()
    elif normalized_status == "blocked":
        aggregate.blocked += 1
    else:
        aggregate.failures += 1
        aggregate.last_failure_at = utc_now()
        if error_code:
            aggregate.common_error_json = _updated_error_counts(aggregate.common_error_json, error_code)

    aggregate.avg_duration_ms = _running_average(
        aggregate.avg_duration_ms,
        previous_attempts,
        max(0, int(duration_ms)),
    )
    aggregate.avg_iterations = _running_average(
        aggregate.avg_iterations,
        previous_attempts,
        max(1, int(iteration_count)),
    )
    aggregate.updated_at = utc_now()
    return event


def effectiveness_for(
    session: Session,
    capability_type: str,
    capability_id: str,
    goal_type: str = "general",
) -> CapabilityEffectiveness | None:
    direct = session.get(CapabilityEffectiveness, effectiveness_key(capability_type, capability_id, goal_type))
    if direct is not None:
        return direct
    general = session.get(CapabilityEffectiveness, effectiveness_key(capability_type, capability_id, "general"))
    if general is not None:
        return general
    return (
        session.execute(
            select(CapabilityEffectiveness)
            .where(CapabilityEffectiveness.capability_type == capability_type)
            .where(CapabilityEffectiveness.capability_id == capability_id)
            .order_by(CapabilityEffectiveness.updated_at.desc())
        )
        .scalars()
        .first()
    )


def effectiveness_rows(
    session: Session,
    *,
    capability_type: str | None = None,
    capability_id: str | None = None,
) -> list[CapabilityEffectiveness]:
    stmt = select(CapabilityEffectiveness).order_by(CapabilityEffectiveness.updated_at.desc())
    if capability_type is not None:
        stmt = stmt.where(CapabilityEffectiveness.capability_type == capability_type)
    if capability_id is not None:
        stmt = stmt.where(CapabilityEffectiveness.capability_id == capability_id)
    return list(session.execute(stmt).scalars().all())


def effectiveness_summary_dict(row: CapabilityEffectiveness) -> dict[str, object]:
    return {
        "id": row.id,
        "capability_type": row.capability_type,
        "capability_id": row.capability_id,
        "goal_type": row.goal_type,
        "attempts": row.attempts,
        "successes": row.successes,
        "failures": row.failures,
        "blocked": row.blocked,
        "avg_duration_ms": row.avg_duration_ms,
        "avg_iterations": row.avg_iterations,
        "success_rate": (row.successes / row.attempts) if row.attempts else 0.0,
        "common_errors": json.loads(row.common_error_json or "{}"),
    }


def _running_average(previous_average: int, previous_count: int, value: int) -> int:
    if previous_count <= 0:
        return value
    return int(round(((previous_average * previous_count) + value) / (previous_count + 1)))


def _updated_error_counts(raw_json: str, error_code: str) -> str:
    try:
        payload = json.loads(raw_json or "{}")
    except json.JSONDecodeError:
        payload = {}
    counter = Counter({str(key): int(value) for key, value in payload.items() if str(value).isdigit()})
    counter[error_code] += 1
    return json.dumps(dict(counter.most_common(8)), ensure_ascii=False, sort_keys=True)
