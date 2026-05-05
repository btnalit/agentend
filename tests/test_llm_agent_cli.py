from hashlib import sha256
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import Run
from agentend.db.session import session_scope


def test_llm_config_can_be_set_and_reported(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()

    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    set_result = runner.invoke(
        app,
        ["llm", "set", "--home", str(home), "--provider", "openai", "--model", "gpt-4.1-mini"],
    )
    current = runner.invoke(app, ["llm", "current", "--home", str(home)])

    assert set_result.exit_code == 0
    assert current.exit_code == 0
    assert "openai" in current.output
    assert "gpt-4.1-mini" in current.output


def test_llm_test_reports_missing_api_key(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    result = runner.invoke(app, ["llm", "test", "--home", str(home)])

    assert result.exit_code == 1
    assert "OPENAI_API_KEY" in result.output


def test_chat_run_records_agent_profile_hash_and_llm_config(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()

    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    assert runner.invoke(
        app,
        ["llm", "set", "--home", str(home), "--provider", "openai", "--model", "gpt-4.1-mini"],
    ).exit_code == 0

    profile = home / "agent.md"
    profile.write_text("# Custom Agent\n\nReply briefly.\n", encoding="utf-8")
    expected_hash = sha256(profile.read_bytes()).hexdigest()

    chat = runner.invoke(app, ["chat", "--home", str(home), "--message", "remember profile"])

    assert chat.exit_code == 0
    with session_scope(home) as session:
        run = session.execute(select(Run)).scalar_one()
        assert run.agent_profile_path == str(profile)
        assert run.agent_profile_hash == expected_hash
        assert run.llm_provider == "openai"
        assert run.llm_model == "gpt-4.1-mini"
