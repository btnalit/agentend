"""Tests for Telegram approval security fixes (Task R2).

Bug 1 — Auth bypass: any user can approve another's run.
Bug 3 — TOCTOU: approve_clarification + enqueue_resume_intent must be atomic.
"""
from pathlib import Path
from uuid import uuid4

import os
import sys

import pytest
from sqlalchemy import select
from typer.testing import CliRunner
from unittest.mock import AsyncMock, MagicMock, patch

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

def _make_approval_fixtures(
    home: Path, *, external_user_id: str = "100:42"
) -> tuple[str, str, str]:
    """Create AgentRun + linked Run + ClarificationRequest; return (agent_run_id, run_id, req_id)."""
    agent_run_id = str(uuid4())
    linked_run_id = str(uuid4())
    conv_id = str(uuid4())
    iter_id = str(uuid4())
    req_id = str(uuid4())

    with session_scope(home) as session:
        session.add(Conversation(id=conv_id, channel="telegram", external_user_id=external_user_id))
        session.add(Run(
            id=linked_run_id, conversation_id=conv_id, workflow_id="simple_chat",
            status="waiting_input", input_json="{}", result_json="{}",
            agent_profile_path="", agent_profile_hash="",
            llm_provider="fake", llm_model="fake",
        ))
        session.add(AgentRun(
            id=agent_run_id, channel="telegram", external_user_id=external_user_id,
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


# ---------------------------------------------------------------------------
# Auth rejection path in handle_approval_callback (Finding 1)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_approval_callback_rejects_unauthorized_user(tmp_path: Path) -> None:
    """A user who does not own the run must receive the rejection message.

    The AgentRun is owned by chat 100, user 1 (external_user_id "100:1").
    The callback arrives from user 2 in the same chat → auth check must block it.
    """
    home = _init_home(tmp_path)

    # Fixtures: run is owned by user "1" in chat "100"
    agent_run_id, _, req_id = _make_approval_fixtures(home, external_user_id="100:1")

    # --- Build mock PTB objects; attacker is user id=2 ---
    edit_text_mock = AsyncMock()
    query = MagicMock()
    query.data = f"approve:{req_id}"
    query.answer = AsyncMock()
    query.edit_message_text = edit_text_mock
    query.from_user = MagicMock()
    query.from_user.id = 2  # different user — not the owner
    query.message = MagicMock()
    query.message.chat.id = 100

    update = MagicMock()
    update.callback_query = query
    update.effective_chat = MagicMock()
    update.effective_chat.id = 100

    # --- Capture handle_approval_callback by running serve_telegram with mocked PTB ---
    handlers_added: list = []

    mock_app_instance = MagicMock()
    mock_app_instance.add_handler = lambda h: handlers_added.append(h)
    mock_app_instance.run_polling = MagicMock(side_effect=SystemExit(0))

    mock_builder = MagicMock()
    mock_builder.token.return_value = mock_builder
    mock_builder.build.return_value = mock_app_instance

    mock_Application = MagicMock()
    mock_Application.builder.return_value = mock_builder

    mock_tg = MagicMock()
    mock_tg_ext = MagicMock()
    mock_tg_ext.Application = mock_Application
    # Identity wrapper so the raw coroutine function ends up in handlers_added
    mock_tg_ext.CallbackQueryHandler = lambda f: f

    with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"}):
        with patch.dict(sys.modules, {"telegram": mock_tg, "telegram.ext": mock_tg_ext}):
            from agentend.telegram_bot import serve_telegram
            try:
                serve_telegram(home)
            except SystemExit:
                pass  # expected: mocked run_polling raises SystemExit(0)

    # The last registered handler is handle_approval_callback
    # (CallbackQueryHandler = identity, so it is the raw coroutine function)
    assert handlers_added, "serve_telegram must register at least one handler"
    handle_approval_callback = handlers_added[-1]

    await handle_approval_callback(update, MagicMock())

    edit_text_mock.assert_called_once_with("您无权操作此审批。")
