from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4
from typer.testing import CliRunner
from agentend.cli import app
from agentend.db.models import AgentRun, TaskItem, utc_now
from agentend.db.session import session_scope


def _init_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    CliRunner().invoke(app, ["init", "--home", str(home)])
    return home


def _make_waiting_run(home: Path) -> str:
    """Insert a waiting_input AgentRun and matching TaskItem, return agent_run_id."""
    run_id = str(uuid4())
    task_id = str(uuid4())
    with session_scope(home) as session:
        session.add(AgentRun(
            id=run_id, channel="task", external_user_id="test",
            goal="test goal", status="waiting_input",
            stop_reason="clarification_required",
            max_iterations=3,
        ))
        session.add(TaskItem(
            id=task_id, title="Resume test", workflow_id="agent.resume",
            input_text="", status="pending", source="approval",
            agent_run_id=run_id, run_mode="normal",
        ))
    return run_id


def test_worker_resume_calls_resume_not_run(tmp_path: Path) -> None:
    home = _init_home(tmp_path)
    run_id = _make_waiting_run(home)

    mock_result = MagicMock()
    mock_result.agent_run_id = run_id

    with patch("agentend.core.worker.AgentRunController") as MockCtrl:
        MockCtrl.return_value.resume.return_value = mock_result
        from agentend.core.worker import AgentWorker
        result = AgentWorker(home).run_once()

    MockCtrl.return_value.resume.assert_called_once_with(
        run_id, max_iterations=3, run_mode="normal"
    )
    MockCtrl.return_value.run.assert_not_called()
    assert result.processed_tasks == 1


def test_worker_skips_resume_task_when_run_not_waiting(tmp_path: Path) -> None:
    home = _init_home(tmp_path)
    stale_run_id = str(uuid4())
    task_id = str(uuid4())
    with session_scope(home) as session:
        session.add(AgentRun(
            id=stale_run_id, channel="task", external_user_id="test",
            goal="stale", status="completed", max_iterations=3,
        ))
        session.add(TaskItem(
            id=task_id, title="stale", workflow_id="agent.resume",
            input_text="", status="pending", source="approval",
            agent_run_id=stale_run_id, run_mode="normal",
        ))

    with patch("agentend.core.worker.AgentRunController") as MockCtrl:
        from agentend.core.worker import AgentWorker
        AgentWorker(home).run_once()

    MockCtrl.return_value.resume.assert_not_called()
    MockCtrl.return_value.run.assert_not_called()
    with session_scope(home) as session:
        task = session.get(TaskItem, task_id)
        assert task.status == "failed"
