from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from agentend.core.events import record_event
from agentend.core.intent_router import IntentDecision
from agentend.core.secrets import redact_text
from agentend.db.models import IntentDecisionRecord


def record_intent_decision(
    home: Path,
    session: Session,
    text: str,
    decision: IntentDecision | dict[str, Any],
    *,
    conversation_id: str | None = None,
    run_id: str | None = None,
    agent_run_id: str | None = None,
    channel: str | None = None,
    external_user_id: str | None = None,
    route_type: str | None = None,
    context_summary: dict[str, Any] | None = None,
    model_provider: str | None = None,
    model_model: str | None = None,
) -> IntentDecisionRecord:
    decision_payload = decision.to_dict() if isinstance(decision, IntentDecision) else dict(decision)
    if model_provider is None and decision_payload.get("model_provider"):
        model_provider = str(decision_payload["model_provider"])
    if model_model is None and decision_payload.get("model_model"):
        model_model = str(decision_payload["model_model"])
    safe_decision = _redact_value(home, decision_payload)
    safe_context = _redact_value(home, context_summary or {})
    row = IntentDecisionRecord(
        id=str(uuid4()),
        conversation_id=conversation_id,
        run_id=run_id,
        agent_run_id=agent_run_id,
        channel=channel,
        external_user_id=external_user_id,
        input_hash=sha256(text.encode("utf-8")).hexdigest(),
        schema_version=str(decision_payload.get("schema_version") or "1"),
        intent_type=str(decision_payload.get("intent_type") or "unknown"),
        confidence=float(decision_payload.get("confidence") or 0.0),
        risk_level=str(decision_payload.get("risk_level") or "low"),
        source=str(decision_payload.get("source") or "rule"),
        route_type=route_type,
        decision_json=json.dumps(safe_decision, ensure_ascii=False, sort_keys=True),
        context_summary_json=json.dumps(safe_context, ensure_ascii=False, sort_keys=True),
        model_provider=model_provider,
        model_model=model_model,
    )
    session.add(row)
    record_event(
        session,
        "intent.decided",
        {
            "intent_decision_id": row.id,
            "intent_type": row.intent_type,
            "risk_level": row.risk_level,
            "route_type": route_type,
            "agent_run_id": agent_run_id,
        },
        run_id=run_id,
    )
    return row


def intent_record_to_dict(row: IntentDecisionRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "conversation_id": row.conversation_id,
        "run_id": row.run_id,
        "agent_run_id": row.agent_run_id,
        "channel": row.channel,
        "external_user_id": row.external_user_id,
        "input_hash": row.input_hash,
        "schema_version": row.schema_version,
        "intent_type": row.intent_type,
        "confidence": row.confidence,
        "risk_level": row.risk_level,
        "source": row.source,
        "route_type": row.route_type,
        "decision": _json_dict(row.decision_json),
        "context_summary": _json_dict(row.context_summary_json),
        "model_provider": row.model_provider,
        "model_model": row.model_model,
        "created_at": row.created_at.isoformat(),
    }


def _redact_value(home: Path, value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(home, value)
    if isinstance(value, list):
        return [_redact_value(home, item) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact_value(home, item) for key, item in value.items()}
    return value


def _json_dict(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}
