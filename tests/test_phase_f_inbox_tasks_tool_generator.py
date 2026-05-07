import json
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import Capability, ErrorRecord, GeneratedTool, Run, Schedule, TaskItem, ToolManifest
from agentend.db.session import session_scope


def test_workflow_run_accepts_stdin_json_output_and_clipboard_file_backend(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "agentend-home"
    clipboard_file = tmp_path / "clipboard.txt"
    monkeypatch.setenv("AGENTEND_CLIPBOARD_FILE", str(clipboard_file))
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    run = runner.invoke(
        app,
        ["workflows", "run", "simple_chat", "--home", str(home), "--stdin", "--output", "json"],
        input="hello from stdin",
    )
    wrote = runner.invoke(app, ["clipboard", "write", "--home", str(home), "--text", "clip text"])
    read = runner.invoke(app, ["clipboard", "read", "--home", str(home)])

    assert run.exit_code == 0
    payload = json.loads(run.output)
    assert payload["status"] == "completed"
    assert payload["run_id"]
    assert "hello from stdin" in payload["output"]
    assert wrote.exit_code == 0
    assert read.exit_code == 0
    assert read.output.strip() == "clip text"


def test_inbox_watch_creates_task_and_task_run_resume_flow(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    inbox_file = home / "data" / "inbox" / "request.txt"
    inbox_file.parent.mkdir(parents=True, exist_ok=True)
    inbox_file.write_text("summarize this file", encoding="utf-8")

    watched = runner.invoke(app, ["inbox", "watch", "--home", str(home), "--workflow", "simple_chat", "--once"])
    listed = runner.invoke(app, ["tasks", "list", "--home", str(home)])

    assert watched.exit_code == 0
    assert "Task:" in watched.output
    assert listed.exit_code == 0
    assert "pending" in listed.output
    with session_scope(home) as session:
        task = session.execute(select(TaskItem)).scalar_one()
        assert task.source == "file_inbox"
        assert task.source_path == str(inbox_file)

    run = runner.invoke(app, ["tasks", "run", task.id, "--home", str(home)])
    resume = runner.invoke(app, ["tasks", "resume", task.id, "--home", str(home), "--message", "try again"])

    assert run.exit_code == 0
    assert "completed" in run.output
    assert resume.exit_code == 0
    assert "pending" in resume.output
    with session_scope(home) as session:
        resumed = session.get(TaskItem, task.id)
        assert resumed.status == "pending"
        assert resumed.input_text == "try again"


def test_task_run_marks_waiting_input_run_as_blocked(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    (home / "workflows" / "definitions" / "ask_demo.yaml").write_text(
        """id: ask_demo
name: Ask Demo
nodes:
  - id: ask
    type: human_input
    input:
      prompt: "Need value?"
  - id: final
    type: final
    depends_on: [ask]
""",
        encoding="utf-8",
    )
    added = runner.invoke(app, ["tasks", "add", "start", "--home", str(home), "--workflow", "ask_demo"])
    assert added.exit_code == 0
    with session_scope(home) as session:
        task = session.execute(select(TaskItem)).scalar_one()

    run = runner.invoke(app, ["tasks", "run", task.id, "--home", str(home)])

    assert run.exit_code == 0
    assert "status=blocked" in run.output
    assert "Need value?" in run.output
    with session_scope(home) as session:
        refreshed = session.get(TaskItem, task.id)
        workflow_run = session.get(Run, refreshed.run_id)
        assert refreshed.status == "blocked"
        assert workflow_run.status == "waiting_input"


def test_schedule_run_now_creates_task_and_records_run(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    added = runner.invoke(
        app,
        ["schedule", "add", "--home", str(home), "--workflow", "simple_chat", "--cron", "* * * * *", "--input", "scheduled"],
    )
    assert added.exit_code == 0
    with session_scope(home) as session:
        schedule = session.execute(select(Schedule)).scalar_one()

    triggered = runner.invoke(app, ["schedule", "run-now", schedule.id, "--home", str(home)])
    listed = runner.invoke(app, ["schedule", "list", "--home", str(home)])

    assert triggered.exit_code == 0
    assert "Task:" in triggered.output
    assert "Run:" in triggered.output
    assert listed.exit_code == 0
    with session_scope(home) as session:
        refreshed = session.get(Schedule, schedule.id)
        task = session.execute(select(TaskItem).where(TaskItem.id == refreshed.last_task_id)).scalar_one()
        assert refreshed.last_run_id
        assert task.status == "completed"


def test_schedule_tick_uses_fake_clock_and_deduplicates_same_minute(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    added = runner.invoke(
        app,
        ["schedule", "add", "--home", str(home), "--workflow", "simple_chat", "--cron", "0 9 * * *", "--input", "tick"],
    )
    assert added.exit_code == 0

    first = runner.invoke(app, ["schedule", "tick", "--home", str(home), "--now", "2026-05-06T09:00:00+08:00"])
    second = runner.invoke(app, ["schedule", "tick", "--home", str(home), "--now", "2026-05-06T09:00:30+08:00"])

    assert first.exit_code == 0
    assert "Run:" in first.output
    assert second.exit_code == 0
    assert "No due schedules" in second.output
    with session_scope(home) as session:
        tasks = session.execute(select(TaskItem)).scalars().all()
        schedule = session.execute(select(Schedule)).scalar_one()
        assert len(tasks) == 1
        assert schedule.last_triggered_at is not None


def test_scheduler_run_mode_blocks_external_write_tools(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
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
    added = runner.invoke(
        app,
        ["schedule", "add", "--home", str(home), "--workflow", "scheduled_send", "--cron", "* * * * *"],
    )
    assert added.exit_code == 0
    with session_scope(home) as session:
        schedule = session.execute(select(Schedule)).scalar_one()

    triggered = runner.invoke(app, ["schedule", "run-now", schedule.id, "--home", str(home)])

    assert triggered.exit_code == 1
    assert "external_write is blocked during scheduler" in triggered.output
    with session_scope(home) as session:
        task = session.execute(select(TaskItem)).scalar_one()
        errors = session.execute(select(ErrorRecord).where(ErrorRecord.run_id == task.run_id)).scalars().all()
        assert task.status == "failed"
        assert any(error.error_code == "external_side_effect_blocked" for error in errors)


def test_tool_generator_creates_disabled_draft_without_registering_tool(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    generated = runner.invoke(
        app,
        [
            "tools",
            "test",
            "tools.generate",
            "--home",
            str(home),
            "--input",
            json.dumps({"name": "generated.parse_csv", "goal": "parse CSV summary"}),
        ],
    )

    assert generated.exit_code == 0
    shown = runner.invoke(app, ["tools", "show", "generated.parse_csv", "--home", str(home)])
    assert shown.exit_code != 0
    draft_dir = home / "data" / "generated_tools" / "generated.parse_csv"
    assert (draft_dir / "tool.yaml").exists()
    assert (draft_dir / "implementation.py").exists()
    assert (draft_dir / "test_workflow.yaml").exists()
    refreshed = runner.invoke(app, ["capabilities", "refresh", "--home", str(home)])
    assert refreshed.exit_code == 0
    with session_scope(home) as session:
        draft = session.get(GeneratedTool, "generated.parse_csv")
        manifest = session.get(ToolManifest, "generated.parse_csv")
        generator = session.get(ToolManifest, "tools.generate")
        capability = session.get(Capability, "generated.parse_csv")
        assert draft is not None
        assert draft.status == "draft"
        assert manifest is None
        assert generator is not None
        assert capability is not None
        assert capability.source == "generated"
