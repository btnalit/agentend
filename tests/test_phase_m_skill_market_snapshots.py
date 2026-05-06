import json
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import Capability, ExtensionRecord, ExtensionVersion, Skill
from agentend.db.session import session_scope


def _write_skill(root: Path, skill_id: str, version: str, prompt: str) -> Path:
    skill_dir = root / skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.yaml").write_text(
        f"""id: {skill_id}
version: {version}
description: Demo market skill.
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
        f"""id: skill.{skill_id}
name: Demo Market Skill
nodes:
  - id: answer
    type: llm
    prompt: "{prompt}: {{input}}"
  - id: final
    type: final
    depends_on: [answer]
""",
        encoding="utf-8",
    )
    return skill_dir


def test_git_market_refresh_writes_cache_snapshots_and_file_rollback(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    market = tmp_path / "market-repo"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    _write_skill(market, "demo.echo", "0.1.0", "V1")

    added = runner.invoke(app, ["skills", "markets", "add", "fixture", "--home", str(home), "--git", str(market)])
    first_refresh = runner.invoke(app, ["skills", "refresh", "--home", str(home)])

    assert added.exit_code == 0, added.output
    assert first_refresh.exit_code == 0, first_refresh.output
    assert "demo.echo" in first_refresh.output
    with session_scope(home) as session:
        skill = session.get(Skill, "demo.echo")
        version = session.execute(
            select(ExtensionVersion)
            .where(ExtensionVersion.extension_id == "skill:demo.echo")
            .where(ExtensionVersion.version == "0.1.0")
        ).scalar_one()
        assert skill is not None
        assert Path(skill.source_location).is_relative_to(home / "skills" / "market-cache")
        assert Path(version.source).exists()
        assert Path(version.source).is_relative_to(home / "skills" / "market-cache" / "fixture" / "snapshots")
        assert version.content_hash and len(version.content_hash) == 64

    _write_skill(market, "demo.echo", "0.2.0", "V2")
    second_refresh = runner.invoke(app, ["skills", "refresh", "--home", str(home)])
    assert second_refresh.exit_code == 0, second_refresh.output
    with session_scope(home) as session:
        skill = session.get(Skill, "demo.echo")
        current_source = Path(skill.source_location)
        assert skill.version == "0.2.0"
        assert "V2" in (current_source / "workflow.yaml").read_text(encoding="utf-8")

    rolled_back = runner.invoke(app, ["extensions", "rollback", "skill:demo.echo", "--home", str(home), "--version", "0.1.0"])

    assert rolled_back.exit_code == 0, rolled_back.output
    with session_scope(home) as session:
        skill = session.get(Skill, "demo.echo")
        current_source = Path(skill.source_location)
        assert skill.version == "0.1.0"
        assert "version: 0.1.0" in (current_source / "skill.yaml").read_text(encoding="utf-8")
        assert "V1" in (current_source / "workflow.yaml").read_text(encoding="utf-8")


def test_bad_skill_bundle_is_quarantined_without_blocking_valid_market_skills(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    market = tmp_path / "skills-market"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    _write_skill(market, "demo.good", "0.1.0", "GOOD")
    bad_dir = market / "demo.bad"
    bad_dir.mkdir(parents=True)
    (bad_dir / "skill.yaml").write_text(
        """id: demo.bad
version: 0.1.0
description: Bad market skill.
triggers: [bad]
workflow: missing.yaml
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

    added = runner.invoke(app, ["skills", "markets", "add", "local", "--home", str(home), "--directory", str(market)])
    refreshed = runner.invoke(app, ["skills", "refresh", "--home", str(home)])
    refreshed_capabilities = runner.invoke(app, ["capabilities", "refresh", "--home", str(home)])

    assert added.exit_code == 0, added.output
    assert refreshed.exit_code == 0, refreshed.output
    assert "demo.good" in refreshed.output
    assert "demo.bad" not in refreshed.output
    assert refreshed_capabilities.exit_code == 0
    with session_scope(home) as session:
        good = session.get(Skill, "demo.good")
        bad = session.get(Skill, "demo.bad")
        extension = session.get(ExtensionRecord, "skill:demo.bad")
        capability = session.get(Capability, "demo.bad")
        assert good is not None
        assert bad is None or bad.enabled == "false"
        assert capability is None
        assert extension is not None
        assert extension.status == "quarantined"
        report = Path(extension.source)
        assert report.exists()
        payload = json.loads(report.read_text(encoding="utf-8"))
        assert payload["skill_id"] == "demo.bad"
        assert "Missing workflow" in payload["error"]
