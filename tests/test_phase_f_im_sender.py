import json
from pathlib import Path

from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import ToolManifest
from agentend.db.session import session_scope


def test_telegram_im_sender_dry_run_message_and_file(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    file_path = home / "report.txt"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    file_path.write_text("hello file", encoding="utf-8")

    message = runner.invoke(
        app,
        [
            "tools",
            "test",
            "im.telegram.send_message",
            "--home",
            str(home),
            "--input",
            json.dumps({"chat_id": "123", "text": "hello", "dry_run": True}),
        ],
    )
    sent_file = runner.invoke(
        app,
        [
            "tools",
            "test",
            "im.telegram.send_file",
            "--home",
            str(home),
            "--input",
            json.dumps({"chat_id": "123", "path": str(file_path), "dry_run": True}),
        ],
    )

    assert message.exit_code == 0
    assert '"dry_run": true' in message.output
    assert sent_file.exit_code == 0
    assert "report.txt" in sent_file.output
    with session_scope(home) as session:
        manifest = session.get(ToolManifest, "im.telegram.send_message")
        assert manifest is not None
        assert manifest.side_effect == "external_write"
