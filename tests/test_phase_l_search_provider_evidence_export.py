import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import ErrorRecord, EvidenceLink, ResultCache, Run, SourceRecord
from agentend.db.session import session_scope


def _configure_brave_search(home: Path, base_url: str, api_key_env: str) -> None:
    config = home / "config.toml"
    text = config.read_text(encoding="utf-8")
    start = text.find("\n[search]\n")
    end = text.find("\n[data]\n")
    if start != -1 and end != -1 and start < end:
        text = text[:start] + text[end:]
    config.write_text(
        text
        + f"""

[search]
provider = "brave"

[search.providers.brave]
api_key_env = "{api_key_env}"
base_url = "{base_url}"
""",
        encoding="utf-8",
    )


class _BraveFixture:
    def __init__(self) -> None:
        self.requests = 0
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def __enter__(self) -> "_BraveFixture":
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                owner.requests += 1
                body = json.dumps(
                    {
                        "web": {
                            "results": [
                                {
                                    "title": "AgentEnd provider result",
                                    "url": "https://example.com/agentend",
                                    "description": "AgentEnd search provider snippet",
                                }
                            ]
                        }
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                _ = format, args

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        assert self.server is not None
        self.server.shutdown()
        if self.thread is not None:
            self.thread.join(timeout=2)
        self.server.server_close()

    @property
    def url(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.server_port}/res/v1/web/search"


def test_brave_search_provider_records_sources_cache_and_export(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "agentend-home"
    export_dir = tmp_path / "exports"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    with _BraveFixture() as fixture:
        _configure_brave_search(home, fixture.url, "AGENTEND_BRAVE_SEARCH_KEY")
        monkeypatch.setenv("AGENTEND_BRAVE_SEARCH_KEY", "secret-value-that-must-not-leak")

        searched = runner.invoke(app, ["tools", "test", "web.search", "--home", str(home), "--input", '{"query":"agentend","limit":1}'])

    assert searched.exit_code == 0, searched.output
    assert "AgentEnd provider result" in searched.output
    assert "secret-value-that-must-not-leak" not in searched.output
    with session_scope(home) as session:
        source = session.execute(select(SourceRecord)).scalar_one()
        cache = session.execute(select(ResultCache).where(ResultCache.tool_name == "web.search")).scalar_one()
        link = session.execute(select(EvidenceLink).where(EvidenceLink.source_id == source.id)).scalar_one()
        run = session.get(Run, source.used_by_run_id)
        assert source.source_type == "web_search"
        assert source.title == "AgentEnd provider result"
        assert source.content_hash
        assert source.quote == "AgentEnd search provider snippet"
        assert cache.status == "active"
        assert link.run_id == source.used_by_run_id
        assert run is not None
        run_id = run.id

    listed = runner.invoke(app, ["sources", "list", "--home", str(home), "--run", run_id])
    shown = runner.invoke(app, ["sources", "show", source.id, "--home", str(home)])
    exported = runner.invoke(app, ["runs", "export", run_id, "--home", str(home), "--output", str(export_dir)])

    assert listed.exit_code == 0
    assert "AgentEnd provider result" in listed.output
    assert shown.exit_code == 0
    assert "https://example.com/agentend" in shown.output
    assert exported.exit_code == 0, exported.output
    evidence = json.loads((export_dir / run_id / "evidence_manifest.json").read_text(encoding="utf-8"))
    manifest = json.loads((export_dir / run_id / "run.json").read_text(encoding="utf-8"))
    exported_source = evidence["sources"][0]
    assert exported_source["query"] == "agentend"
    assert exported_source["content_hash"] == source.content_hash
    assert exported_source["fetched_at"]
    assert exported_source["used_by_run_id"] == run_id
    assert exported_source["tool_call_id"]
    assert "secret-value-that-must-not-leak" not in json.dumps(evidence)
    assert manifest["evidence_manifest"]["sources"][0]["id"] == source.id


def test_web_search_cache_hit_recreates_evidence_for_current_run(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    with _BraveFixture() as fixture:
        _configure_brave_search(home, fixture.url, "AGENTEND_BRAVE_SEARCH_KEY")
        monkeypatch.setenv("AGENTEND_BRAVE_SEARCH_KEY", "cache-secret")
        first = runner.invoke(app, ["tools", "test", "web.search", "--home", str(home), "--input", '{"query":"agentend","limit":1}'])
        second = runner.invoke(app, ["tools", "test", "web.search", "--home", str(home), "--input", '{"query":"agentend","limit":1}'])
        assert fixture.requests == 1

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    with session_scope(home) as session:
        sources = session.execute(select(SourceRecord).order_by(SourceRecord.fetched_at)).scalars().all()
        assert len(sources) == 2
        assert sources[0].used_by_run_id != sources[1].used_by_run_id
        assert sources[0].url == sources[1].url


def test_missing_search_secret_records_structured_error_without_leaking_value(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    _configure_brave_search(home, "http://127.0.0.1:1/res/v1/web/search", "AGENTEND_MISSING_SEARCH_KEY")
    monkeypatch.delenv("AGENTEND_MISSING_SEARCH_KEY", raising=False)

    searched = runner.invoke(app, ["tools", "test", "web.search", "--home", str(home), "--input", '{"query":"agentend","limit":1}'])

    assert searched.exit_code == 1
    assert "missing_config" in searched.output
    assert "AGENTEND_MISSING_SEARCH_KEY" in searched.output
    with session_scope(home) as session:
        error = session.execute(select(ErrorRecord).where(ErrorRecord.source == "tool")).scalar_one()
        run = session.execute(select(Run).where(Run.workflow_id == "tools.test")).scalar_one()
        assert error.error_code == "missing_config"
        assert run.status == "failed"
        assert run.error == "Search provider secret is not set: AGENTEND_MISSING_SEARCH_KEY"
