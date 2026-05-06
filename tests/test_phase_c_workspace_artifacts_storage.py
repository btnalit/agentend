import json
import re
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import Artifact, RunExport, WorkspaceIndex
from agentend.db.session import session_scope


def test_workspace_index_and_summary_capture_project_context(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    (home / "README.md").write_text("# Demo\n\nRun tests with python -m pytest -q\n", encoding="utf-8")
    (home / "AGENTS.md").write_text("# Rules\n\nUse Chinese.\n", encoding="utf-8")

    indexed = runner.invoke(app, ["workspace", "index", "--home", str(home)])
    summary = runner.invoke(app, ["workspace", "summary", "--home", str(home)])

    assert indexed.exit_code == 0
    assert summary.exit_code == 0
    assert "README.md" in summary.output
    assert "AGENTS.md" in summary.output
    assert "python -m pytest -q" in summary.output
    with session_scope(home) as session:
        rows = session.execute(select(WorkspaceIndex)).scalars().all()
        assert {row.source_path for row in rows} >= {"README.md", "AGENTS.md"}


def test_artifacts_list_show_and_run_export(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    export_dir = tmp_path / "exports"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    workflow = home / "workflows" / "definitions" / "write_file_demo.yaml"
    workflow.write_text(
        """id: write_file_demo
name: Write File Demo
nodes:
  - id: save
    type: tool
    tool: file.write_text
    input:
      path: report.txt
      content: "Report: {input}"
  - id: final
    type: final
    depends_on: [save]
""",
        encoding="utf-8",
    )
    run = runner.invoke(app, ["workflows", "run", "write_file_demo", "--home", str(home), "--input", "export"])
    run_id = re.search(r"Run:\s+([0-9a-f-]+)", run.output).group(1)
    with session_scope(home) as session:
        artifact = session.execute(select(Artifact)).scalar_one()

    listed = runner.invoke(app, ["artifacts", "list", "--home", str(home), "--run", run_id])
    shown = runner.invoke(app, ["artifacts", "show", artifact.id, "--home", str(home)])
    exported = runner.invoke(app, ["runs", "export", run_id, "--home", str(home), "--output", str(export_dir)])

    assert listed.exit_code == 0
    assert "report.txt" in listed.output
    assert shown.exit_code == 0
    assert "Report: export" in shown.output
    assert exported.exit_code == 0
    manifest = json.loads((export_dir / run_id / "run.json").read_text(encoding="utf-8"))
    assert manifest["run"]["id"] == run_id
    with session_scope(home) as session:
        row = session.execute(select(RunExport)).scalar_one()
        assert row.run_id == run_id


def test_storage_usage_cleanup_dry_run_and_backup(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    backup_dir = tmp_path / "backups"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    assert runner.invoke(app, ["db", "init", "--home", str(home)]).exit_code == 0

    usage = runner.invoke(app, ["storage", "usage", "--home", str(home)])
    dry_run = runner.invoke(app, ["storage", "cleanup", "--home", str(home), "--older-than", "0d", "--dry-run"])
    backup = runner.invoke(app, ["storage", "backup", "--home", str(home), "--output", str(backup_dir)])

    assert usage.exit_code == 0
    assert "agentend.sqlite" in usage.output
    assert dry_run.exit_code == 0
    assert "dry-run" in dry_run.output
    assert backup.exit_code == 0
    assert (backup_dir / "agentend.sqlite").exists()
