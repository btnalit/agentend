import re
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import TaskItem
from agentend.db.session import session_scope


def test_serve_once_processes_pending_task_through_agent_run(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    added = runner.invoke(
        app,
        ["tasks", "add", "List project test command.", "--home", str(home), "--workflow", "simple_chat"],
    )
    assert added.exit_code == 0, added.output

    served = runner.invoke(app, ["serve", "--home", str(home), "--once"])

    assert served.exit_code == 0, served.output
    assert "processed=1" in served.output
    assert re.search(r"AgentRun:\s+[0-9a-f-]+", served.output)
    with session_scope(home) as session:
        task = session.execute(select(TaskItem)).scalar_one()
        assert task.status == "completed"
        assert task.agent_run_id
        assert task.heartbeat_at is not None
        assert task.progress_artifact_id
        assert task.resume_cursor_json != "{}"


def test_serve_once_reports_no_work_and_rejects_parallel_concurrency(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    empty = runner.invoke(app, ["serve", "--home", str(home), "--once"])
    invalid = runner.invoke(app, ["serve", "--home", str(home), "--once", "--max-concurrency", "2"])

    assert empty.exit_code == 0, empty.output
    assert "no work" in empty.output.lower()
    assert invalid.exit_code != 0
    assert "--max-concurrency currently only supports 1" in invalid.output
