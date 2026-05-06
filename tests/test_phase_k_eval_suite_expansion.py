import json
import re
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import Run
from agentend.db.session import session_scope


def _eval_id(output: str) -> str:
    match = re.search(r"Eval:\s+([0-9a-f-]+)", output)
    assert match is not None, output
    return match.group(1)


def _run_id(output: str) -> str:
    match = re.search(r"Run:\s+([0-9a-f-]+)", output)
    assert match is not None, output
    return match.group(1)


def _report(runner: CliRunner, home: Path, eval_run_id: str) -> dict:
    result = runner.invoke(app, ["eval", "report", eval_run_id, "--home", str(home)])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_tools_smoke_eval_covers_high_impact_tools_and_summary(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    listed = runner.invoke(app, ["eval", "list"])
    result = runner.invoke(app, ["eval", "run", "tools-smoke", "--home", str(home)])

    assert listed.exit_code == 0
    assert "tools-smoke" in listed.output
    assert "skills-smoke" in listed.output
    assert result.exit_code == 0, result.output
    payload = _report(runner, home, _eval_id(result.output))
    cases = {case["id"]: case for case in payload["cases"]}

    assert payload["suite"] == "tools-smoke"
    assert payload["status"] == "passed"
    assert "passed" in payload["human_summary"]
    assert {
        "shell.run",
        "python.exec",
        "browser.extract",
        "db.write_rows",
        "im.telegram.send_message",
        "vision.describe",
        "tools.generate",
    } <= set(cases)
    for case in cases.values():
        assert case["status"] == "passed"
        assert case["run_id"]
        assert case["tool_call_id"]
        assert case["assertions"]
        assert all(assertion["status"] == "passed" for assertion in case["assertions"])


def test_skills_smoke_eval_runs_builtin_skills(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    assert runner.invoke(app, ["llm", "set", "--home", str(home), "--provider", "fake", "--model", "fake-model"]).exit_code == 0

    result = runner.invoke(app, ["eval", "run", "skills-smoke", "--home", str(home)])

    assert result.exit_code == 0, result.output
    payload = _report(runner, home, _eval_id(result.output))
    cases = {case["id"]: case for case in payload["cases"]}

    assert payload["suite"] == "skills-smoke"
    assert payload["status"] == "passed"
    assert {"file.workspace_ops", "code.local_task", "research.report", "mcp.tool_setup"} <= set(cases)
    for case in cases.values():
        assert case["status"] == "passed"
        assert case["run_id"]
        assert case["assertions"]
        assert all(assertion["status"] == "passed" for assertion in case["assertions"])


def test_failed_tools_smoke_eval_exports_failed_run(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    disabled = runner.invoke(app, ["tools", "disable", "shell.run", "--home", str(home)])
    assert disabled.exit_code == 0, disabled.output

    result = runner.invoke(app, ["eval", "run", "tools-smoke", "--home", str(home)])

    assert result.exit_code == 0, result.output
    payload = _report(runner, home, _eval_id(result.output))
    cases = {case["id"]: case for case in payload["cases"]}
    shell_case = cases["shell.run"]
    export_path = Path(shell_case["export_path"])

    assert payload["status"] == "failed"
    assert shell_case["status"] == "failed"
    assert export_path.exists()
    assert (export_path / "run.json").exists()
    assert (export_path / "tool_contracts.json").exists()


def test_episode_skill_draft_eval_runs_after_validation(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    assert runner.invoke(app, ["llm", "set", "--home", str(home), "--provider", "fake", "--model", "fake-model"]).exit_code == 0
    wrote = runner.invoke(
        app,
        ["tools", "test", "fs.write_text", "--home", str(home), "--input", '{"path":"out.txt","content":"hello"}'],
    )
    assert wrote.exit_code == 0, wrote.output

    with session_scope(home) as session:
        run_id = session.execute(select(Run).where(Run.workflow_id == "tools.test")).scalar_one().id
    summarized = runner.invoke(app, ["episodes", "summarize", run_id, "--home", str(home)])
    assert summarized.exit_code == 0, summarized.output
    episode_id = re.search(r"Episode:\s+([0-9a-f-]+)", summarized.output).group(1)
    promoted = runner.invoke(app, ["episodes", "promote", episode_id, "--home", str(home), "--skill-id", "demo.promoted"])
    draft_dir = home / "data" / "skill_drafts" / "demo.promoted"
    validated = runner.invoke(app, ["skills", "validate", "--home", str(home), "--path", str(draft_dir)])
    evaluated = runner.invoke(app, ["eval", "run", "skills-smoke", "--home", str(home), "--skill-path", str(draft_dir)])

    assert promoted.exit_code == 0, promoted.output
    assert validated.exit_code == 0, validated.output
    assert evaluated.exit_code == 0, evaluated.output
    smoke_eval = json.loads((draft_dir / "evals" / "smoke.json").read_text(encoding="utf-8"))
    assert smoke_eval["suite"] == "skills-smoke"
    assert smoke_eval["input"]
    payload = _report(runner, home, _eval_id(evaluated.output))
    assert payload["status"] == "passed"
    assert payload["cases"][0]["id"] == "demo.promoted"
