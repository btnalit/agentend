import json
import re
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import EvalRun, ToolContractSnapshot
from agentend.db.session import session_scope


def _eval_id(output: str) -> str:
    match = re.search(r"Eval:\s+([0-9a-f-]+)", output)
    assert match is not None, output
    return match.group(1)


def _run_id(output: str) -> str:
    match = re.search(r"Run:\s+([0-9a-f-]+)", output)
    assert match is not None, output
    return match.group(1)


def test_context_smoke_eval_reports_cases_assertions_and_linked_runs(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    assert runner.invoke(app, ["llm", "set", "--home", str(home), "--provider", "fake", "--model", "fake-model"]).exit_code == 0

    listed = runner.invoke(app, ["eval", "list"])
    result = runner.invoke(app, ["eval", "run", "context-smoke", "--home", str(home)])

    assert listed.exit_code == 0
    assert "context-smoke" in listed.output
    assert result.exit_code == 0
    eval_run_id = _eval_id(result.output)

    report = runner.invoke(app, ["eval", "report", eval_run_id, "--home", str(home)])
    assert report.exit_code == 0
    payload = json.loads(report.output)
    cases = {case["id"]: case for case in payload["cases"]}

    assert payload["suite"] == "context-smoke"
    assert payload["status"] == "passed"
    assert {"lost-context", "tool-output-bloat", "memory-retrieval", "policy-merge"} <= set(cases)
    for case in cases.values():
        assert case["status"] == "passed"
        assert case["run_id"]
        assert case["assertions"]
        assert all(assertion["status"] == "passed" for assertion in case["assertions"])

    assert cases["memory-retrieval"]["context_ledger_id"]
    with session_scope(home) as session:
        row = session.get(EvalRun, eval_run_id)
        assert row is not None
        assert row.status == "passed"


def test_smoke_eval_embeds_context_smoke_results(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    assert runner.invoke(app, ["llm", "set", "--home", str(home), "--provider", "fake", "--model", "fake-model"]).exit_code == 0

    result = runner.invoke(app, ["eval", "run", "smoke", "--home", str(home)])

    assert result.exit_code == 0
    report = runner.invoke(app, ["eval", "report", _eval_id(result.output), "--home", str(home)])
    payload = json.loads(report.output)
    nested = payload["nested_suites"]["context-smoke"]

    assert payload["suite"] == "smoke"
    assert payload["status"] == "passed"
    assert payload["checks"]["context_smoke_passed"] is True
    assert nested["status"] == "passed"
    assert any(case["id"] == "memory-retrieval" for case in nested["cases"])


def test_run_export_contains_tool_contract_snapshots(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    export_dir = tmp_path / "exports"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    workflow = home / "workflows" / "definitions" / "contract_export_demo.yaml"
    workflow.write_text(
        """id: contract_export_demo
name: Contract Export Demo
nodes:
  - id: save
    type: tool
    tool: file.write_text
    input:
      path: contract.txt
      content: "contract snapshot"
  - id: final
    type: final
    depends_on: [save]
""",
        encoding="utf-8",
    )

    run = runner.invoke(app, ["workflows", "run", "contract_export_demo", "--home", str(home), "--input", "snapshot"])
    assert run.exit_code == 0
    run_id = _run_id(run.output)

    with session_scope(home) as session:
        snapshots = session.execute(select(ToolContractSnapshot).where(ToolContractSnapshot.run_id == run_id)).scalars().all()
        assert any(snapshot.tool_name == "file.write_text" for snapshot in snapshots)

    exported = runner.invoke(app, ["runs", "export", run_id, "--home", str(home), "--output", str(export_dir)])

    assert exported.exit_code == 0
    manifest = json.loads((export_dir / run_id / "run.json").read_text(encoding="utf-8"))
    contracts = json.loads((export_dir / run_id / "tool_contracts.json").read_text(encoding="utf-8"))
    file_contract = next(item for item in manifest["tool_contract_snapshots"] if item["tool_name"] == "file.write_text")

    assert file_contract["contract"]["side_effect"] == "local_write"
    assert any(item["tool_name"] == "file.write_text" for item in contracts)
