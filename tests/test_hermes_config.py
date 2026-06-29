from pathlib import Path
from typer.testing import CliRunner
from agentend.cli import app
from agentend.config import load_config


def test_default_config_has_hermes_home_empty(tmp_path: Path) -> None:
    home = tmp_path / "home"
    CliRunner().invoke(app, ["init", "--home", str(home)])
    config = load_config(home)
    assert hasattr(config, "hermes_home")
    assert config.hermes_home == ""


def test_hermes_home_loaded_from_config(tmp_path: Path) -> None:
    home = tmp_path / "home"
    CliRunner().invoke(app, ["init", "--home", str(home)])
    hermes_path = str(tmp_path / "hermes")
    config_path = home / "config.toml"
    # Convert path to forward slashes for TOML compatibility
    hermes_path_toml = (tmp_path / "hermes").as_posix()
    # Replace the existing home = "" line in [hermes] section instead of appending
    original = config_path.read_text(encoding="utf-8")
    updated = original.replace('[hermes]\nhome = ""', f'[hermes]\nhome = "{hermes_path_toml}"')
    config_path.write_text(updated, encoding="utf-8")
    config = load_config(home)
    assert config.hermes_home == hermes_path_toml
