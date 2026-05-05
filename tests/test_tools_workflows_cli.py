from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import Artifact, ToolCall
from agentend.db.session import session_scope


def test_workflow_tool_node_writes_artifact_and_records_tool_call(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()

    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
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

    result = runner.invoke(app, ["workflows", "run", "write_file_demo", "--home", str(home), "--input", "hello"])

    assert result.exit_code == 0
    assert "Report: hello" in result.output
    with session_scope(home) as session:
        call = session.execute(select(ToolCall).where(ToolCall.tool_name == "file.write_text")).scalar_one()
        artifact = session.execute(select(Artifact)).scalar_one()
        assert call.status == "completed"
        assert Path(artifact.path).read_text(encoding="utf-8") == "Report: hello"


def test_workflow_call_node_runs_child_workflow(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()

    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    workflow_dir = home / "workflows" / "definitions"
    (workflow_dir / "child_writer.yaml").write_text(
        """id: child_writer
name: Child Writer
nodes:
  - id: save
    type: tool
    tool: file.write_text
    input:
      path: child.txt
      content: "Child: {input}"
  - id: final
    type: final
    depends_on: [save]
""",
        encoding="utf-8",
    )
    (workflow_dir / "parent_writer.yaml").write_text(
        """id: parent_writer
name: Parent Writer
nodes:
  - id: child
    type: workflow_call
    workflow: child_writer
  - id: final
    type: final
    depends_on: [child]
""",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["workflows", "run", "parent_writer", "--home", str(home), "--input", "delegated"])

    assert result.exit_code == 0
    assert "Child: delegated" in result.output
