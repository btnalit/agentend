from pathlib import Path

from typer.testing import CliRunner

from agentend.cli import app
from agentend.core.effectiveness import effectiveness_for, record_effectiveness_event
from agentend.db.session import init_database, session_scope


def test_effectiveness_store_aggregates_success_failure_and_blocked(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    init_database(home)

    with session_scope(home) as session:
        record_effectiveness_event(
            session,
            capability_type="tool",
            capability_id="shell.run",
            goal_type="code",
            status="success",
            duration_ms=10,
        )
        record_effectiveness_event(
            session,
            capability_type="tool",
            capability_id="shell.run",
            goal_type="code",
            status="failure",
            error_code="bad_command",
            duration_ms=20,
        )
        record_effectiveness_event(
            session,
            capability_type="tool",
            capability_id="shell.run",
            goal_type="code",
            status="blocked",
        )
        row = effectiveness_for(session, "tool", "shell.run", "code")

    assert row is not None
    assert row.attempts == 3
    assert row.successes == 1
    assert row.failures == 1
    assert row.blocked == 1
    assert row.avg_duration_ms == 10


def test_effectiveness_cli_shows_skill_and_capability_rows(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    with session_scope(home) as session:
        record_effectiveness_event(
            session,
            capability_type="skill",
            capability_id="code.local_task",
            goal_type="code",
            status="success",
        )
        record_effectiveness_event(
            session,
            capability_type="tool",
            capability_id="shell.run",
            goal_type="code",
            status="failure",
            error_code="bad_command",
        )

    skill = runner.invoke(app, ["skills", "effectiveness", "show", "code.local_task", "--home", str(home)])
    capability = runner.invoke(app, ["capabilities", "effectiveness", "show", "shell.run", "--home", str(home)])

    assert skill.exit_code == 0, skill.output
    assert "code.local_task" in skill.output
    assert "successes=1" in skill.output
    assert capability.exit_code == 0, capability.output
    assert "shell.run" in capability.output
    assert "failures=1" in capability.output
