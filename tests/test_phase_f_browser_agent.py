import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import Artifact, Run, ToolManifest
from agentend.db.session import session_scope
from agentend.tools.browser import playwright_status


def test_browser_agent_opens_extracts_and_records_screenshot_artifact(tmp_path: Path) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text(
        """<html><title>Browser Fixture</title><body><h1>Hello Browser</h1><a href="/next">Next</a></body></html>""",
        encoding="utf-8",
    )

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(site), **kwargs)

        def log_message(self, format: str, *args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/index.html"
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    try:
        opened = runner.invoke(app, ["tools", "test", "browser.open", "--home", str(home), "--input", json.dumps({"url": url})])
        extracted = runner.invoke(app, ["tools", "test", "browser.extract", "--home", str(home), "--input", json.dumps({"url": url})])
        screenshot = runner.invoke(
            app,
            ["tools", "test", "browser.screenshot", "--home", str(home), "--input", json.dumps({"url": url, "path": "shot.png"})],
        )
    finally:
        server.shutdown()

    assert opened.exit_code == 0
    assert "Browser Fixture" in opened.output
    assert extracted.exit_code == 0
    assert "Hello Browser" in extracted.output
    assert screenshot.exit_code == 0
    assert "shot.png" in screenshot.output
    with session_scope(home) as session:
        manifest = session.get(ToolManifest, "browser.screenshot")
        assert manifest is not None
        assert manifest.side_effect == "network_read"
        run = session.execute(select(Run).where(Run.workflow_id == "tools.test").order_by(Run.created_at.desc())).scalars().first()
        artifact = session.execute(select(Artifact).where(Artifact.run_id == run.id)).scalar_one()
        assert Path(artifact.path).exists()


def test_browser_action_fallback_records_dom_excerpt_and_screenshot_artifact(tmp_path: Path, monkeypatch) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text(
        """<html><title>Action Fixture</title><body><button id="go">Click Me</button></body></html>""",
        encoding="utf-8",
    )

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(site), **kwargs)

        def log_message(self, format: str, *args) -> None:
            return

    import agentend.tools.browser as browser_module

    monkeypatch.setattr(browser_module, "_try_playwright_action", lambda *args, **kwargs: (None, "forced linux fallback"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/index.html"
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    try:
        clicked = runner.invoke(app, ["tools", "test", "browser.click", "--home", str(home), "--input", json.dumps({"url": url, "selector": "#go"})])
    finally:
        server.shutdown()

    assert clicked.exit_code == 0, clicked.output
    payload = json.loads(clicked.output)
    assert payload["fallback"] is True
    assert payload["fallback_reason"] == "forced linux fallback"
    assert "Click Me" in payload["dom_excerpt"]
    assert Path(payload["screenshot_path"]).exists()
    with session_scope(home) as session:
        manifest = session.get(ToolManifest, "browser.click")
        assert manifest is not None
        assert manifest.artifact_policy == "capture_artifact"
        run = session.execute(select(Run).where(Run.workflow_id == "tools.test").order_by(Run.created_at.desc())).scalars().first()
        artifact = session.execute(select(Artifact).where(Artifact.run_id == run.id)).scalar_one()
        assert Path(artifact.path).exists()


def test_doctor_reports_browser_playwright_status(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    result = runner.invoke(app, ["doctor", "--home", str(home), "--json"])

    assert result.exit_code == 0, result.output
    checks = {item["name"]: item for item in json.loads(result.output)}
    assert "browser_playwright" in checks
    assert checks["browser_playwright"]["status"] in {"ok", "warning"}


def test_browser_screenshot_uses_playwright_when_available(tmp_path: Path) -> None:
    if not playwright_status().browser_available:
        pytest.skip("Playwright Chromium is not available in this environment")
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text(
        """<html><title>Real Browser Fixture</title><body><h1>Rendered</h1></body></html>""",
        encoding="utf-8",
    )

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(site), **kwargs)

        def log_message(self, format: str, *args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/index.html"
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    try:
        screenshot = runner.invoke(
            app,
            ["tools", "test", "browser.screenshot", "--home", str(home), "--input", json.dumps({"url": url, "path": "real.png"})],
        )
    finally:
        server.shutdown()

    assert screenshot.exit_code == 0, screenshot.output
    with session_scope(home) as session:
        run = session.execute(select(Run).where(Run.workflow_id == "tools.test").order_by(Run.created_at.desc())).scalars().first()
        artifact = session.execute(select(Artifact).where(Artifact.run_id == run.id)).scalar_one()
        metadata = json.loads(artifact.metadata_json)
        assert metadata["backend"] == "playwright"
        assert metadata["fallback"] is False
        assert metadata["size_bytes"] > 100
