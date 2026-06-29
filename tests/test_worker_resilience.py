"""Tests for AgentWorker resilience fixes (R1).

Bug 1: resume() ValueError must be caught; worker must not crash.
Bug 2: enqueue_resume_intent must treat 'running' tasks as already-enqueued.
"""
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.core.worker import AgentWorker
from agentend.db.models import (
    AgentIteration,
    AgentRun,
    ClarificationRequest,
    Conversation,
    Run,
    TaskItem,
)
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
    with session_scope(home) as session:
        count = len(
            session.execute(
                select(TaskItem).where(TaskItem.agent_run_id == run_id)
            ).scalars().all()
        )
    assert count == 1


# ---------------------------------------------------------------------------
# End-to-end integration: approve → enqueue → worker → resume (Finding 2)
# ---------------------------------------------------------------------------

def test_approve_then_worker_resumes_run(tmp_path: Path) -> None:
    """Full path: approve_clarification → TaskItem in DB → AgentWorker.run_once() → resume() called."""
    from agentend.telegram_bot import TelegramMessageRouter

    home = _init_home(tmp_path)

    # Set up DB fixtures: AgentRun waiting for input, linked ClarificationRequest
    agent_run_id = str(uuid4())
    linked_run_id = str(uuid4())
    conv_id = str(uuid4())
    iter_id = str(uuid4())
    req_id = str(uuid4())

    with session_scope(home) as session:
        session.add(Conversation(
            id=conv_id, channel="telegram", external_user_id="100:42",
        ))
        session.add(Run(
            id=linked_run_id, conversation_id=conv_id, workflow_id="simple_chat",
            status="waiting_input", input_json="{}", result_json="{}",
            agent_profile_path="", agent_profile_hash="",
            llm_provider="fake", llm_model="fake",
        ))
        session.add(AgentRun(
            id=agent_run_id, channel="telegram", external_user_id="100:42",
            goal="integration test goal", status="waiting_input",
            stop_reason="clarification_required", max_iterations=3,
        ))
        session.add(AgentIteration(
            id=iter_id, agent_run_id=agent_run_id, iteration_index=1,
            status="waiting", linked_run_id=linked_run_id,
        ))
        session.add(ClarificationRequest(
            id=req_id, run_id=linked_run_id, step_id=iter_id,
            request_type="tool_confirm", question="确认操作？",
            status="pending", resume_token=str(uuid4()),
            free_text_allowed="true",
        ))

    # Step 1: approve_clarification must succeed and create a TaskItem
    router = TelegramMessageRouter(home)
    msg = router.approve_clarification(req_id, agent_run_id)
    assert "已批准" in msg or "✅" in msg

    # Step 2: verify TaskItem exists with status=pending
    with session_scope(home) as session:
        task = session.execute(
            select(TaskItem).where(TaskItem.agent_run_id == agent_run_id)
        ).scalars().first()
    assert task is not None, "TaskItem must be created after approve_clarification"
    assert task.status == "pending"

    # Step 3: AgentWorker.run_once() must call resume() with the correct agent_run_id
    mock_result = MagicMock()
    mock_result.agent_run_id = agent_run_id

    with patch("agentend.core.worker.AgentRunController") as MockCtrl:
        MockCtrl.return_value.resume.return_value = mock_result
        worker_result = AgentWorker(home).run_once()

    # Step 4: verify resume() was called with the correct arguments
    MockCtrl.return_value.resume.assert_called_once_with(
        agent_run_id, max_iterations=3, run_mode="normal"
    )
    assert worker_result.processed_tasks == 1
