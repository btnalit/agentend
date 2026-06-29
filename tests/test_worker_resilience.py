"""Tests for AgentWorker resilience fixes (R1).

Bug 1: resume() ValueError must be caught; worker must not crash.
Bug 2: enqueue_resume_intent must treat 'running' tasks as already-enqueued.
"""
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from typer.testing import CliRunner

from agentend.cli import app
from agentend.core.worker import AgentWorker
from agentend.db.models import AgentRun, TaskItem
from agentend.db.session import session_scope


def _init_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    CliRunner().invoke(app, ["init", "--home", str(home)])
    return home


# ---------------------------------------------------------------------------
# Bug 1: resume() ValueError must not crash the worker
# ---------------------------------------------------------------------------

def test_worker_resume_cancelled_run_fails_task_does_not_crash(tmp_path: Path) -> None:
    """Worker must not crash if resume() raises ValueError for a cancelled run."""
    home = _init_home(tmp_path)
    run_id = str(uuid4())
    task_id = str(uuid4())

    with session_scope(home) as session:
        session.add(AgentRun(
            id=run_id,
            channel="telegram",
            external_user_id="tg-test",
            goal="cancelled goal",
            status="waiting_input",  # appears waiting at claim time
            max_iterations=3,
        ))
        session.add(TaskItem(
            id=task_id,
            title="Resume test",
            workflow_id="agent.resume",
            input_text="",
            status="pending",
            source="approval",
            agent_run_id=run_id,
            run_mode="normal",
        ))

    # Simulate race: run transitions to 'cancelled' between status check and resume()
    with patch("agentend.core.worker.AgentRunController") as MockCtrl:
        MockCtrl.return_value.resume.side_effect = ValueError(
            f"Cannot resume cancelled agent run: {run_id}"
        )
        result = AgentWorker(home).run_once()

    # Must not raise; must account for the task
    assert result.processed_tasks == 1
    assert result.message == "resume_failed"

    with session_scope(home) as session:
        task = session.get(TaskItem, task_id)
        assert task is not None
        assert task.status == "failed"
        assert "resume_error" in (task.error or "")


def test_worker_resume_unexpected_exception_fails_task_does_not_crash(tmp_path: Path) -> None:
    """Worker must not crash if resume() raises an unexpected Exception."""
    home = _init_home(tmp_path)
    run_id = str(uuid4())
    task_id = str(uuid4())

    with session_scope(home) as session:
        session.add(AgentRun(
            id=run_id,
            channel="task",
            external_user_id="test",
            goal="goal",
            status="waiting_input",
            max_iterations=3,
        ))
        session.add(TaskItem(
            id=task_id,
            title="Resume test",
            workflow_id="agent.resume",
            input_text="",
            status="pending",
            source="approval",
            agent_run_id=run_id,
            run_mode="normal",
        ))

    with patch("agentend.core.worker.AgentRunController") as MockCtrl:
        MockCtrl.return_value.resume.side_effect = RuntimeError("db connection lost")
        result = AgentWorker(home).run_once()

    assert result.processed_tasks == 1
    assert result.message == "resume_failed"

    with session_scope(home) as session:
        task = session.get(TaskItem, task_id)
        assert task.status == "failed"
        assert "resume_error" in (task.error or "")


# ---------------------------------------------------------------------------
# Bug 2: enqueue_resume_intent must deduplicate 'running' tasks
# ---------------------------------------------------------------------------

def test_enqueue_resume_intent_deduplicates_running_task(tmp_path: Path) -> None:
    """A second enqueue_resume_intent call must be a no-op if a running task exists."""
    from agentend.core.tasks import TaskManager

    home = _init_home(tmp_path)
    run_id = str(uuid4())
    mgr = TaskManager(home)

    # First enqueue creates a pending task
    task1 = mgr.enqueue_resume_intent(run_id)
    assert task1 is not None
    assert task1.status == "pending"

    # Simulate a worker claiming it → status becomes 'running'
    with session_scope(home) as session:
        t = session.get(TaskItem, task1.id)
        t.status = "running"

    # Second enqueue must return None (idempotent against running tasks too)
    task2 = mgr.enqueue_resume_intent(run_id)
    assert task2 is None

    # Only one task should exist in DB for this run
    from sqlalchemy import select
    with session_scope(home) as session:
        count = len(
            session.execute(
                select(TaskItem).where(TaskItem.agent_run_id == run_id)
            ).scalars().all()
        )
    assert count == 1
