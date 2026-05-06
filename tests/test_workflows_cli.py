from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import Run, RunStep, ToolCall
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


def test_workflow_validate_requires_exactly_one_final_node(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    (home / "workflows" / "definitions" / "no_final.yaml").write_text(
        """id: no_final
name: No Final
nodes:
  - id: answer
    type: llm
    prompt: "{input}"
""",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["workflows", "validate", "--home", str(home)])

    assert result.exit_code == 1
    assert "exactly one final" in result.output


def test_runner_uses_final_node_output_when_final_is_not_last(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    (home / "workflows" / "definitions" / "final_not_last.yaml").write_text(
        """id: final_not_last
name: Final Not Last
nodes:
  - id: answer
    type: llm
    prompt: "Answer: {input}"
  - id: final
    type: final
    depends_on: [answer]
  - id: trailing
    type: tool
    tool: fs.write_text
    depends_on: [final]
    input:
      path: trailing.txt
      content: "should not run"
""",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["workflows", "run", "final_not_last", "--home", str(home), "--input", "hello"])

    assert result.exit_code == 0, result.output
    assert "Fake LLM: Answer: hello" in result.output
    assert not (home / "trailing.txt").exists()
    with session_scope(home) as session:
        calls = session.execute(select(ToolCall).where(ToolCall.tool_name == "fs.write_text")).scalars().all()
        assert calls == []


def test_condition_executes_only_selected_branch(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    (home / "workflows" / "definitions" / "condition_branch.yaml").write_text(
        """id: condition_branch
name: Condition Branch
nodes:
  - id: choose
    type: condition
    input:
      left: "{input}"
      equals: "yes"
    then: [write_yes]
    else: [write_no]
  - id: write_yes
    type: tool
    tool: fs.write_text
    depends_on: [choose]
    input:
      path: yes.txt
      content: "yes branch"
  - id: write_no
    type: tool
    tool: fs.write_text
    depends_on: [choose]
    input:
      path: no.txt
      content: "no branch"
  - id: final
    type: final
    depends_on: [write_yes, write_no]
""",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["workflows", "run", "condition_branch", "--home", str(home), "--input", "yes"])

    assert result.exit_code == 0, result.output
    assert "yes branch" in result.output
    assert (home / "yes.txt").exists()
    assert not (home / "no.txt").exists()
    with session_scope(home) as session:
        steps = session.execute(select(RunStep).where(RunStep.node_id == "write_no")).scalars().all()
        assert len(steps) == 1
        assert steps[0].status == "skipped"
