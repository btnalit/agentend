import agentend.telegram_bot as bot_module
import agentend.core.worker as worker_module


def test_telegram_bot_does_not_import_agent_run_controller() -> None:
    """D1 守则：telegram_bot 不得直接使用 AgentRunController。"""
    import inspect, agentend.telegram_bot as m
    source = inspect.getsource(m)
    assert "AgentRunController" not in source, \
        "telegram_bot.py must not reference AgentRunController (D1)"


def test_render_goal_view_contains_goal_and_status(tmp_path) -> None:
    from pathlib import Path
    from uuid import uuid4
    from typer.testing import CliRunner
    from agentend.cli import app
    from agentend.db.models import AgentRun
    from agentend.db.session import session_scope
    from agentend.telegram_bot import TelegramMessageRouter

    home = tmp_path / "home"
    CliRunner().invoke(app, ["init", "--home", str(home)])
    run_id = str(uuid4())
    with session_scope(home) as session:
        session.add(AgentRun(
            id=run_id, channel="telegram", external_user_id="tg:42:42",
            goal="整理项目文档", status="running", max_iterations=3,
        ))

    router = TelegramMessageRouter(home)
    view = router.render_goal_view(run_id)

    assert "整理项目文档" in view
    assert run_id[:8] in view


def test_render_goal_view_shows_approve_reject_when_waiting(tmp_path) -> None:
    from pathlib import Path
    from uuid import uuid4
    from typer.testing import CliRunner
    from agentend.cli import app
    from agentend.db.models import AgentRun, AgentIteration, Run, Conversation, ClarificationRequest
    from agentend.db.session import session_scope
    from agentend.telegram_bot import TelegramMessageRouter

    home = tmp_path / "home"
    CliRunner().invoke(app, ["init", "--home", str(home)])

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
            request_type="tool_confirm", question="确认写入文件？",
            status="pending", resume_token=str(uuid4()),
        ))

    router = TelegramMessageRouter(home)
    view = router.render_goal_view(agent_run_id)

    assert "写报告" in view
    # approve/reject text lines removed — buttons are now InlineKeyboardMarkup (Finding 2A fix)
    assert "approve:" + req_id not in view
    assert "reject:" + req_id not in view
    assert "👆 请使用下方按钮审批" in view
