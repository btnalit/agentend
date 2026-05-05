from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import Run, RunStep
from agentend.db.session import session_scope


def test_workflow_validate_reports_invalid_yaml(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    workflow_path = home / "workflows" / "definitions" / "broken.yaml"
    workflow_path.write_text("id: broken\nname: Broken\n", encoding="utf-8")

    result = runner.invoke(app, ["workflows", "validate", "--home", str(home)])

    assert result.exit_code == 1
    assert "nodes" in result.output


def test_llm_to_final_workflow_runs_and_records_steps(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()

    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    assert runner.invoke(
        app,
        ["llm", "set", "--home", str(home), "--provider", "fake", "--model", "fake-model"],
    ).exit_code == 0

    workflow_path = home / "workflows" / "definitions" / "simple_chat.yaml"
    workflow_path.write_text(
        """id: simple_chat
name: Simple Chat
description: Test workflow.
nodes:
  - id: answer
    type: llm
    prompt: "Answer: {input}"
  - id: final
    type: final
    depends_on: [answer]
""",
        encoding="utf-8",
    )

    validate = runner.invoke(app, ["workflows", "validate", "--home", str(home)])
    result = runner.invoke(app, ["workflows", "run", "simple_chat", "--home", str(home), "--input", "hello"])

    assert validate.exit_code == 0
    assert result.exit_code == 0
    assert "Fake LLM: Answer: hello" in result.output

    with session_scope(home) as session:
        run = session.execute(select(Run).where(Run.workflow_id == "simple_chat")).scalar_one()
        steps = session.execute(select(RunStep).where(RunStep.run_id == run.id).order_by(RunStep.node_id)).scalars().all()
        assert run.status == "completed"
        assert {step.node_id for step in steps} == {"answer", "final"}
        assert {step.status for step in steps} == {"completed"}
