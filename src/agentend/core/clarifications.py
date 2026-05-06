from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentend.core.events import record_event
from agentend.db.models import ClarificationRequest


def create_clarification_request(
    session: Session,
    *,
    run_id: str,
    step_id: str,
    request_type: str,
    question: str,
    reason: str = "",
    choices: list[str] | None = None,
    free_text_allowed: bool = True,
    expires_at: datetime | None = None,
) -> ClarificationRequest:
    request = ClarificationRequest(
        id=str(uuid4()),
        run_id=run_id,
        step_id=step_id,
        request_type=request_type,
        question=question,
        reason=reason,
        choices_json=json.dumps(choices or [], ensure_ascii=False, sort_keys=True),
        free_text_allowed="true" if free_text_allowed else "false",
        resume_token=str(uuid4()),
        status="pending",
        expires_at=expires_at,
    )
    session.add(request)
    record_event(
        session,
        "clarification.created",
        {"request_id": request.id, "request_type": request_type, "question": question},
        run_id=run_id,
    )
    return request


def pending_clarification_for_run(session: Session, run_id: str) -> ClarificationRequest | None:
    return (
        session.execute(
            select(ClarificationRequest)
            .where(ClarificationRequest.run_id == run_id)
            .where(ClarificationRequest.status == "pending")
            .order_by(ClarificationRequest.created_at.desc())
        )
        .scalars()
        .first()
    )


def answer_clarification(session: Session, request: ClarificationRequest, answer: str) -> ClarificationRequest:
    expires_at = request.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at is not None and expires_at <= datetime.now(timezone.utc):
        request.status = "expired"
        record_event(
            session,
            "clarification.expired",
            {"request_id": request.id},
            run_id=request.run_id,
        )
        raise ValueError("Clarification request expired")
    if request.free_text_allowed != "true":
        choices = json.loads(request.choices_json)
        if answer not in choices:
            raise ValueError("Answer must match one of the clarification choices")
    request.status = "answered"
    request.answer = answer
    record_event(
        session,
        "clarification.answered",
        {"request_id": request.id},
        run_id=request.run_id,
    )
    return request
