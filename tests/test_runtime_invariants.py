import json
from uuid import uuid4

from sqlalchemy import select

from agentend.core.runtime_invariants import check_run_invariants
from agentend.db.models import (
    ActionPolicyDecision,
    AgentIteration,
    AgentRun,
    ClarificationRequest,
    ContextLedger,
    Conversation,
    CostUsage,
    Run,
    RunStep,
    ToolCall,
)
from agentend.db.session import init_database, session_scope


def _conversation(session, *, channel: str = "test") -> Conversation:
    conversation = Conversation(id=str(uuid4()), channel=channel, external_user_id="local", title="runtime invariant")
    session.add(conversation)
    session.flush()
    return conversation


def _run_with_step(session, conversation: Conversation, *, status: str = "completed", node_id: str = "node") -> tuple[Run, RunStep]:
    run = Run(
        id=str(uuid4()),
        conversation_id=conversation.id,
        workflow_id="runtime_invariant_fixture",
        status=status,
        input_json="{}",
        result_json="{}",
    )
    step = RunStep(
        id=str(uuid4()),
        run_id=run.id,
        node_id=node_id,
        status=status,
        input_json="{}",
        output_json="{}",
    )
    session.add(run)
    session.add(step)
    session.flush()
    return run, step


def _codes(issues) -> set[str]:
    return {issue.code for issue in issues}


def test_waiting_input_agent_run_requires_pending_clarification(tmp_path) -> None:
    home = tmp_path / "agentend-home"
    init_database(home)
    with session_scope(home) as session:
        conversation = _conversation(session)
        run, step = _run_with_step(session, conversation, status="waiting_input", node_id="intent.clarification")
        agent_run = AgentRun(
            id=str(uuid4()),
            conversation_id=conversation.id,
            channel="test",
            external_user_id="local",
            goal="write file",
            status="waiting_input",
            final_result_json=json.dumps({"linked_run_id": run.id}),
            stop_reason="clarification_required",
        )
        session.add(agent_run)
        session.flush()

        missing = check_run_invariants(session, agent_run_id=agent_run.id)

        session.add(
            ClarificationRequest(
                id=str(uuid4()),
                run_id=run.id,
                step_id=step.id,
                request_type="missing_input",
                question="What path?",
                resume_token=str(uuid4()),
                status="pending",
            )
        )
        session.flush()
        clean = check_run_invariants(session, agent_run_id=agent_run.id)

    assert "waiting_input_missing_clarification" in _codes(missing)
    assert "waiting_input_missing_clarification" not in _codes(clean)


def test_tool_calls_and_llm_calls_require_audit_links(tmp_path) -> None:
    home = tmp_path / "agentend-home"
    init_database(home)
    with session_scope(home) as session:
        conversation = _conversation(session)
        run, step = _run_with_step(session, conversation)
        tool_call = ToolCall(
            id=str(uuid4()),
            run_id=run.id,
            step_id=step.id,
            tool_name="fs.read_text",
            input_json="{}",
            output_json="{}",
            status="completed",
        )
        usage = CostUsage(
            id=str(uuid4()),
            run_id=run.id,
            step_id=step.id,
            workflow_id="runtime_invariant_fixture",
            model_stage="workflow_step",
            provider="fake",
            model="fake-model",
        )
        session.add(tool_call)
        session.add(usage)
        session.flush()

        missing = check_run_invariants(session, run_id=run.id)

        session.add(
            ActionPolicyDecision(
                id=str(uuid4()),
                run_id=run.id,
                step_id=step.id,
                tool_name="fs.read_text",
                decision="allow",
                side_effect="local_read",
                reason="test fixture",
            )
        )
        session.add(
            ContextLedger(
                id=str(uuid4()),
                run_id=run.id,
                workflow_step_id=step.id,
                model_stage="workflow_step",
                model_provider="fake",
                model_model="fake-model",
                estimated_input_tokens=1,
            )
        )
        session.flush()
        clean = check_run_invariants(session, run_id=run.id)

    assert {"tool_call_missing_policy_decision", "llm_call_missing_context_ledger"} <= _codes(missing)
    assert "tool_call_missing_policy_decision" not in _codes(clean)
    assert "llm_call_missing_context_ledger" not in _codes(clean)


def test_repeated_tool_and_llm_calls_require_matching_audit_record_counts(tmp_path) -> None:
    home = tmp_path / "agentend-home"
    init_database(home)
    with session_scope(home) as session:
        conversation = _conversation(session)
        run, step = _run_with_step(session, conversation)
        for _ in range(2):
            session.add(
                ToolCall(
                    id=str(uuid4()),
                    run_id=run.id,
                    step_id=step.id,
                    tool_name="fs.read_text",
                    input_json="{}",
                    output_json="{}",
                    status="completed",
                )
            )
            session.add(
                CostUsage(
                    id=str(uuid4()),
                    run_id=run.id,
                    step_id=step.id,
                    workflow_id="runtime_invariant_fixture",
                    model_stage="workflow_step",
                    provider="fake",
                    model="fake-model",
                )
            )
        session.add(
            ActionPolicyDecision(
                id=str(uuid4()),
                run_id=run.id,
                step_id=step.id,
                tool_name="fs.read_text",
                decision="allow",
                side_effect="local_read",
                reason="only one decision for two calls",
            )
        )
        session.add(
            ContextLedger(
                id=str(uuid4()),
                run_id=run.id,
                workflow_step_id=step.id,
                model_stage="workflow_step",
                model_provider="fake",
                model_model="fake-model",
                estimated_input_tokens=1,
            )
        )
        session.flush()

        issues = check_run_invariants(session, run_id=run.id)

    assert {"tool_call_missing_policy_decision", "llm_call_missing_context_ledger"} <= _codes(issues)


def test_completed_agent_run_cannot_have_active_iteration(tmp_path) -> None:
    home = tmp_path / "agentend-home"
    init_database(home)
    with session_scope(home) as session:
        conversation = _conversation(session)
        agent_run = AgentRun(
            id=str(uuid4()),
            conversation_id=conversation.id,
            channel="test",
            external_user_id="local",
            goal="list tests",
            status="completed",
            stop_reason="success",
        )
        session.add(agent_run)
        session.add(
            AgentIteration(
                id=str(uuid4()),
                agent_run_id=agent_run.id,
                iteration_index=1,
                status="running",
                plan_json="{}",
                selected_action_json="{}",
                observation_json="{}",
                evaluation_json="{}",
            )
        )
        session.flush()

        issues = check_run_invariants(session, agent_run_id=agent_run.id)

    assert "completed_agent_run_has_active_iteration" in _codes(issues)


def test_clean_real_chat_run_satisfies_core_invariants(tmp_path) -> None:
    home = tmp_path / "agentend-home"
    from typer.testing import CliRunner

    from agentend.cli import app

    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    result = runner.invoke(app, ["chat", "--home", str(home), "--message", "hello"])
    assert result.exit_code == 0, result.output

    with session_scope(home) as session:
        run = session.execute(select(Run).order_by(Run.created_at.desc())).scalars().first()
        assert run is not None
        issues = check_run_invariants(session, run_id=run.id)

    assert issues == []
