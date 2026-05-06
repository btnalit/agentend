import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import Capability, SourceRecord
from agentend.db.session import session_scope


def test_web_fetch_records_source_and_sources_cli(tmp_path: Path) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<html><title>Demo</title><body>Hello evidence</body></html>", encoding="utf-8")

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
        fetched = runner.invoke(app, ["tools", "test", "web.fetch", "--home", str(home), "--input", json.dumps({"url": url})])
    finally:
        server.shutdown()

    assert fetched.exit_code == 0
    assert "Hello evidence" in fetched.output
    with session_scope(home) as session:
        source = session.execute(select(SourceRecord)).scalar_one()
    listed = runner.invoke(app, ["sources", "list", "--home", str(home), "--run", source.used_by_run_id])
    shown = runner.invoke(app, ["sources", "show", source.id, "--home", str(home)])

    assert listed.exit_code == 0
    assert "Demo" in listed.output
    assert shown.exit_code == 0
    assert url in shown.output


def test_web_search_fake_provider_and_capability_map(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    searched = runner.invoke(
        app,
        ["tools", "test", "web.search", "--home", str(home), "--input", '{"query":"AgentEnd","provider":"fake","limit":2}'],
    )
    refreshed = runner.invoke(app, ["capabilities", "refresh", "--home", str(home)])
    queried = runner.invoke(app, ["capabilities", "query", "fetch URL", "--home", str(home)])
    discovered = runner.invoke(app, ["tools", "test", "tools.discover", "--home", str(home), "--input", '{"query":"fetch"}'])

    assert searched.exit_code == 0
    assert "AgentEnd" in searched.output
    assert refreshed.exit_code == 0
    assert queried.exit_code == 0
    assert "web.fetch" in queried.output
    assert discovered.exit_code == 0
    assert "web.fetch" in discovered.output
    with session_scope(home) as session:
        capability = session.execute(select(Capability).where(Capability.name == "web.fetch")).scalar_one()
        assert capability.source == "tool"
