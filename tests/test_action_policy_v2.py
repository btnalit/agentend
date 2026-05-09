import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from agentend.core.action_policy import decide_action, record_action_decision
from agentend.db.models import ActionPolicyDecision, Conversation, EventLog, Run
from agentend.db.session import init_database, session_scope


def _run(session) -> Run:
    conversation = Conversation(id=str(uuid4()), channel="test", external_user_id="local", title="policy v2")
    run = Run(
        id=str(uuid4()),
        conversation_id=conversation.id,
        workflow_id="policy_v2_fixture",
        status="running",
        input_json="{}",
        result_json="{}",
    )
    session.add(conversation)
    session.add(run)
    session.flush()
    return run


def test_decide_action_returns_v2_payload_for_external_write_confirmation() -> None:
    decision = decide_action(
        side_effect="external_write",
        run_mode="normal",
        channel="cli",
        input_data={"dry_run": False},
    )

    payload = decision.to_payload()

    assert decision.decision == "require_clarification"
    assert payload["reason_code"] == "external_write_requires_confirmation"
    assert payload["risk_level"] == "high"
    assert payload["target"] == "external"
    assert payload["operation"] == "publish"
    assert payload["visibility"] == "external"
    assert payload["idempotency"] == "non_idempotent"
    assert payload["requires_preview"] is True
    assert payload["requires_user_confirmation"] is True


def test_dry_run_external_write_is_allowed_but_still_explained() -> None:
    decision = decide_action(
        side_effect="external_write",
        run_mode="normal",
        channel="cli",
        input_data={"dry_run": True},
    )

    payload = decision.to_payload()

    assert decision.decision == "allow"
    assert payload["reason_code"] == "external_write_dry_run_allowed"
    assert payload["risk_level"] == "low"
    assert payload["requires_user_confirmation"] is False


def test_record_action_decision_writes_v2_event_payload(tmp_path) -> None:
    home = tmp_path / "agentend-home"
    init_database(home)
    with session_scope(home) as session:
        run = _run(session)

        with pytest.raises(PermissionError, match="network_write is blocked during scheduler"):
            record_action_decision(
                session,
                run_id=run.id,
                step_id=None,
                tool_name="http.request",
                side_effect="network_write",
                run_mode="scheduler",
                channel="scheduler",
                input_data={"method": "POST"},
            )

        row = session.execute(select(ActionPolicyDecision).where(ActionPolicyDecision.run_id == run.id)).scalar_one()
        event = session.execute(select(EventLog).where(EventLog.event_type == "policy.decided.v2")).scalar_one()
        payload = json.loads(event.payload_json)

    assert row.decision == "block"
    assert row.side_effect == "network_write"
    assert payload["tool_name"] == "http.request"
    assert payload["decision"] == "block"
    assert payload["reason_code"] == "scheduler_network_write_blocked"
    assert payload["risk_level"] == "high"
    assert payload["actor"] == "scheduler"
    assert payload["channel"] == "scheduler"
    assert payload["target"] == "external"
