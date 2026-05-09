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
    worker_service = root / "deploy" / "agentend-worker.service"
    openwrt_worker = root / "deploy" / "openwrt" / "agentend-worker.init"
    openwrt_telegram = root / "deploy" / "openwrt" / "agentend-telegram.init"
    installer = root / "scripts" / "install-linux.sh"
    readme = root / "README.md"
    env_example = root / ".env.example"

    assert service.exists()
    assert "agentend telegram serve" in service.read_text(encoding="utf-8")
    assert worker_service.exists()
    assert "agentend serve" in worker_service.read_text(encoding="utf-8")
    assert openwrt_worker.exists()
    assert "serve --home" in openwrt_worker.read_text(encoding="utf-8")
    assert openwrt_telegram.exists()
    assert "telegram serve --home" in openwrt_telegram.read_text(encoding="utf-8")
    assert installer.exists()
    installer_text = installer.read_text(encoding="utf-8")
    assert "agentend init --home" in installer_text
    assert "python -m pip install -e \"$INSTALL_SPEC\"" in installer_text
    assert "python3 -m venv .venv" in installer_text
    assert "run_interactive_setup" in installer_text
    assert "AGENTEND_SETUP" in installer_text
    assert "--base-url" in installer_text
    assert "DEEPSEEK_API_KEY" in installer_text
    assert "TELEGRAM_BOT_TOKEN" in installer_text
    assert "BRAVE_SEARCH_API_KEY" in installer_text
    assert "install_background_services" in installer_text
    assert "agentend-worker" in installer_text
    assert "agentend-telegram" in installer_text
    assert "systemctl enable --now" in installer_text
    assert "/etc/init.d/agentend-worker" in installer_text
    assert "AGENTEND_START_SERVICES" in installer_text
    assert ".[dev]" not in installer_text
    assert env_example.exists()
    assert "TELEGRAM_BOT_TOKEN=" in env_example.read_text(encoding="utf-8")
    readme_text = readme.read_text(encoding="utf-8")
    assert "systemd" in readme_text
    assert "python -m pip install -e ." in readme_text


def test_runtime_install_does_not_require_playwright() -> None:
    root = Path(__file__).resolve().parents[1]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    dependencies = pyproject.split("[project.optional-dependencies]", maxsplit=1)[0]

    assert "playwright" not in dependencies
    assert "browser = [" in pyproject
