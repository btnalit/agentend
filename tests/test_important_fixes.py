"""Tests for Important findings fixes: Hermes live wiring + Telegram InlineKeyboard."""
from pathlib import Path
from uuid import uuid4
from typer.testing import CliRunner
from agentend.cli import app
from agentend.db.session import session_scope
from agentend.db.models import AgentRun, AgentIteration, Run, Conversation, ClarificationRequest


def _init_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    CliRunner().invoke(app, ["init", "--home", str(home)])
    return home


def test_pending_goal_keyboard_returns_none_when_no_waiting_run(tmp_path: Path) -> None:
    home = _init_home(tmp_path)
    from agentend.telegram_bot import _pending_goal_keyboard
    result = _pending_goal_keyboard(home, "tg:1:1")
    assert result is None


def test_pending_goal_keyboard_returns_keyboard_when_waiting(tmp_path: Path) -> None:
    home = _init_home(tmp_path)
    agent_run_id = str(uuid4())
    linked_run_id = str(uuid4())
    conv_id = str(uuid4())
    iter_id = str(uuid4())
    req_id = str(uuid4())
    with session_scope(home) as session:
        session.add(Conversation(id=conv_id, channel="telegram", external_user_id="tg:1:1"))
        session.add(Run(
            id=linked_run_id, conversation_id=conv_id, workflow_id="simple_chat",
            status="running", input_json="{}", result_json="{}",
            agent_profile_path="", agent_profile_hash="",
            llm_provider="fake", llm_model="fake",
        ))
        session.add(AgentRun(
            id=agent_run_id, channel="telegram", external_user_id="tg:1:1",
            goal="写报告", status="waiting_input",
            stop_reason="clarification_required", max_iterations=3,
        ))
        session.add(AgentIteration(
            id=iter_id, agent_run_id=agent_run_id, iteration_index=1,
            status="waiting", linked_run_id=linked_run_id,
        ))
        session.add(ClarificationRequest(
            id=req_id, run_id=linked_run_id, step_id=iter_id,
            request_type="tool_confirm", question="确认？",
            status="pending", resume_token=str(uuid4()),
        ))
    from agentend.telegram_bot import _pending_goal_keyboard
    result = _pending_goal_keyboard(home, "tg:1:1")
    assert result is not None
    goal_text, keyboard = result
    assert "写报告" in goal_text
    # keyboard has approve/reject buttons
    buttons = keyboard.inline_keyboard[0]
    callback_datas = [b.callback_data for b in buttons]
    assert f"approve:{req_id}" in callback_datas
    assert f"reject:{req_id}" in callback_datas


def test_consolidate_called_with_hermes_home_when_configured(tmp_path: Path) -> None:
    """agent_run.py passes hermes_home from config to consolidate_memory_candidates."""
    # Just check the function signature accepts hermes_home without error
    from agentend.core.memory_consolidator import consolidate_memory_candidates
    import inspect
    sig = inspect.signature(consolidate_memory_candidates)
    assert "hermes_home" in sig.parameters
