from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import Checkpoint, ContextLedger, ContextSummary, MemoryItem
from agentend.db.session import session_scope


def test_llm_workflow_records_context_ledger_and_checkpoint(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    assert runner.invoke(app, ["llm", "set", "--home", str(home), "--provider", "fake", "--model", "fake-model"]).exit_code == 0

    result = runner.invoke(app, ["workflows", "run", "simple_chat", "--home", str(home), "--input", "hello context"])

    assert result.exit_code == 0
    with session_scope(home) as session:
        ledger = session.execute(select(ContextLedger)).scalar_one()
        checkpoint = session.execute(select(Checkpoint).where(Checkpoint.run_id == ledger.run_id)).scalars().first()
        assert ledger.workflow_step_id is not None
        assert ledger.model_stage == "workflow_step"
        assert checkpoint is not None
        assert checkpoint.step_id is not None

    shown = runner.invoke(app, ["context", "ledger", "show", ledger.id, "--home", str(home)])
    assert shown.exit_code == 0
    assert "hello context" in shown.output


def test_context_preview_and_tool_compaction(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    preview = runner.invoke(app, ["context", "preview", "--home", str(home), "--workflow", "simple_chat", "--input", "preview me"])
    assert preview.exit_code == 0
    assert "preview me" in preview.output

    workflow_path = home / "workflows" / "definitions" / "write_file_demo.yaml"
    workflow_path.write_text(
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
    run = runner.invoke(app, ["workflows", "run", "write_file_demo", "--home", str(home), "--input", "compact"])

    assert run.exit_code == 0
    with session_scope(home) as session:
        summary = session.execute(select(ContextSummary)).scalar_one()
        assert summary.source_type == "tool_result"
        assert "file.write_text" in summary.summary


def test_memory_cli_write_search_and_forget(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    written = runner.invoke(
        app,
        ["memory", "write", "--home", str(home), "--scope", "project", "--content", "部署命令是 agentend doctor"],
    )
    found = runner.invoke(app, ["memory", "search", "部署命令", "--home", str(home), "--scope", "project"])

    assert written.exit_code == 0
    assert found.exit_code == 0
    assert "agentend doctor" in found.output
    with session_scope(home) as session:
        memory = session.execute(select(MemoryItem)).scalar_one()

    forgotten = runner.invoke(app, ["memory", "forget", memory.id, "--home", str(home)])
    missing = runner.invoke(app, ["memory", "search", "部署命令", "--home", str(home), "--scope", "project"])

    assert forgotten.exit_code == 0
    assert "forgotten" in forgotten.output
    assert "agentend doctor" not in missing.output


def test_memory_write_redacts_token_like_content(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    result = runner.invoke(
        app,
        [
            "memory",
            "write",
            "--home",
            str(home),
            "--scope",
            "project",
            "--content",
            "token sk-testsecret1234567890",
        ],
    )

    assert result.exit_code == 0
    with session_scope(home) as session:
        memory = session.execute(select(MemoryItem)).scalar_one()
        assert "sk-testsecret" not in memory.content
        assert "[REDACTED]" in memory.content
