import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import ContextPackItem, ContextPolicy, EventLog, MemoryItem, MemoryRetrieval, ResultCache
from agentend.db.session import session_scope


def test_result_cache_hits_and_expires_for_web_fetch(tmp_path: Path) -> None:
    requests = {"count": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests["count"] += 1
            body = f"<html><title>Cache Demo</title><body>hit {requests['count']}</body></html>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    try:
        first = runner.invoke(app, ["tools", "test", "web.fetch", "--home", str(home), "--input", json.dumps({"url": url})])
        second = runner.invoke(app, ["tools", "test", "web.fetch", "--home", str(home), "--input", json.dumps({"url": url})])
        with session_scope(home) as session:
            cache = session.execute(select(ResultCache).where(ResultCache.tool_name == "web.fetch")).scalar_one()
            cache.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        third = runner.invoke(app, ["tools", "test", "web.fetch", "--home", str(home), "--input", json.dumps({"url": url})])
    finally:
        server.shutdown()

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert third.exit_code == 0
    assert "hit 1" in first.output
    assert "hit 1" in second.output
    assert "hit 2" in third.output
    assert requests["count"] == 2
    with session_scope(home) as session:
        events = [row.event_type for row in session.execute(select(EventLog)).scalars().all()]
        cache = session.execute(select(ResultCache).where(ResultCache.tool_name == "web.fetch")).scalar_one()
        assert "cache.miss" in events
        assert "cache.hit" in events
        assert "cache.stale" in events
        assert cache.hit_count == 1


def test_memory_search_uses_fts_scope_confidence_and_updates_last_used(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    assert runner.invoke(
        app,
        ["memory", "write", "--home", str(home), "--scope", "project", "--confidence", "0.2", "--content", "linux deployment old note"],
    ).exit_code == 0
    assert runner.invoke(
        app,
        ["memory", "write", "--home", str(home), "--scope", "project", "--confidence", "0.9", "--content", "linux deployment standard command"],
    ).exit_code == 0
    assert runner.invoke(
        app,
        ["memory", "write", "--home", str(home), "--scope", "session", "--confidence", "1.0", "--content", "linux deployment session scratch"],
    ).exit_code == 0

    found = runner.invoke(app, ["memory", "search", "linux deployment", "--home", str(home), "--scope", "project"])

    assert found.exit_code == 0
    assert "standard command" in found.output
    assert "old note" in found.output
    assert "session scratch" not in found.output
    assert found.output.index("standard command") < found.output.index("old note")
    with session_scope(home) as session:
        rows = session.execute(select(MemoryItem).where(MemoryItem.scope == "project")).scalars().all()
        assert all(row.last_used_at is not None for row in rows)
        assert session.execute(select(MemoryRetrieval)).first() is not None


def test_context_policy_merge_blocks_workflow_from_relaxing_global_redaction_and_step_shrinks_pack(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    assert runner.invoke(app, ["llm", "set", "--home", str(home), "--provider", "fake", "--model", "fake-model"]).exit_code == 0
    with session_scope(home) as session:
        session.add(
            ContextPolicy(
                id="global",
                scope="global",
                target="default",
                policy_json=json.dumps({"redact_secrets": True, "max_items": 10}, sort_keys=True),
            )
        )
    assert runner.invoke(
        app,
        ["memory", "write", "--home", str(home), "--scope", "project", "--content", "policy memory should be retrievable"],
    ).exit_code == 0
    (home / "workflows" / "definitions" / "policy_demo.yaml").write_text(
        """id: policy_demo
name: Policy Demo
context:
  include_memory: true
  redact_secrets: false
  max_items: 8
nodes:
  - id: answer
    type: llm
    prompt: "{input}"
    context:
      max_items: 2
  - id: final
    type: final
    depends_on: [answer]
""",
        encoding="utf-8",
    )

    preview = runner.invoke(app, ["context", "preview", "--home", str(home), "--workflow", "policy_demo", "--input", "policy memory"])
    run = runner.invoke(app, ["workflows", "run", "policy_demo", "--home", str(home), "--input", "policy memory"])

    assert preview.exit_code == 0
    assert '"redact_secrets": true' in preview.output
    assert '"max_items": 8' in preview.output
    assert run.exit_code == 0
    with session_scope(home) as session:
        items = session.execute(select(ContextPackItem)).scalars().all()
        assert len(items) <= 2


def test_memory_write_policy_rejects_untrusted_project_scope_and_allows_task_scope(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    rejected = runner.invoke(
        app,
        ["memory", "write", "--home", str(home), "--scope", "project", "--source", "web", "--content", "untrusted fact"],
    )
    allowed = runner.invoke(
        app,
        ["memory", "write", "--home", str(home), "--scope", "task", "--source", "web", "--content", "temporary web fact"],
    )
    manual = runner.invoke(
        app,
        ["memory", "write", "--home", str(home), "--scope", "project", "--source", "manual", "--content", "manual fact"],
    )

    assert rejected.exit_code == 1
    assert "untrusted" in rejected.output.lower()
    assert allowed.exit_code == 0
    assert manual.exit_code == 0
    with session_scope(home) as session:
        rows = session.execute(select(MemoryItem)).scalars().all()
        assert {row.scope for row in rows} == {"task", "project"}
