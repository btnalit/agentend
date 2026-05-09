import json
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.core.capabilities import capability_manifest, query_executable_capabilities
from agentend.core.goal_analyzer import analyze_goal
from agentend.db.models import Capability, GeneratedTool
from agentend.db.session import session_scope


def test_capability_manifest_marks_generated_drafts_non_executable(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    with session_scope(home) as session:
        session.add(
            GeneratedTool(
                id="generated.draft_helper",
                goal="draft helper for generated research",
                draft_path=str(home / "data" / "generated_tools" / "generated.draft_helper"),
                status="draft",
            )
        )

    refreshed = runner.invoke(app, ["capabilities", "refresh", "--home", str(home)])
    assert refreshed.exit_code == 0, refreshed.output

    with session_scope(home) as session:
        tool = session.get(Capability, "web.search")
        skill = session.get(Capability, "research.report")
        draft = session.get(Capability, "generated.draft_helper")
        assert tool is not None
        assert skill is not None
        assert draft is not None

        tool_manifest = capability_manifest(tool)
        skill_manifest = capability_manifest(skill)
        draft_manifest = capability_manifest(draft)
        executable_names = [row.name for row in query_executable_capabilities(session, "generated research")]

    assert tool_manifest["type"] == "tool"
    assert tool_manifest["executable"] is True
    assert tool_manifest["side_effect_upper_bound"] == "network_read"
    assert skill_manifest["type"] == "skill"
    assert skill_manifest["executable"] is True
    assert "required_tools" in skill_manifest
    assert draft_manifest["type"] == "generated"
    assert draft_manifest["enabled"] is False
    assert draft_manifest["executable"] is False
    assert draft_manifest["eval_status"] == "draft"
    assert "draft" in draft_manifest["policy_tags"]
    assert "generated.draft_helper" not in executable_names


def test_goal_analyzer_consumes_executable_capability_manifest_not_generated_drafts(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    with session_scope(home) as session:
        session.add(
            GeneratedTool(
                id="generated.research_helper",
                goal="research generated helper",
                draft_path=str(home / "data" / "generated_tools" / "generated.research_helper"),
                status="draft",
            )
        )
        payload = analyze_goal(home, session, "research generated helper")

    capability_ids = {item["id"] for item in payload["candidate_capabilities"]}
    assert "generated.research_helper" not in capability_ids
    assert {"research.report", "web.search"}.intersection(capability_ids)
    assert "generated.research_helper" not in payload["candidate_tools"]
    assert "generated.research_helper" not in payload["candidate_skills"]


def test_goal_analyzer_emits_allowed_capabilities_from_intent_policy(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    with session_scope(home) as session:
        payload = analyze_goal(home, session, "Read README and identify pytest command.")

    allowed = set(payload["allowed_capabilities"])
    assert "code.local_task" in allowed
    assert "git.status" in allowed
    assert "fs.read_text" in allowed
    assert "shell.run" not in allowed
