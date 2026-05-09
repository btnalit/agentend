import json
import re
from pathlib import Path

from typer.testing import CliRunner

from agentend.cli import app


def _eval_id(output: str) -> str:
    match = re.search(r"Eval:\s+([0-9a-f-]+)", output)
    assert match is not None, output
    return match.group(1)


def _report(runner: CliRunner, home: Path, eval_run_id: str) -> dict:
    result = runner.invoke(app, ["eval", "report", eval_run_id, "--home", str(home)])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_runtime_invariants_eval_suite_covers_core_audit_links(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    listed = runner.invoke(app, ["eval", "list"])
    result = runner.invoke(app, ["eval", "run", "runtime-invariants", "--home", str(home)])

    assert listed.exit_code == 0
    assert "runtime-invariants" in listed.output
    assert result.exit_code == 0, result.output
    payload = _report(runner, home, _eval_id(result.output))
    cases = {case["id"]: case for case in payload["cases"]}

    assert payload["suite"] == "runtime-invariants"
    assert payload["status"] == "passed"
    assert {
        "tool-call-policy-link",
        "llm-context-ledger-link",
        "scheduler-network-write-blocked",
        "prompt-injection-context-boundary",
        "waiting-input-clarification-link",
        "completed-agent-run-resume-stable",
    } <= set(cases)
    for case in cases.values():
        assert case["status"] == "passed"
        assert case["assertions"]
        assert all(assertion["status"] == "passed" for assertion in case["assertions"])
