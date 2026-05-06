import json
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import Artifact, ToolManifest
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
