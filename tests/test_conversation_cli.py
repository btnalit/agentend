import re
from pathlib import Path

from typer.testing import CliRunner

from agentend.cli import app


def test_chat_message_creates_persisted_run_that_can_be_listed_and_shown(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()

    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    assert runner.invoke(app, ["db", "init", "--home", str(home)]).exit_code == 0

    chat = runner.invoke(app, ["chat", "--home", str(home), "--message", "hello agent"])

    assert chat.exit_code == 0
    match = re.search(r"Run:\s+([0-9a-f-]+)", chat.output)
    assert match, chat.output
    run_id = match.group(1)

    runs = runner.invoke(app, ["runs", "list", "--home", str(home)])

    assert runs.exit_code == 0
    assert run_id in runs.output
    assert "completed" in runs.output

    shown = runner.invoke(app, ["runs", "show", run_id, "--home", str(home)])

    assert shown.exit_code == 0
    assert "hello agent" in shown.output
    assert "Fake LLM: hello agent" in shown.output
