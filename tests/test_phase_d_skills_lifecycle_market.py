from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
import re

from agentend.db.models import Artifact, Capability, ExtensionRecord, Skill, SkillMarket, ToolCall
from agentend.db.session import session_scope


def test_builtin_skills_can_be_listed_validated_run_and_disabled(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    listed = runner.invoke(app, ["skills", "list", "--home", str(home)])
    shown = runner.invoke(app, ["skills", "show", "file.workspace_ops", "--home", str(home)])
    validated = runner.invoke(app, ["skills", "validate", "--home", str(home)])
    run = runner.invoke(
        app,
        ["skills", "run", "file.workspace_ops", "--home", str(home), "--input", '{"task":"list files"}'],
    )
    run_id = _run_id(run.output)
    refreshed_capabilities = runner.invoke(app, ["capabilities", "refresh", "--home", str(home)])
    queried_capabilities = runner.invoke(app, ["capabilities", "query", "workspace", "--home", str(home)])
    disabled = runner.invoke(app, ["skills", "disable", "file.workspace_ops", "--home", str(home)])
    disabled_run = runner.invoke(
        app,
        ["skills", "run", "file.workspace_ops", "--home", str(home), "--input", '{"task":"list files"}'],
    )
    disabled_refreshed_capabilities = runner.invoke(app, ["capabilities", "refresh", "--home", str(home)])
    disabled_queried_capabilities = runner.invoke(app, ["capabilities", "query", "workspace", "--home", str(home)])
    enabled = runner.invoke(app, ["skills", "enable", "file.workspace_ops", "--home", str(home)])
    enabled_refreshed_capabilities = runner.invoke(app, ["capabilities", "refresh", "--home", str(home)])

    assert listed.exit_code == 0
    assert "file.workspace_ops" in listed.output
    assert shown.exit_code == 0
    assert "required_tools" in shown.output
    assert validated.exit_code == 0
    assert "OK file.workspace_ops" in validated.output
    assert run.exit_code == 0
    assert "Run:" in run.output
    assert refreshed_capabilities.exit_code == 0
    assert queried_capabilities.exit_code == 0
    assert "file.workspace_ops" in queried_capabilities.output
    assert disabled.exit_code == 0
    assert disabled_run.exit_code == 1
    assert "disabled" in disabled_run.output
    assert disabled_refreshed_capabilities.exit_code == 0
    assert disabled_queried_capabilities.exit_code == 0
    assert "file.workspace_ops" not in disabled_queried_capabilities.output
    assert enabled.exit_code == 0
    assert enabled_refreshed_capabilities.exit_code == 0
    with session_scope(home) as session:
        skill = session.get(Skill, "file.workspace_ops")
        extension = session.get(ExtensionRecord, "skill:file.workspace_ops")
        assert skill is not None
        assert skill.enabled == "true"
        assert extension is not None
        assert extension.status == "enabled"
        capability = session.get(Capability, "file.workspace_ops")
        assert capability is not None
        assert capability.source == "skill"
        calls = session.execute(select(ToolCall).where(ToolCall.run_id == run_id).order_by(ToolCall.created_at)).scalars().all()
        assert {call.tool_name for call in calls} >= {"fs.list", "fs.glob"}


def test_builtin_research_report_calls_tools_and_writes_report(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    result = runner.invoke(
        app,
        ["skills", "run", "research.report", "--home", str(home), "--input", '{"topic":"AgentEnd"}'],
    )

    assert result.exit_code == 0, result.output
    run_id = _run_id(result.output)
    with session_scope(home) as session:
        calls = session.execute(select(ToolCall).where(ToolCall.run_id == run_id).order_by(ToolCall.created_at)).scalars().all()
        artifact = session.execute(select(Artifact).where(Artifact.run_id == run_id)).scalar_one()
        assert [call.tool_name for call in calls] == ["web.search", "web.fetch", "fs.write_text"]
        report = Path(artifact.path).read_text(encoding="utf-8")
        assert "Research Report" in report
        assert "Offline fixture content" in report


def test_skill_validate_rejects_required_tools_not_used_by_workflow(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    bad_skill = tmp_path / "bad.skill"
    bad_skill.mkdir()
    (bad_skill / "skill.yaml").write_text(
        """id: bad.skill
version: 0.1.0
description: Bad skill.
triggers: [bad]
workflow: workflow.yaml
required_tools: [fs.list]
required_mcp: []
input_schema:
  type: object
output_schema:
  type: object
enabled: true
source:
  type: local
""",
        encoding="utf-8",
    )
    (bad_skill / "workflow.yaml").write_text(
        """id: bad.skill
name: Bad Skill
nodes:
  - id: answer
    type: llm
    prompt: "{input}"
  - id: final
    type: final
    depends_on: [answer]
""",
        encoding="utf-8",
    )
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    result = runner.invoke(app, ["skills", "validate", "--home", str(home), "--path", str(bad_skill)])

    assert result.exit_code == 1
    assert "required_tools not used" in result.output


def test_extension_lifecycle_lists_and_shows_skill_extension(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    assert runner.invoke(app, ["skills", "list", "--home", str(home)]).exit_code == 0

    listed = runner.invoke(app, ["extensions", "list", "--home", str(home)])
    shown = runner.invoke(app, ["extensions", "show", "skill:file.workspace_ops", "--home", str(home)])
    rolled_back = runner.invoke(
        app,
        ["extensions", "rollback", "skill:file.workspace_ops", "--home", str(home), "--version", "0.1.0"],
    )

    assert listed.exit_code == 0
    assert "skill:file.workspace_ops" in listed.output
    assert shown.exit_code == 0
    assert "enabled" in shown.output
    assert rolled_back.exit_code == 0
    assert "Rolled back extension" in rolled_back.output


def test_directory_skill_market_refresh_installs_skill_metadata(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    market = tmp_path / "skills-market"
    skill_dir = market / "demo.echo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(
        """id: demo.echo
version: 0.1.0
description: Demo echo skill.
triggers: [demo]
workflow: workflow.yaml
required_tools: []
required_mcp: []
input_schema:
  type: object
output_schema:
  type: object
enabled: true
source:
  type: market
""",
        encoding="utf-8",
    )
    (skill_dir / "workflow.yaml").write_text(
        """id: demo.echo
name: Demo Echo
nodes:
  - id: answer
    type: llm
    prompt: "Echo: {input}"
  - id: final
    type: final
    depends_on: [answer]
""",
        encoding="utf-8",
    )
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    added = runner.invoke(app, ["skills", "markets", "add", "local", "--home", str(home), "--directory", str(market)])
    markets = runner.invoke(app, ["skills", "markets", "list", "--home", str(home)])
    installed = runner.invoke(app, ["skills", "install", "demo.echo", "--home", str(home)])
    refreshed = runner.invoke(app, ["skills", "refresh", "--home", str(home)])
    listed = runner.invoke(app, ["skills", "list", "--home", str(home)])

    assert added.exit_code == 0
    assert markets.exit_code == 0
    assert "local" in markets.output
    assert installed.exit_code == 0
    assert "Installed skill: demo.echo" in installed.output
    assert refreshed.exit_code == 0
    assert "demo.echo" in refreshed.output
    assert "demo.echo" in listed.output
    with session_scope(home) as session:
        row = session.get(SkillMarket, "local")
        skill = session.get(Skill, "demo.echo")
        assert row is not None
        assert skill is not None
        assert skill.source_type == "market"


def _run_id(output: str) -> str:
    match = re.search(r"Run:\s+([0-9a-f-]+)", output)
    assert match is not None, output
    return match.group(1)
