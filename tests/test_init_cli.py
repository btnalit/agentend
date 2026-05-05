from pathlib import Path

from typer.testing import CliRunner

from agentend.cli import app


def test_help_command_is_available() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "AgentEnd Lite" in result.output
    assert "init" in result.output


def test_init_creates_local_home_without_overwriting_agent_profile(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()

    first = runner.invoke(app, ["init", "--home", str(home)])

    assert first.exit_code == 0
    assert (home / "config.toml").exists()
    assert (home / ".env.example").exists()
    assert (home / "agent.md").exists()
    assert (home / "data" / "artifacts").is_dir()
    assert (home / "data" / "logs").is_dir()
    assert (home / "workflows" / "definitions").is_dir()

    profile = home / "agent.md"
    profile.write_text("# Custom Agent\n", encoding="utf-8")

    second = runner.invoke(app, ["init", "--home", str(home)])

    assert second.exit_code == 0
    assert profile.read_text(encoding="utf-8") == "# Custom Agent\n"
