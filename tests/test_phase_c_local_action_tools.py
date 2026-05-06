import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import ActionPolicyDecision, Artifact, EventLog, ResultCache, ToolManifest
from agentend.db.session import session_scope


def test_fs_tools_cover_basic_file_operations(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    assert runner.invoke(app, ["tools", "test", "fs.mkdir", "--home", str(home), "--input", '{"path":"work"}']).exit_code == 0
    assert (
        runner.invoke(
            app,
            ["tools", "test", "fs.write_text", "--home", str(home), "--input", '{"path":"work/a.txt","content":"hello"}'],
        ).exit_code
        == 0
    )
    read = runner.invoke(app, ["tools", "test", "fs.read_text", "--home", str(home), "--input", '{"path":"work/a.txt"}'])
    copied = runner.invoke(
        app,
        ["tools", "test", "fs.copy", "--home", str(home), "--input", '{"src":"work/a.txt","dst":"work/b.txt"}'],
    )
    moved = runner.invoke(
        app,
        ["tools", "test", "fs.move", "--home", str(home), "--input", '{"src":"work/b.txt","dst":"work/c.txt"}'],
    )
    globbed = runner.invoke(app, ["tools", "test", "fs.glob", "--home", str(home), "--input", '{"pattern":"work/*.txt"}'])
    listed = runner.invoke(app, ["tools", "test", "fs.list", "--home", str(home), "--input", '{"path":"work"}'])
    stat = runner.invoke(app, ["tools", "test", "fs.stat", "--home", str(home), "--input", '{"path":"work/c.txt"}'])
    deleted = runner.invoke(app, ["tools", "test", "fs.delete", "--home", str(home), "--input", '{"path":"work/c.txt"}'])

    assert "hello" in read.output
    assert copied.exit_code == 0
    assert moved.exit_code == 0
    assert "c.txt" in globbed.output
    assert "a.txt" in listed.output
    assert "size_bytes" in stat.output
    assert deleted.exit_code == 0
    assert not (home / "work" / "c.txt").exists()
    with session_scope(home) as session:
        manifest = session.get(ToolManifest, "fs.delete")
        assert manifest is not None
        assert manifest.side_effect == "local_write"


def test_fs_tools_reject_absolute_and_parent_traversal_paths(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    absolute = runner.invoke(
        app,
        ["tools", "test", "fs.delete", "--home", str(home), "--input", json.dumps({"path": str(tmp_path), "recursive": True})],
    )
    traversal = runner.invoke(
        app,
        ["tools", "test", "fs.write_text", "--home", str(home), "--input", '{"path":"../outside.txt","content":"x"}'],
    )
    root_delete = runner.invoke(
        app,
        ["tools", "test", "fs.delete", "--home", str(home), "--input", '{"path":".","recursive":true}'],
    )

    assert absolute.exit_code == 1
    assert "relative to AgentEnd home" in absolute.output
    assert traversal.exit_code == 1
    assert "must not contain '..'" in traversal.output
    assert root_delete.exit_code == 1
    assert "must not be the AgentEnd home root" in root_delete.output


def test_browser_screenshot_rejects_absolute_artifact_path_before_network(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    result = runner.invoke(
        app,
        [
            "tools",
            "test",
            "browser.screenshot",
            "--home",
            str(home),
            "--input",
            json.dumps({"url": "http://127.0.0.1:1", "path": str(tmp_path / "x.png")}),
        ],
    )

    assert result.exit_code == 1
    assert "relative to the run artifact directory" in result.output


def test_http_request_dynamic_side_effect_controls_policy_and_cache(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    with _HTTPFixture() as fixture:
        get_one = runner.invoke(
            app,
            ["tools", "test", "http.request", "--home", str(home), "--input", json.dumps({"url": fixture.url, "method": "GET"})],
        )
        get_two = runner.invoke(
            app,
            ["tools", "test", "http.request", "--home", str(home), "--input", json.dumps({"url": fixture.url, "method": "GET"})],
        )
        post_one = runner.invoke(
            app,
            [
                "tools",
                "test",
                "http.request",
                "--home",
                str(home),
                "--input",
                json.dumps({"url": fixture.url, "method": "POST", "json": {"n": 1}}),
            ],
        )
        post_two = runner.invoke(
            app,
            [
                "tools",
                "test",
                "http.request",
                "--home",
                str(home),
                "--input",
                json.dumps({"url": fixture.url, "method": "POST", "json": {"n": 1}}),
            ],
        )

    assert get_one.exit_code == 0
    assert get_two.exit_code == 0
    assert post_one.exit_code == 0
    assert post_two.exit_code == 0
    assert fixture.gets == 1
    assert fixture.posts == 2
    with session_scope(home) as session:
        cache_rows = session.execute(select(ResultCache).where(ResultCache.tool_name == "http.request")).scalars().all()
        decisions = session.execute(
            select(ActionPolicyDecision).where(ActionPolicyDecision.tool_name == "http.request").order_by(ActionPolicyDecision.created_at)
        ).scalars().all()
        events = session.execute(select(EventLog).where(EventLog.event_type == "cache.hit")).scalars().all()
        assert len(cache_rows) == 1
        assert [decision.side_effect for decision in decisions] == [
            "network_read",
            "network_read",
            "network_write",
            "network_write",
        ]
        assert len(events) == 1


def test_shell_run_records_success_failure_and_timeout(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    ok = runner.invoke(app, ["tools", "test", "shell.run", "--home", str(home), "--input", '{"command":"python --version"}'])
    failed = runner.invoke(app, ["tools", "test", "shell.run", "--home", str(home), "--input", '{"command":"python -c \\"import sys; sys.exit(7)\\""}'])
    timed_out = runner.invoke(
        app,
        [
            "tools",
            "test",
            "shell.run",
            "--home",
            str(home),
            "--input",
            '{"command":"python -c \\"import time; time.sleep(2)\\"","timeout_seconds":1}',
        ],
    )

    assert ok.exit_code == 0
    assert "exit_code" in ok.output
    assert failed.exit_code == 0
    assert '"exit_code": 7' in failed.output
    assert timed_out.exit_code == 0
    assert "timeout" in timed_out.output


def test_python_exec_uses_local_subprocess_and_records_artifacts(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    result = runner.invoke(
        app,
        [
            "tools",
            "test",
            "python.exec",
            "--home",
            str(home),
            "--input",
            '{"code":"from pathlib import Path\\nPath(\\"out.txt\\").write_text(\\"artifact\\")\\nprint(1+1)"}',
        ],
    )

    assert result.exit_code == 0
    assert "2" in result.output
    with session_scope(home) as session:
        artifacts = session.execute(select(Artifact)).scalars().all()
        assert any(Path(artifact.path).name == "out.txt" for artifact in artifacts)


def test_git_tools_support_status_diff_and_controlled_commit(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    repo = tmp_path / "repo"
    repo.mkdir()
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    assert (
        runner.invoke(
            app,
            ["tools", "test", "shell.run", "--home", str(home), "--input", json.dumps({"command": "git init", "cwd": str(repo)})],
        ).exit_code
        == 0
    )
    (repo / "a.txt").write_text("hello\n", encoding="utf-8")

    status = runner.invoke(app, ["tools", "test", "git.status", "--home", str(home), "--input", json.dumps({"cwd": str(repo)})])
    diff = runner.invoke(app, ["tools", "test", "git.diff", "--home", str(home), "--input", json.dumps({"cwd": str(repo)})])
    commit = runner.invoke(
        app,
        [
            "tools",
            "test",
            "git.commit",
            "--home",
            str(home),
            "--input",
            json.dumps({"cwd": str(repo), "message": "test commit", "files": ["a.txt"]}),
        ],
    )

    assert status.exit_code == 0
    assert "a.txt" in status.output
    assert diff.exit_code == 0
    assert commit.exit_code == 0
    assert "test commit" in commit.output


class _HTTPFixture:
    def __init__(self) -> None:
        self.gets = 0
        self.posts = 0
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def __enter__(self) -> "_HTTPFixture":
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                owner.gets += 1
                self._write_json({"method": "GET", "count": owner.gets})

            def do_POST(self) -> None:  # noqa: N802
                owner.posts += 1
                self._write_json({"method": "POST", "count": owner.posts})

            def _write_json(self, payload: dict) -> None:
                body = json.dumps(payload).encode("utf-8")
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
        return f"http://127.0.0.1:{self.server.server_port}/"
