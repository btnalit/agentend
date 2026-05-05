import re
from pathlib import Path

from typer.testing import CliRunner

from agentend.cli import app


def test_waiting_input_run_can_be_resumed_and_cancelled(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    (home / "workflows" / "definitions" / "ask.yaml").write_text(
        """id: ask
name: Ask
nodes:
  - id: ask_user
    type: human_input
    input:
      prompt: "Need value"
  - id: final
    type: final
    depends_on: [ask_user]
""",
        encoding="utf-8",
    )

    first = runner.invoke(app, ["workflows", "run", "ask", "--home", str(home), "--input", "start"])
    run_id = re.search(r"Run:\s+([0-9a-f-]+)", first.output).group(1)
    shown = runner.invoke(app, ["runs", "show", run_id, "--home", str(home)])
    resumed = runner.invoke(app, ["runs", "resume", run_id, "--home", str(home), "--message", "provided"])

    assert first.exit_code == 0
    assert "Need value" in first.output
    assert "waiting_input" in shown.output
    assert resumed.exit_code == 0
    assert "completed" in resumed.output
    assert "provided" in runner.invoke(app, ["runs", "show", run_id, "--home", str(home)]).output

    second = runner.invoke(app, ["workflows", "run", "ask", "--home", str(home), "--input", "start"])
    second_run_id = re.search(r"Run:\s+([0-9a-f-]+)", second.output).group(1)
    cancelled = runner.invoke(app, ["runs", "cancel", second_run_id, "--home", str(home)])

    assert cancelled.exit_code == 0
    assert "cancelled" in runner.invoke(app, ["runs", "show", second_run_id, "--home", str(home)]).output


def test_failed_workflow_records_error_and_logs_tail(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    (home / "workflows" / "definitions" / "bad.yaml").write_text(
        """id: bad
name: Bad
nodes:
  - id: bad_tool
    type: tool
    tool: missing.tool
  - id: final
    type: final
    depends_on: [bad_tool]
""",
        encoding="utf-8",
    )

    failed = runner.invoke(app, ["workflows", "run", "bad", "--home", str(home), "--input", "x"])
    runs = runner.invoke(app, ["runs", "list", "--home", str(home)])
    logs = runner.invoke(app, ["logs", "tail", "--home", str(home)])

    assert failed.exit_code == 1
    assert "Unknown tool" in failed.output
    assert "failed" in runs.output
    assert "run.failed" in logs.output
