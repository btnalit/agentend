import json
import re
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import EvalRun
from agentend.db.session import session_scope


def _eval_id(output: str) -> str:
    match = re.search(r"Eval:\s+([0-9a-f-]+)", output)
    assert match is not None, output
    return match.group(1)


def _report(runner: CliRunner, home: Path, eval_run_id: str) -> dict:
    result = runner.invoke(app, ["eval", "report", eval_run_id, "--home", str(home)])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_agent_orchestration_eval_suites_are_listed_and_runnable(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    listed = runner.invoke(app, ["eval", "list"])
    assert listed.exit_code == 0, listed.output
    for suite in [
        "orchestration-smoke",
        "tool-first",
        "memory-consolidation",
        "skill-effectiveness",
        "long-task-worker",
        "agent-replan",
    ]:
        assert suite in listed.output

    for suite in ["orchestration-smoke", "memory-consolidation", "long-task-worker"]:
        result = runner.invoke(app, ["eval", "run", suite, "--home", str(home)])
        assert result.exit_code == 0, result.output
        payload = _report(runner, home, _eval_id(result.output))
        assert payload["suite"] == suite
        assert payload["status"] == "passed"
        assert payload["cases"]
        assert all(case["status"] == "passed" for case in payload["cases"])


def test_eval_cli_uses_suite_isolated_home_by_default(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    result = runner.invoke(app, ["eval", "run", "tool-first", "--home", str(home)])

    assert result.exit_code == 0, result.output
    payload = _report(runner, home, _eval_id(result.output))
    assert payload["suite"] == "tool-first"
    assert payload["status"] == "passed"
    assert payload["shared_home"] is False
    assert Path(payload["effective_home"]).resolve() != home.resolve()
    assert home.resolve() in Path(payload["effective_home"]).resolve().parents


def test_eval_cli_shared_home_preserves_old_behavior(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    result = runner.invoke(app, ["eval", "run", "tool-first", "--home", str(home), "--shared-home"])

    assert result.exit_code == 0, result.output
    eval_id = _eval_id(result.output)
    payload = _report(runner, home, eval_id)
    assert payload["shared_home"] is True
    assert Path(payload["effective_home"]).resolve() == home.resolve()
    with session_scope(home) as session:
        row = session.execute(select(EvalRun).where(EvalRun.id == eval_id)).scalar_one()
        assert row.suite == "tool-first"
