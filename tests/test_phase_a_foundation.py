import json
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import ActionPolicyDecision, ErrorRecord, EvalRun, ToolManifest
from agentend.db.session import session_scope


def test_tools_cli_exposes_contract_and_records_policy_decision(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    listed = runner.invoke(app, ["tools", "list", "--home", str(home)])
    shown = runner.invoke(app, ["tools", "show", "file.write_text", "--home", str(home)])
    tested = runner.invoke(
        app,
        [
            "tools",
            "test",
            "file.write_text",
            "--home",
            str(home),
            "--input",
            '{"path":"contract.txt","content":"hello"}',
        ],
    )

    assert listed.exit_code == 0
    assert "file.write_text" in listed.output
    assert "local_write" in shown.output
    assert tested.exit_code == 0
    assert "hello" in tested.output
    with session_scope(home) as session:
        manifest = session.execute(select(ToolManifest).where(ToolManifest.name == "file.write_text")).scalar_one()
        decision = session.execute(
            select(ActionPolicyDecision).where(ActionPolicyDecision.tool_name == "file.write_text")
        ).scalar_one()
        assert manifest.side_effect == "local_write"
        assert decision.decision == "allow"


def test_secrets_cli_checks_existence_without_printing_secret_value(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    monkeypatch.setenv("AGENTEND_TEST_SECRET", "super-secret-value")
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    checked = runner.invoke(app, ["secrets", "check", "AGENTEND_TEST_SECRET", "--home", str(home)])
    listed = runner.invoke(app, ["secrets", "list", "--home", str(home)])

    assert checked.exit_code == 0
    assert "present" in checked.output
    assert "super-secret-value" not in checked.output
    assert listed.exit_code == 0
    assert "OPENAI_API_KEY" in listed.output
    assert "super-secret-value" not in listed.output


def test_tool_failure_records_structured_error_code(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    failed = runner.invoke(app, ["tools", "test", "missing.tool", "--home", str(home), "--input", "{}"])

    assert failed.exit_code == 1
    assert "tool_not_found" in failed.output
    with session_scope(home) as session:
        error = session.execute(select(ErrorRecord)).scalar_one()
        assert error.error_code == "tool_not_found"


def test_doctor_models_budget_and_eval_phase_a_cli(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    doctor = runner.invoke(app, ["doctor", "--home", str(home)])
    routes = runner.invoke(app, ["models", "routes", "list", "--home", str(home)])
    route_set = runner.invoke(
        app,
        [
            "models",
            "routes",
            "set",
            "goal_analyze",
            "--home",
            str(home),
            "--provider",
            "fake",
            "--model",
            "fake-small",
        ],
    )
    budget_set = runner.invoke(
        app,
        ["budget", "set", "--home", str(home), "--workflow", "simple_chat", "--max-llm-calls", "3"],
    )
    eval_run = runner.invoke(app, ["eval", "run", "smoke", "--home", str(home)])

    assert doctor.exit_code == 0
    assert "sqlite" in doctor.output
    assert routes.exit_code == 0
    assert "workflow_step" in routes.output
    assert route_set.exit_code == 0
    assert "goal_analyze" in route_set.output
    assert budget_set.exit_code == 0
    assert "simple_chat" in budget_set.output
    assert eval_run.exit_code == 0
    assert "passed" in eval_run.output
    with session_scope(home) as session:
        row = session.execute(select(EvalRun)).scalar_one()
        payload = json.loads(row.result_json)
        assert row.status == "passed"
        assert payload["suite"] == "smoke"
