from pathlib import Path

from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import AgentRun
from agentend.db.session import session_scope
from agentend.telegram_bot import TelegramMessageRouter


def test_telegram_router_handles_start_plain_message_and_run_command(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    router = TelegramMessageRouter(home)

    assert "AgentEnd" in router.handle_text(chat_id="chat-1", user_id="user-1", text="/start")
    assert "Fake LLM: hello" in router.handle_text(chat_id="chat-1", user_id="user-1", text="hello")
    assert "simple_chat" in router.handle_text(chat_id="chat-1", user_id="user-1", text="/workflows")
    assert "Fake LLM: from telegram" in router.handle_text(
        chat_id="chat-1",
        user_id="user-1",
        text="/run simple_chat from telegram",
    )


def test_telegram_availability_check_stays_chat_without_shell_probe(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    router = TelegramMessageRouter(home)

    reply = router.handle_text(chat_id="chat-1", user_id="user-1", text="测试是否可用")

    assert "Run:" in reply
    assert "Fake LLM:" in reply
    assert "pytest" not in reply
    assert "exit_code" not in reply
    with session_scope(home) as session:
        assert session.query(AgentRun).count() == 0


def test_telegram_serve_reports_missing_token(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    result = runner.invoke(app, ["telegram", "serve", "--home", str(home)])

    assert result.exit_code == 1
    assert "TELEGRAM_BOT_TOKEN" in result.output
