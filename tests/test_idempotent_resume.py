import json
import re
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.core.agent_run import AgentRunController
from agentend.db.models import (
    AgentIteration,
    AgentRun,
    ClarificationRequest,
    Conversation,
    EventLog,
    Run,
    RunStep,
    ToolCall,
    ToolContractSnapshot,
)
from agentend.db.session import session_scope


def _run_id(output: str) -> str:
    match = re.search(r"Run:\s+([0-9a-f-]+)", output)
    assert match is not None, output
    return match.group(1)


def test_tool_contract_snapshots_include_idempotency_metadata(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    (home / "input.txt").write_text("stable", encoding="utf-8")
    (home / "workflows" / "definitions" / "idempotency_snapshot.yaml").write_text(
        """id: idempotency_snapshot
name: Idempotency Snapshot
nodes:
  - id: read
    type: tool
    tool: fs.read_text
    input:
      path: input.txt
  - id: write
    type: tool
    tool: fs.write_text
    input:
      path: output.txt
      content: "{read}"
  - id: final
    type: final
    depends_on: [write]
""",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["workflows", "run", "idempotency_snapshot", "--home", str(home), "--input", "x"])

    assert result.exit_code == 0, result.output
    with session_scope(home) as session:
        run_id = _run_id(result.output)
        snapshots = {
            row.tool_name: json.loads(row.contract_json)
            for row in session.execute(select(ToolContractSnapshot).where(ToolContractSnapshot.run_id == run_id)).scalars()
        }

    read_contract = snapshots["fs.read_text"]
    write_contract = snapshots["fs.write_text"]
    assert read_contract["idempotent"] is True
    assert read_contract["idempotency_key_supported"] is False
    assert read_contract["preview_supported"] is False
    assert read_contract["dry_run_supported"] is False
    assert write_contract["idempotent"] is False
    assert write_contract["preview_supported"] is True
    assert write_contract["dry_run_supported"] is False
    assert "overwrite the same target" in write_contract["compensation_hint"]


def test_replay_plan_marks_reused_idempotent_reads_and_blocked_non_idempotent_writes(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    (home / "input.txt").write_text("stable", encoding="utf-8")
    (home / "workflows" / "definitions" / "idempotency_replay.yaml").write_text(
        """id: idempotency_replay
name: Idempotency Replay
nodes:
  - id: read
    type: tool
    tool: fs.read_text
    input:
      path: input.txt
  - id: write
    type: tool
    tool: fs.write_text
    input:
      path: output.txt
      content: "{read}"
  - id: final
    type: final
    depends_on: [write]
""",
        encoding="utf-8",
    )
    source = runner.invoke(app, ["workflows", "run", "idempotency_replay", "--home", str(home), "--input", "x"])
    source_run_id = _run_id(source.output)

    dry_run = runner.invoke(app, ["runs", "replay", source_run_id, "--home", str(home), "--dry-run"])

    assert dry_run.exit_code == 0, dry_run.output
    plan = json.loads(dry_run.output)
    read_step = next(step for step in plan["steps"] if step["node_id"] == "read")
    write_step = next(step for step in plan["steps"] if step["node_id"] == "write")
    assert read_step["strategy"] == "reuse_output"
    assert read_step["idempotency"] == "idempotent"
    assert read_step["replay_action"] == "reuse_recorded_output"
    assert write_step["strategy"] == "block"
    assert write_step["idempotency"] == "non_idempotent"
    assert write_step["replay_action"] == "manual_review_required"
    assert write_step["preview_supported"] is True
    assert "non-idempotent" in write_step["skip_reason"]


def test_replay_reused_tool_calls_keep_idempotency_metadata(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    (home / "input.txt").write_text("stable", encoding="utf-8")
    (home / "workflows" / "definitions" / "idempotent_read_replay.yaml").write_text(
        """id: idempotent_read_replay
name: Idempotent Read Replay
nodes:
  - id: read
    type: tool
    tool: fs.read_text
    input:
      path: input.txt
  - id: final
    type: final
    depends_on: [read]
""",
        encoding="utf-8",
    )
    source = runner.invoke(app, ["workflows", "run", "idempotent_read_replay", "--home", str(home), "--input", "x"])
    source_run_id = _run_id(source.output)

    replayed = runner.invoke(app, ["runs", "replay", source_run_id, "--home", str(home)])

    assert replayed.exit_code == 0, replayed.output
    replay_run_id = _run_id(replayed.output)
    with session_scope(home) as session:
        reused = (
            session.execute(select(ToolCall).where(ToolCall.run_id == replay_run_id).where(ToolCall.tool_name == "fs.read_text"))
            .scalars()
            .one()
        )
        snapshot = (
            session.execute(
                select(ToolContractSnapshot)
                .where(ToolContractSnapshot.run_id == replay_run_id)
                .where(ToolContractSnapshot.tool_name == "fs.read_text")
            )
            .scalars()
            .one()
        )
        contract = json.loads(snapshot.contract_json)

    assert reused.status == "reused"
    assert contract["idempotent"] is True


def _create_agent_run_with_running_non_idempotent_call(home: Path) -> tuple[str, str]:
    with session_scope(home) as session:
        conversation = Conversation(id=str(uuid4()), channel="cli", external_user_id="local", title="resume review")
        agent_run = AgentRun(
            id=str(uuid4()),
            conversation_id=conversation.id,
            channel="cli",
            external_user_id="local",
            goal="continue after uncertain file write",
            goal_package_json="{}",
            status="failed",
            stop_reason="crashed",
            final_result_json="{}",
            max_iterations=1,
        )
        run = Run(
            id=str(uuid4()),
            conversation_id=conversation.id,
            workflow_id="agent.tool_call",
            status="failed",
            input_json="{}",
            result_json="{}",
            error="process crashed after starting write",
        )
        step = RunStep(
            id=str(uuid4()),
            run_id=run.id,
            node_id="agent.1",
            status="running",
            input_json='{"path":"out.txt","content":"value"}',
            output_json="{}",
        )
        tool_call = ToolCall(
            id=str(uuid4()),
            run_id=run.id,
            step_id=step.id,
            tool_name="fs.write_text",
            input_json='{"path":"out.txt","content":"value"}',
            output_json="{}",
            status="running",
        )
        iteration = AgentIteration(
            id=str(uuid4()),
            agent_run_id=agent_run.id,
            iteration_index=1,
            status="failed",
            selected_action_json=json.dumps({"type": "tool_call", "name": "fs.write_text"}),
            observation_json=json.dumps({"status": "failed", "run_id": run.id, "tool_call_id": tool_call.id}),
            evaluation_json=json.dumps({"complete": False}),
            linked_run_id=run.id,
            linked_tool_call_id=tool_call.id,
            error="process crashed after starting write",
        )
        snapshot = ToolContractSnapshot(
            id=str(uuid4()),
            run_id=run.id,
            tool_name="fs.write_text",
            contract_json=json.dumps(
                {
                    "name": "fs.write_text",
                    "side_effect": "local_write",
                    "idempotent": False,
                    "preview_supported": True,
                    "dry_run_supported": False,
                    "compensation_supported": False,
                    "compensation_hint": "inspect target before continuing",
                },
                sort_keys=True,
            ),
        )
        session.add_all([conversation, agent_run, run, step, tool_call, iteration, snapshot])
        return agent_run.id, tool_call.id


def test_agent_resume_requires_manual_review_for_running_non_idempotent_tool_call(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    agent_run_id, tool_call_id = _create_agent_run_with_running_non_idempotent_call(home)

    result = AgentRunController(home).resume(agent_run_id, max_iterations=1)

    assert result.status == "waiting_input"
    assert result.stop_reason == "resume_manual_review_required"
    assert "fs.write_text" in result.content
    with session_scope(home) as session:
        refreshed = session.get(AgentRun, agent_run_id)
        iterations = session.execute(select(AgentIteration).where(AgentIteration.agent_run_id == agent_run_id)).scalars().all()
        request = session.execute(select(ClarificationRequest).where(ClarificationRequest.status == "pending")).scalar_one()
        event = session.execute(select(EventLog).where(EventLog.event_type == "resume.manual_review_required")).scalar_one()
        event_payload = json.loads(event.payload_json)

    assert refreshed.status == "waiting_input"
    assert refreshed.stop_reason == "resume_manual_review_required"
    assert len(iterations) == 1
    assert request.request_type == "resume_manual_review"
    assert "non-idempotent" in request.reason
    assert event_payload["tool_call_id"] == tool_call_id


def test_agent_resume_manual_review_answer_allows_next_resume(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    agent_run_id, tool_call_id = _create_agent_run_with_running_non_idempotent_call(home)
    blocked = AgentRunController(home).resume(agent_run_id, max_iterations=1)

    answered = runner.invoke(
        app,
        ["runs", "resume", str(blocked.linked_run_id), "--home", str(home), "--answer", "reviewed_continue"],
    )
    assert answered.exit_code == 0, answered.output
    with session_scope(home) as session:
        reviewed_agent_run = session.get(AgentRun, agent_run_id)
        review_payload = json.loads(reviewed_agent_run.final_result_json)

    resumed = AgentRunController(home).resume(agent_run_id, max_iterations=1)

    assert resumed.stop_reason != "resume_manual_review_required"
    assert resumed.status != "waiting_input"
    with session_scope(home) as session:
        iterations = session.execute(select(AgentIteration).where(AgentIteration.agent_run_id == agent_run_id)).scalars().all()

    assert review_payload["resume_manual_review"]["decision"] == "reviewed_continue"
    assert review_payload["resume_manual_review"]["resume_issue"]["tool_call_id"] == tool_call_id
    assert len(iterations) > 1
