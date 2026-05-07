import json
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.core.agent_run import AgentRunController
from agentend.core.tasks import TaskManager
from agentend.core.worker import AgentWorker
from agentend.db.models import AgentRun, Artifact, TaskItem
from agentend.db.session import session_scope


def test_progress_artifact_includes_long_task_state_envelope(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    result = AgentRunController(home).run(
        "List the project test command and explain evidence.",
        max_iterations=2,
    )

    assert result.status == "completed"
    with session_scope(home) as session:
        artifact = session.get(Artifact, result.progress_artifact_id)
        assert artifact is not None
        payload = json.loads(Path(artifact.path).read_text(encoding="utf-8"))
        progress = payload["progress"]
        assert set(progress) >= {"done", "doing", "next", "blockers", "evidence", "goal_state"}
        assert "test_command_evidence" in progress["goal_state"]["satisfied_requirements"]
        assert progress["blockers"] == []


def test_max_iteration_result_preserves_partial_result_and_resume_cursor(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    def incomplete_action(self, selected, request, *, agent_run_id: str, iteration_id: str) -> dict:
        return {
            "status": "completed",
            "run_id": None,
            "output": "Goal: List the project test command and explain evidence.\nPython 3.13.7",
            "error": None,
        }

    monkeypatch.setattr(AgentRunController, "_execute_action", incomplete_action)

    result = AgentRunController(home).run(
        "List the project test command and explain evidence.",
        max_iterations=1,
    )

    assert result.status == "failed"
    with session_scope(home) as session:
        row = session.get(AgentRun, result.agent_run_id)
        final_result = json.loads(row.final_result_json)
        assert final_result["partial_result"].startswith("Goal:")
        assert "test_command_evidence" in final_result["missing_requirements"]
        assert final_result["resume_cursor"]["agent_run_id"] == result.agent_run_id
        assert final_result["resume_cursor"]["next_probe"] == "shell.run"


def test_worker_resume_cursor_mirrors_long_task_state(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    task = TaskManager(home).add_task(
        workflow_id="simple_chat",
        input_text="List the project test command and explain evidence.",
        title="Long task state",
        source="eval",
    )

    result = AgentWorker(home).run_once()

    assert result.processed_tasks == 1
    with session_scope(home) as session:
        refreshed = session.get(TaskItem, task.id)
        assert refreshed is not None
        cursor = json.loads(refreshed.resume_cursor_json)
        assert cursor["agent_run_id"] == refreshed.agent_run_id
        assert "missing_requirements" in cursor
        assert "partial_result" in cursor
        assert "progress_artifact_id" in cursor
        assert session.execute(select(Artifact).where(Artifact.id == refreshed.progress_artifact_id)).scalar_one()
