from pathlib import Path

from typer.testing import CliRunner

from agentend.cli import app
from agentend.core.agent_selector import capability_contract_for, select_next_action_with_trace
from agentend.db.session import session_scope


def test_selector_trace_exposes_capability_contract_and_requirement_match(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    previous = [
        {
            "action_name": "code.local_task",
            "status": "incomplete",
            "error_code": "goal_incomplete",
            "missing_requirements": ["test_command_evidence"],
        }
    ]
    goal_analysis = {
        "candidate_skills": ["code.local_task"],
        "candidate_tools": ["shell.run", "git.status"],
        "requirements": [
            {
                "id": "test_command_evidence",
                "kind": "test_command_evidence",
                "description": "Show concrete test command evidence.",
                "required": True,
                "evidence_hint": "pytest",
            }
        ],
    }

    with session_scope(home) as session:
        result = select_next_action_with_trace(
            home,
            session,
            "List the project test command and explain evidence.",
            goal_analysis,
            previous,
        )

    shell_candidate = next(item for item in result.trace["candidates"] if item["name"] == "shell.run")
    assert "test_command_evidence" in shell_candidate["contract"]["evidence_produced"]
    assert shell_candidate["score_breakdown"]["requirement_match"] > 0
    assert result.selected.name == "shell.run"


def test_capability_contract_for_shell_run_describes_test_evidence() -> None:
    contract = capability_contract_for("tool_call", "shell.run")

    assert "test_command_evidence" in contract["evidence_produced"]
    assert contract["verification_hints"]
    assert "command_failed" in contract["failure_modes"]
