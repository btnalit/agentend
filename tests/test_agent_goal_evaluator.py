import json
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.core.agent_evaluator import evaluate_goal_observation, infer_goal_requirements
from agentend.core.agent_run import AgentRunController
from agentend.db.models import AgentEvaluationEvent
from agentend.db.session import session_scope


def test_goal_requirements_and_deterministic_evaluation() -> None:
    requirements = infer_goal_requirements("List the project test command and explain evidence.", {})

    assert any(requirement.id == "test_command_evidence" for requirement in requirements)

    echo_only = evaluate_goal_observation(
        "List the project test command and explain evidence.",
        {"status": "completed", "output": "Goal: List the project test command and explain evidence.\nPython 3.13.7"},
        iteration_index=1,
        max_iterations=1,
    )
    assert echo_only["complete"] is False
    assert "test_command_evidence" in echo_only["missing_requirements"]
    assert echo_only["next_probe"] == "shell.run"

    pytest_goal_echo = evaluate_goal_observation(
        "Run pytest and report evidence.",
        {"status": "completed", "output": "Goal: Run pytest and report evidence.\nNo command executed."},
        iteration_index=1,
        max_iterations=1,
    )
    assert pytest_goal_echo["complete"] is False
    assert pytest_goal_echo["evidence_refs"] == []

    pytest_output = evaluate_goal_observation(
        "List the project test command and explain evidence.",
        {"status": "completed", "output": '{"stdout": "pytest 8.4.2\\n"}'},
        iteration_index=1,
        max_iterations=1,
    )
    assert pytest_output["complete"] is True
    assert "test_command_evidence" in pytest_output["satisfied_requirements"]
    assert pytest_output["confidence"] >= 0.9

    custom_analysis = {
        "requirements": [
            {
                "id": "test_command_evidence",
                "kind": "test_command_evidence",
                "description": "Show concrete test command evidence.",
                "required": True,
                "evidence_hint": "pytest",
            }
        ]
    }
    empty_output = evaluate_goal_observation(
        "List the project test command.",
        {"status": "completed", "output": ""},
        iteration_index=1,
        max_iterations=1,
        goal_analysis=custom_analysis,
    )
    assert empty_output["complete"] is False
    assert "non_empty_observation" in empty_output["missing_requirements"]


def test_agent_run_persists_evaluator_event(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    def irrelevant_completed_action(self, selected, request, *, agent_run_id: str, iteration_id: str) -> dict:
        return {
            "status": "completed",
            "run_id": None,
            "output": "Goal: List the project test command and explain evidence.\nPython 3.13.7",
            "error": None,
        }

    monkeypatch.setattr(AgentRunController, "_execute_action", irrelevant_completed_action)

    result = AgentRunController(home).run(
        "List the project test command and explain evidence.",
        max_iterations=1,
    )

    assert result.status == "failed"
    with session_scope(home) as session:
        event = session.execute(
            select(AgentEvaluationEvent).where(AgentEvaluationEvent.agent_run_id == result.agent_run_id)
        ).scalar_one()
        missing = json.loads(event.missing_requirements_json)
        evidence = json.loads(event.evidence_refs_json)
        assert event.complete == "false"
        assert "test_command_evidence" in missing
        assert event.next_probe == "shell.run"
        assert evidence == []
