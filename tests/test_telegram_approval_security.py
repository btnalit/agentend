"""Tests for Telegram approval security fixes (Task R2).

Bug 1 — Auth bypass: any user can approve another's run.
Bug 3 — TOCTOU: approve_clarification + enqueue_resume_intent must be atomic.
"""
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
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
# Bug 1 — auth check helpers
# ---------------------------------------------------------------------------

def test_external_user_id_differs_by_user(tmp_path: Path) -> None:
    """_telegram_external_user_id must produce distinct values for different users."""
    from agentend.telegram_bot import _telegram_external_user_id

    assert _telegram_external_user_id("100", "1") != _telegram_external_user_id("100", "2")


def test_external_user_id_differs_by_chat(tmp_path: Path) -> None:
    """_telegram_external_user_id must produce distinct values for different chats."""
    from agentend.telegram_bot import _telegram_external_user_id

    assert _telegram_external_user_id("100", "1") != _telegram_external_user_id("200", "1")


def test_external_user_id_same_for_same_inputs(tmp_path: Path) -> None:
    """_telegram_external_user_id is deterministic."""
    from agentend.telegram_bot import _telegram_external_user_id

    assert _telegram_external_user_id("100", "42") == _telegram_external_user_id("100", "42")


# ---------------------------------------------------------------------------
# Bug 3 — atomic approve_clarification
# ---------------------------------------------------------------------------

def _make_approval_fixtures(home: Path) -> tuple[str, str, str]:
    """Create AgentRun + linked Run + ClarificationRequest; return (agent_run_id, run_id, req_id)."""
    agent_run_id = str(uuid4())
    linked_run_id = str(uuid4())
    conv_id = str(uuid4())
    iter_id = str(uuid4())
    req_id = str(uuid4())

    with session_scope(home) as session:
        session.add(Conversation(id=conv_id, channel="telegram", external_user_id="100:42"))
        session.add(Run(
            id=linked_run_id, conversation_id=conv_id, workflow_id="simple_chat",
            status="waiting_input", input_json="{}", result_json="{}",
            agent_profile_path="", agent_profile_hash="",
            llm_provider="fake", llm_model="fake",
        ))
        session.add(AgentRun(
            id=agent_run_id, channel="telegram", external_user_id="100:42",
            goal="test goal", status="waiting_input",
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

    return agent_run_id, linked_run_id, req_id


def test_approve_clarification_creates_task_item(tmp_path: Path) -> None:
    """approve_clarification must persist a TaskItem in the DB after approval."""
    home = _init_home(tmp_path)
    agent_run_id, _, req_id = _make_approval_fixtures(home)

    from agentend.telegram_bot import TelegramMessageRouter
    router = TelegramMessageRouter(home)
    result = router.approve_clarification(req_id, agent_run_id)

    assert "已批准" in result or "✅" in result

    with session_scope(home) as session:
        task = session.execute(
            select(TaskItem).where(TaskItem.agent_run_id == agent_run_id)
        ).scalars().first()
        assert task is not None, "TaskItem must be created after approve_clarification"
        assert task.status == "pending"
        assert task.workflow_id == "agent.resume"
        assert task.source == "approval"


def test_approve_clarification_marks_clarification_answered(tmp_path: Path) -> None:
    """approve_clarification must set clarification.status = 'answered'."""
    home = _init_home(tmp_path)
    agent_run_id, _, req_id = _make_approval_fixtures(home)

    from agentend.telegram_bot import TelegramMessageRouter
    router = TelegramMessageRouter(home)
    router.approve_clarification(req_id, agent_run_id)

    with session_scope(home) as session:
        clarification = session.get(ClarificationRequest, req_id)
        assert clarification is not None
        assert clarification.status == "answered"


def test_approve_clarification_idempotent_task_enqueue(tmp_path: Path) -> None:
    """Calling approve_clarification twice must not create duplicate TaskItems."""
    home = _init_home(tmp_path)
    agent_run_id, _, req_id = _make_approval_fixtures(home)

    from agentend.telegram_bot import TelegramMessageRouter
    router = TelegramMessageRouter(home)
    router.approve_clarification(req_id, agent_run_id)
    # Second call — clarification is already answered, should bail early
    router.approve_clarification(req_id, agent_run_id)

    with session_scope(home) as session:
        tasks = session.execute(
            select(TaskItem).where(TaskItem.agent_run_id == agent_run_id)
        ).scalars().all()
        assert len(tasks) == 1, "Must not create duplicate TaskItems"


def test_approve_clarification_expired_returns_error_msg(tmp_path: Path) -> None:
    """approve_clarification on a non-pending clarification returns an error message."""
    home = _init_home(tmp_path)
    agent_run_id, _, req_id = _make_approval_fixtures(home)

    # Mark it cancelled first
    with session_scope(home) as session:
        clarification = session.get(ClarificationRequest, req_id)
        clarification.status = "cancelled"

    from agentend.telegram_bot import TelegramMessageRouter
    router = TelegramMessageRouter(home)
    result = router.approve_clarification(req_id, agent_run_id)

    assert "已过期" in result or "已处理" in result
