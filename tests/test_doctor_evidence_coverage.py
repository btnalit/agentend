import json
import re
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import Artifact, EvidenceLink, MCPServer, SkillMarket, SourceRecord
from agentend.db.session import session_scope


def test_doctor_reports_extended_runtime_dependency_checks(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    with session_scope(home) as session:
        session.add(
            MCPServer(
                id=str(uuid4()),
                name="broken",
                transport="stdio",
                command="mock:missing",
                status="unhealthy",
                last_error="refresh failed",
            )
        )
        session.add(SkillMarket(name="missing", backend="directory", location=str(home / "missing-market")))

    result = runner.invoke(app, ["doctor", "--home", str(home), "--json"])

    assert result.exit_code == 0, result.output
    checks = {item["name"]: item for item in json.loads(result.output)}
    assert checks["artifacts"]["status"] == "ok"
    assert checks["sandboxes"]["status"] == "ok"
    assert checks["telegram"]["status"] == "warning"
    assert "TELEGRAM_BOT_TOKEN" in checks["telegram"]["message"]
    assert checks["mcp_servers"]["status"] == "warning"
    assert "broken" in checks["mcp_servers"]["message"]
    assert checks["search"]["status"] == "ok"
    assert checks["skill_markets"]["status"] == "warning"
    assert "missing" in checks["skill_markets"]["message"]


def test_file_and_browser_sources_are_exported_with_artifact_links(tmp_path: Path) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text(
        """<html><title>Evidence Fixture</title><body><h1>Evidence Body</h1><a href="/next">Next</a></body></html>""",
        encoding="utf-8",
    )

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(site), **kwargs)

        def log_message(self, format: str, *args) -> None:
            _ = format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/index.html"
    home = tmp_path / "agentend-home"
    export_dir = tmp_path / "exports"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    (home / "notes.txt").write_text("local source evidence", encoding="utf-8")
    (home / "workflows" / "definitions" / "evidence_coverage.yaml").write_text(
        """id: evidence_coverage
name: Evidence Coverage
nodes:
  - id: fs_read
    type: tool
    tool: fs.read_text
    input:
      path: notes.txt
  - id: file_read
    type: tool
    tool: file.read_text
    input:
      path: notes.txt
  - id: extract
    type: tool
    tool: browser.extract
    input:
      url: "{input}"
  - id: screenshot
    type: tool
    tool: browser.screenshot
    input:
      url: "{input}"
      path: evidence.png
  - id: final
    type: final
    depends_on: [fs_read, file_read, extract, screenshot]
""",
        encoding="utf-8",
    )

    try:
        run = runner.invoke(app, ["workflows", "run", "evidence_coverage", "--home", str(home), "--input", url])
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    assert run.exit_code == 0, run.output
    run_id = re.search(r"Run:\s+([0-9a-f-]+)", run.output).group(1)
    with session_scope(home) as session:
        sources = session.execute(select(SourceRecord).where(SourceRecord.used_by_run_id == run_id)).scalars().all()
        source_types = [source.source_type for source in sources]
        assert source_types.count("file_read") == 2
        assert "browser_extract" in source_types
        assert "browser_screenshot" in source_types
        screenshot_source = next(source for source in sources if source.source_type == "browser_screenshot")
        assert screenshot_source.title == "Evidence Fixture"
        assert screenshot_source.content_hash
        link = session.execute(select(EvidenceLink).where(EvidenceLink.source_id == screenshot_source.id)).scalar_one()
        artifact = session.get(Artifact, link.artifact_id)
        assert artifact is not None
        assert Path(artifact.path).exists()

    listed = runner.invoke(app, ["sources", "list", "--home", str(home), "--run", run_id])
    exported = runner.invoke(app, ["runs", "export", run_id, "--home", str(home), "--output", str(export_dir)])

    assert listed.exit_code == 0, listed.output
    assert "notes.txt" in listed.output
    assert "Evidence Fixture" in listed.output
    assert exported.exit_code == 0, exported.output
    evidence = json.loads((export_dir / run_id / "evidence_manifest.json").read_text(encoding="utf-8"))
    exported_types = [source["source_type"] for source in evidence["sources"]]
    assert exported_types.count("file_read") == 2
    assert "browser_extract" in exported_types
    assert "browser_screenshot" in exported_types
    assert any(link["artifact_id"] for link in evidence["links"] if link["relation"] == "captured_artifact")
