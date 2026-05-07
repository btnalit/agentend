import re
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import ClarificationRequest, Conversation, Run
from agentend.db.session import session_scope
from agentend.telegram_bot import TelegramMessageRouter


def test_telegram_pending_clarifications_are_scoped_to_chat_and_user(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    _write_ask_workflow(home)
    router = TelegramMessageRouter(home)

    started_one = router.handle_text("chat-1", "user-1", "/run ask_bound first")
    started_two = router.handle_text("chat-2", "user-2", "/run ask_bound second")
    run_one = _run_id(started_one)
    run_two = _run_id(started_two)
    listed = runner.invoke(app, ["clarifications", "list", "--home", str(home)])

    assert "Bound value?" in started_one
    assert "Bound value?" in started_two
    assert listed.exit_code == 0
    assert "channel=telegram" in listed.output
    assert "user=chat-1:user-1" in listed.output
    assert "user=chat-2:user-2" in listed.output

    answered_one = router.handle_text("chat-1", "user-1", "answer-one")

    assert "answer-one" in answered_one
    with session_scope(home) as session:
        first_request = session.execute(select(ClarificationRequest).where(ClarificationRequest.run_id == run_one)).scalar_one()
        second_request = session.execute(select(ClarificationRequest).where(ClarificationRequest.run_id == run_two)).scalar_one()
        first_run = session.get(Run, run_one)
        second_run = session.get(Run, run_two)
        first_conversation = session.get(Conversation, first_run.conversation_id)
        second_conversation = session.get(Conversation, second_run.conversation_id)
        assert first_conversation.external_user_id == "chat-1:user-1"
        assert second_conversation.external_user_id == "chat-2:user-2"
        assert first_request.status == "answered"
        assert first_request.answer == "answer-one"
        assert first_run.status == "completed"
        assert second_request.status == "pending"
        assert second_run.status == "waiting_input"

    answered_two = router.handle_text("chat-2", "user-2", "answer-two")

    assert "answer-two" in answered_two
    with session_scope(home) as session:
        second_request = session.execute(select(ClarificationRequest).where(ClarificationRequest.run_id == run_two)).scalar_one()
        second_run = session.get(Run, run_two)
        assert second_request.status == "answered"
        assert second_request.answer == "answer-two"
        assert second_run.status == "completed"


def test_telegram_message_from_other_chat_does_not_answer_pending_request(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    _write_ask_workflow(home)
    router = TelegramMessageRouter(home)

    started = router.handle_text("chat-1", "user-1", "/run ask_bound first")
    run_id = _run_id(started)
    other_reply = router.handle_text("chat-2", "user-2", "not your answer")

    assert "Fake LLM: not your answer" in other_reply
    with session_scope(home) as session:
        request = session.execute(select(ClarificationRequest).where(ClarificationRequest.run_id == run_id)).scalar_one()
        run = session.get(Run, run_id)
        assert request.status == "pending"
        assert request.answer is None
        assert run.status == "waiting_input"


def test_telegram_status_and_cancel_are_scoped_to_chat_and_user(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    _write_ask_workflow(home)
    router = TelegramMessageRouter(home)

    started_one = router.handle_text("chat-1", "user-1", "/run ask_bound first")
    started_two = router.handle_text("chat-2", "user-2", "/run ask_bound second")
    run_one = _run_id(started_one)
    run_two = _run_id(started_two)
    status_one = router.handle_text("chat-1", "user-1", "/status")
    cancelled_two = router.handle_text("chat-2", "user-2", "/cancel")

    assert run_one in status_one
    assert "waiting_input" in status_one
    assert run_two in cancelled_two
    assert "cancelled" in cancelled_two
    with session_scope(home) as session:
        first_run = session.get(Run, run_one)
        second_run = session.get(Run, run_two)
        first_request = session.execute(select(ClarificationRequest).where(ClarificationRequest.run_id == run_one)).scalar_one()
        second_request = session.execute(select(ClarificationRequest).where(ClarificationRequest.run_id == run_two)).scalar_one()
        assert first_run.status == "waiting_input"
        assert first_request.status == "pending"
        assert second_run.status == "cancelled"
        assert second_request.status == "cancelled"


def test_telegram_output_redacts_secret_home_path_and_raw_tool_output(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    secret = "telegram-secret-value"
    monkeypatch.setenv("AGENTEND_TELEGRAM_TEST_TOKEN", secret)
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    (home / "probe.txt").write_text("probe", encoding="utf-8")
    (home / "workflows" / "definitions" / "stat_path.yaml").write_text(
        """id: stat_path
name: Stat Path
nodes:
  - id: stat
    type: tool
    tool: fs.stat
    input:
      path: probe.txt
  - id: final
    type: final
    depends_on: [stat]
""",
        encoding="utf-8",
    )
    router = TelegramMessageRouter(home)

    secret_reply = router.handle_text("chat-1", "user-1", secret)
    agent_reply = router.handle_text("chat-1", "user-1", "/agent")
    raw_tool_reply = router.handle_text("chat-1", "user-1", "/run stat_path now")

    assert secret not in secret_reply
    assert "[REDACTED]" in secret_reply
    assert str(home) not in agent_reply
    assert "Agent profile hash:" in agent_reply
    assert str(home) not in raw_tool_reply
    assert '"path"' not in raw_tool_reply
    assert "Output omitted from Telegram" in raw_tool_reply


def _write_ask_workflow(home: Path) -> None:
    (home / "workflows" / "definitions" / "ask_bound.yaml").write_text(
        """id: ask_bound
name: Ask Bound
nodes:
  - id: ask
    type: human_input
    input:
      type: missing_input
      prompt: "Bound value?"
  - id: final
    type: final
    depends_on: [ask]
""",
        encoding="utf-8",
    )


def _run_id(output: str) -> str:
    match = re.search(r"Run:\s+([0-9a-f-]+)", output)
    assert match is not None, output
    return match.group(1)
