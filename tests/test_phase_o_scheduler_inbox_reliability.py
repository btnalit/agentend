from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import Schedule, TaskItem
from agentend.db.session import session_scope


def test_schedule_validate_accepts_supported_cron_and_rejects_invalid_values() -> None:
    runner = CliRunner()

    valid = runner.invoke(app, ["schedule", "validate", "--cron", "*/5 * * * *"])
    invalid_step = runner.invoke(app, ["schedule", "validate", "--cron", "*/0 * * * *"])
    invalid_range = runner.invoke(app, ["schedule", "validate", "--cron", "61 * * * *"])

    assert valid.exit_code == 0
    assert "Valid cron" in valid.output
    assert invalid_step.exit_code == 1
    assert "Invalid cron" in invalid_step.output
    assert invalid_range.exit_code == 1
    assert "Invalid cron" in invalid_range.output


def test_schedule_auto_pauses_after_consecutive_failures(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    _write_external_write_workflow(home)

    added = runner.invoke(
        app,
        [
            "schedule",
            "add",
            "--home",
            str(home),
            "--workflow",
            "scheduled_send",
            "--cron",
            "* * * * *",
            "--max-failures",
            "2",
        ],
    )
    assert added.exit_code == 0
    with session_scope(home) as session:
        schedule = session.execute(select(Schedule)).scalar_one()

    first = runner.invoke(app, ["schedule", "run-now", schedule.id, "--home", str(home)])
    second = runner.invoke(app, ["schedule", "run-now", schedule.id, "--home", str(home)])

    assert first.exit_code == 1
    assert second.exit_code == 1
    assert "external_write is blocked during scheduler" in second.output
    with session_scope(home) as session:
        refreshed = session.get(Schedule, schedule.id)
        tasks = session.execute(select(TaskItem).order_by(TaskItem.created_at)).scalars().all()
        assert refreshed.status == "paused"
        assert refreshed.consecutive_failures == 2
        assert refreshed.paused_reason == "auto-paused after 2 consecutive failures"
        assert refreshed.last_error
        assert len(tasks) == 2
        assert {task.source for task in tasks} == {"scheduler"}
        assert {task.run_mode for task in tasks} == {"scheduler"}


def test_inbox_watch_dedupes_by_file_hash_and_respects_batch_limit(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    inbox_dir = home / "data" / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    (inbox_dir / "a.txt").write_text("same content", encoding="utf-8")
    (inbox_dir / "b.txt").write_text("same content", encoding="utf-8")
    (inbox_dir / "c.txt").write_text("unique content", encoding="utf-8")

    first = runner.invoke(
        app,
        ["inbox", "watch", "--home", str(home), "--workflow", "simple_chat", "--once", "--limit", "1"],
    )
    second = runner.invoke(
        app,
        ["inbox", "watch", "--home", str(home), "--workflow", "simple_chat", "--once", "--limit", "5"],
    )

    assert first.exit_code == 0
    assert second.exit_code == 0
    with session_scope(home) as session:
        tasks = session.execute(select(TaskItem).order_by(TaskItem.source_path)).scalars().all()
        assert len(tasks) == 2
        assert all(task.source == "file_inbox" for task in tasks)
        assert all(task.source_hash for task in tasks)
        assert all(task.batch_id for task in tasks)
        assert {Path(str(task.source_path)).name for task in tasks} == {"a.txt", "c.txt"}
        assert len({task.source_hash for task in tasks}) == 2


def test_schedule_tick_preserves_scheduler_run_mode_for_blocked_external_write(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    _write_external_write_workflow(home)

    added = runner.invoke(
        app,
        [
            "schedule",
            "add",
            "--home",
            str(home),
            "--workflow",
            "scheduled_send",
            "--cron",
            "*/5 * * * *",
        ],
    )
    assert added.exit_code == 0
    ticked = runner.invoke(app, ["schedule", "tick", "--home", str(home), "--now", "2026-05-06T09:10:00+08:00"])

    assert ticked.exit_code == 1
    assert "external_write is blocked during scheduler" in ticked.output
    with session_scope(home) as session:
        task = session.execute(select(TaskItem)).scalar_one()
        schedule = session.execute(select(Schedule)).scalar_one()
        assert task.status == "failed"
        assert task.source == "scheduler"
        assert task.run_mode == "scheduler"
        assert task.schedule_id == schedule.id
        assert schedule.last_triggered_at is not None


def _write_external_write_workflow(home: Path) -> None:
    (home / "workflows" / "definitions" / "scheduled_send.yaml").write_text(
        """id: scheduled_send
name: Scheduled Send
nodes:
  - id: send
    type: tool
    tool: im.telegram.send_message
    input:
      chat_id: "1"
      text: "hello"
      dry_run: true
  - id: final
    type: final
    depends_on: [send]
""",
        encoding="utf-8",
    )
