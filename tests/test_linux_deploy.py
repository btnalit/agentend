from pathlib import Path

from typer.testing import CliRunner

from agentend.cli import app


def test_db_backup_creates_sqlite_copy(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    backup = tmp_path / "backup.sqlite"
    runner = CliRunner()

    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    assert runner.invoke(app, ["db", "init", "--home", str(home)]).exit_code == 0

    result = runner.invoke(app, ["db", "backup", "--home", str(home), "--output", str(backup)])

    assert result.exit_code == 0
    assert backup.exists()
    assert backup.stat().st_size > 0


def test_linux_deploy_artifacts_are_present() -> None:
    root = Path(__file__).resolve().parents[1]

    service = root / "deploy" / "agentend.service"
    installer = root / "scripts" / "install-linux.sh"
    readme = root / "README.md"

    assert service.exists()
    assert "agentend telegram serve" in service.read_text(encoding="utf-8")
    assert installer.exists()
    assert "agentend init --home" in installer.read_text(encoding="utf-8")
    assert "systemd" in readme.read_text(encoding="utf-8")
