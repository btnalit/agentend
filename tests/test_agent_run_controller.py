import json
import re
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import AgentIteration, AgentRun, CapabilityEffectiveness, MemoryCandidate
from agentend.db.session import session_scope


def _agent_run_id(output: str) -> str:
    match = re.search(r"AgentRun:\s+([0-9a-f-]+)", output)
    assert match is not None, output
    return match.group(1)


def test_agent_run_cli_records_iteration_progress_effectiveness_and_memory_candidate(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    result = runner.invoke(
        app,
        [
            "agent",
            "run",
            "--home",
            str(home),
            "--max-iterations",
            "2",
            "List the current project test command and explain the evidence.",
        ],
    )

    assert result.exit_code == 0, result.output
    agent_run_id = _agent_run_id(result.output)
    assert "Status: completed" in result.output
    with session_scope(home) as session:
        agent_run = session.get(AgentRun, agent_run_id)
        assert agent_run is not None
        assert agent_run.status == "completed"
        assert agent_run.stop_reason == "success"
        assert agent_run.heartbeat_at is not None
        final_result = json.loads(agent_run.final_result_json)
        assert final_result["content"]
        assert final_result["progress_artifact_id"]
        iteration = session.execute(
            select(AgentIteration).where(AgentIteration.agent_run_id == agent_run_id)
        ).scalar_one()
        action = json.loads(iteration.selected_action_json)
        observation = json.loads(iteration.observation_json)
        evaluation = json.loads(iteration.evaluation_json)
        assert action["type"] in {"skill_run", "tool_call", "workflow_run"}
        assert action.get("no_tool_reason") in (None, "")
        assert observation["status"] == "completed"
        assert evaluation["complete"] is True
        assert iteration.progress_artifact_id == final_result["progress_artifact_id"]
        assert session.execute(select(CapabilityEffectiveness)).first() is not None
        candidate = session.execute(
            select(MemoryCandidate).where(MemoryCandidate.agent_run_id == agent_run_id)
        ).scalar_one()
        assert candidate.merge_key
        assert candidate.status in {"created", "merged", "pending"}


def test_agent_show_and_iterations_are_readable(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    ran = runner.invoke(app, ["agent", "run", "--home", str(home), "Read README and summarize test command."])
    assert ran.exit_code == 0, ran.output
    agent_run_id = _agent_run_id(ran.output)

    shown = runner.invoke(app, ["agent", "show", agent_run_id, "--home", str(home)])
    iterations = runner.invoke(app, ["agent", "iterations", agent_run_id, "--home", str(home)])
    missing = runner.invoke(app, ["agent", "show", "missing", "--home", str(home)])

    assert shown.exit_code == 0, shown.output
    payload = json.loads(shown.output)
    assert payload["id"] == agent_run_id
    assert payload["status"] == "completed"
    assert payload["iterations"][0]["iteration_index"] == 1
    assert iterations.exit_code == 0, iterations.output
    assert agent_run_id in iterations.output
    assert "selected_action" in iterations.output
    assert missing.exit_code != 0
    assert "Unknown agent run" in missing.output
