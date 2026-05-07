import json
from pathlib import Path

from typer.testing import CliRunner

from agentend.cli import app
from agentend.core.agent_selector import select_next_action
from agentend.core.effectiveness import record_effectiveness_event
from agentend.db.session import init_database, session_scope


def test_tool_first_selector_prefers_matching_skill_or_tool(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    init_database(home)

    with session_scope(home) as session:
        selected = select_next_action(home, session, "Read README and list test commands.", {}, [])

    assert selected.type in {"skill_run", "tool_call"}
    assert selected.no_tool_reason in (None, "")
    assert selected.name in {"file.workspace_ops", "code.local_task", "fs.read_text", "fs.list", "shell.run"}


def test_effectiveness_changes_selector_ranking_between_matching_skills(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    with session_scope(home) as session:
        for _ in range(3):
            record_effectiveness_event(
                session,
                capability_type="skill",
                capability_id="file.workspace_ops",
                goal_type="workspace",
                status="failure",
                error_code="regression",
            )
        record_effectiveness_event(
            session,
            capability_type="skill",
            capability_id="code.local_task",
            goal_type="workspace",
            status="success",
        )
        selected = select_next_action(
            home,
            session,
            "Read project files and identify pytest command.",
            {"candidate_skills": ["file.workspace_ops", "code.local_task"]},
            [],
        )

    assert selected.type == "skill_run"
    assert selected.name == "code.local_task"
    assert json.loads(selected.to_json())["score"] > 0
