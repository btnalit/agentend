import json
import re
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.core.agent_run import AgentRunController
from agentend.core.skills import ensure_builtin_skills
from agentend.db.models import AgentIteration, AgentRun, CapabilityEffectiveness, MemoryCandidate, MemoryItem
from agentend.db.session import session_scope


def _agent_run_id(output: str) -> str:
    match = re.search(r"AgentRun:\s+([0-9a-f-]+)", output)
    assert match is not None, output
    return match.group(1)


def _break_code_skill_first_action(home: Path) -> None:
    with session_scope(home) as session:
        skill = next(row for row in ensure_builtin_skills(home, session) if row.id == "code.local_task")
        workflow_path = Path(skill.workflow_path)
    workflow_path.write_text(
        """id: skill.code.local_task
name: code.local_task
nodes:
  - id: read_missing
    type: tool
    tool: fs.read_text
    input:
      path: __agentend_missing_replan_fixture__.txt
  - id: git_status
    type: tool
    tool: git.status
    input:
      cwd: .
    depends_on: [read_missing]
  - id: python_version
    type: tool
    tool: shell.run
    input:
      command: python --version
    depends_on: [git_status]
  - id: final
    type: final
    depends_on: [python_version]
""",
        encoding="utf-8",
    )


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
        iterations = (
            session.execute(
                select(AgentIteration)
                .where(AgentIteration.agent_run_id == agent_run_id)
                .order_by(AgentIteration.iteration_index)
            )
            .scalars()
            .all()
        )
        assert iterations
        iteration = iterations[-1]
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


def test_agent_resume_appends_to_existing_run_without_rerunning_iterations(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    _break_code_skill_first_action(home)

    first = runner.invoke(
        app,
        [
            "agent",
            "run",
            "--home",
            str(home),
            "--max-iterations",
            "1",
            "List the project test command and explain evidence.",
        ],
    )
    assert first.exit_code == 0, first.output
    agent_run_id = _agent_run_id(first.output)
    assert "Status: failed" in first.output

    resumed = runner.invoke(
        app,
        ["agent", "resume", agent_run_id, "--home", str(home), "--max-iterations", "2"],
    )

    assert resumed.exit_code == 0, resumed.output
    assert _agent_run_id(resumed.output) == agent_run_id
    with session_scope(home) as session:
        agent_run_count = session.execute(select(AgentRun)).scalars().all()
        iterations = (
            session.execute(
                select(AgentIteration)
                .where(AgentIteration.agent_run_id == agent_run_id)
                .order_by(AgentIteration.iteration_index)
            )
            .scalars()
            .all()
        )
        assert len(agent_run_count) == 1
        assert [row.iteration_index for row in iterations] == [1, 2]
        first_action = json.loads(iterations[0].selected_action_json)
        second_action = json.loads(iterations[1].selected_action_json)
        assert first_action["name"] == "code.local_task"
        assert second_action["name"] != first_action["name"]


def test_agent_resume_completed_run_returns_existing_result_without_appending(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    ran = runner.invoke(
        app,
        [
            "agent",
            "run",
            "--home",
            str(home),
            "--max-iterations",
            "2",
            "List the project test command and explain evidence.",
        ],
    )
    assert ran.exit_code == 0, ran.output
    agent_run_id = _agent_run_id(ran.output)
    assert "Status: completed" in ran.output
    with session_scope(home) as session:
        before_count = (
            session.execute(select(AgentIteration).where(AgentIteration.agent_run_id == agent_run_id)).scalars().all()
        )

    resumed = runner.invoke(app, ["agent", "resume", agent_run_id, "--home", str(home), "--max-iterations", "2"])

    assert resumed.exit_code == 0, resumed.output
    assert _agent_run_id(resumed.output) == agent_run_id
    assert "Status: completed" in resumed.output
    with session_scope(home) as session:
        after_count = (
            session.execute(select(AgentIteration).where(AgentIteration.agent_run_id == agent_run_id)).scalars().all()
        )
        assert len(after_count) == len(before_count)


def test_agent_resume_cancelled_run_is_rejected_without_appending(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    _break_code_skill_first_action(home)

    ran = runner.invoke(
        app,
        [
            "agent",
            "run",
            "--home",
            str(home),
            "--max-iterations",
            "1",
            "List the project test command and explain evidence.",
        ],
    )
    assert ran.exit_code == 0, ran.output
    agent_run_id = _agent_run_id(ran.output)
    with session_scope(home) as session:
        row = session.get(AgentRun, agent_run_id)
        assert row is not None
        row.status = "cancelled"
        before_count = (
            session.execute(select(AgentIteration).where(AgentIteration.agent_run_id == agent_run_id)).scalars().all()
        )

    resumed = runner.invoke(app, ["agent", "resume", agent_run_id, "--home", str(home), "--max-iterations", "2"])

    assert resumed.exit_code != 0
    assert "Cannot resume cancelled agent run" in resumed.output
    with session_scope(home) as session:
        after_count = (
            session.execute(select(AgentIteration).where(AgentIteration.agent_run_id == agent_run_id)).scalars().all()
        )
        assert len(after_count) == len(before_count)


def test_test_command_goal_does_not_complete_without_test_command_evidence(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    def irrelevant_completed_action(self, selected, request, *, agent_run_id: str, iteration_id: str) -> dict:
        return {
            "status": "completed",
            "run_id": None,
            "output": "Goal: List the project test command and explain the evidence.\nPython 3.13.7",
            "error": None,
        }

    monkeypatch.setattr(AgentRunController, "_execute_action", irrelevant_completed_action)

    result = AgentRunController(home).run(
        "List the project test command and explain the evidence.",
        max_iterations=1,
    )

    assert result.status == "failed"
    assert result.stop_reason == "max_iterations_reached"
    with session_scope(home) as session:
        agent_run = session.get(AgentRun, result.agent_run_id)
        final_result = json.loads(agent_run.final_result_json)
        assert "test command evidence" in " ".join(final_result["incomplete_conditions"])


def test_resume_success_refreshes_memory_candidates_after_initial_failure(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    _break_code_skill_first_action(home)

    first = runner.invoke(
        app,
        [
            "agent",
            "run",
            "--home",
            str(home),
            "--max-iterations",
            "1",
            "List the project test command and explain evidence.",
        ],
    )
    assert first.exit_code == 0, first.output
    agent_run_id = _agent_run_id(first.output)
    assert "Status: failed" in first.output

    resumed = runner.invoke(
        app,
        ["agent", "resume", agent_run_id, "--home", str(home), "--max-iterations", "2"],
    )

    assert resumed.exit_code == 0, resumed.output
    with session_scope(home) as session:
        candidates = (
            session.execute(select(MemoryCandidate).where(MemoryCandidate.agent_run_id == agent_run_id))
            .scalars()
            .all()
        )
        candidate_types = {candidate.type for candidate in candidates}
        memories = session.execute(select(MemoryItem).where(MemoryItem.source == "agent_consolidator")).scalars().all()
        assert "failure_lesson" in candidate_types
        assert "successful_procedure" in candidate_types
        assert any(memory.scope == "project" and memory.status == "active" for memory in memories)


def test_resume_reconstructed_observations_preserve_missing_requirements(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    original_execute_action = AgentRunController._execute_action

    def irrelevant_completed_action(self, selected, request, *, agent_run_id: str, iteration_id: str) -> dict:
        return {
            "status": "completed",
            "run_id": None,
            "output": "Goal: List the project test command and explain evidence.\nPython 3.13.7",
            "error": None,
        }

    monkeypatch.setattr(AgentRunController, "_execute_action", irrelevant_completed_action)
    failed = AgentRunController(home).run(
        "List the project test command and explain evidence.",
        max_iterations=1,
    )
    assert failed.status == "failed"

    monkeypatch.setattr(AgentRunController, "_execute_action", original_execute_action)
    resumed = AgentRunController(home).resume(failed.agent_run_id, max_iterations=1)

    assert resumed.status == "completed"
    with session_scope(home) as session:
        iteration = (
            session.execute(
                select(AgentIteration)
                .where(AgentIteration.agent_run_id == failed.agent_run_id)
                .where(AgentIteration.iteration_index == 2)
            )
            .scalars()
            .one()
        )
        plan = json.loads(iteration.plan_json)
        previous = plan["previous_observations"][0]
        shell_candidate = next(item for item in plan["selector_trace"]["candidates"] if item["name"] == "shell.run")
        assert previous["missing_requirements"] == ["test_command_evidence"]
        assert shell_candidate["score_breakdown"]["requirement_match"] > 0
