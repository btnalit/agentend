import json
import re
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import ContextDroppedItem, ContextLedger, ContextPackItem, CostUsage, ErrorRecord, Run
from agentend.db.session import session_scope


def _eval_id(output: str) -> str:
    match = re.search(r"Eval:\s+([0-9a-f-]+)", output)
    assert match is not None, output
    return match.group(1)


def _report(runner: CliRunner, home: Path, eval_run_id: str) -> dict:
    result = runner.invoke(app, ["eval", "report", eval_run_id, "--home", str(home)])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_context_policy_cli_merges_project_and_records_dropped_reasons(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    assert runner.invoke(app, ["llm", "set", "--home", str(home), "--provider", "fake", "--model", "fake-model"]).exit_code == 0
    (home / "workflows" / "definitions" / "context_policy_budget.yaml").write_text(
        """id: context_policy_budget
name: Context Policy Budget
context:
  include_memory: true
  retrieve_top_k: 10
nodes:
  - id: answer
    type: llm
    prompt: "Use the current task: {input}"
  - id: final
    type: final
    depends_on: [answer]
""",
        encoding="utf-8",
    )

    global_policy = runner.invoke(
        app,
        [
            "context",
            "policy",
            "set",
            "--home",
            str(home),
            "--scope",
            "global",
            "--target",
            "default",
            "--json",
            '{"redact_secrets": true, "max_items": 10}',
        ],
    )
    project_policy = runner.invoke(
        app,
        [
            "context",
            "policy",
            "set",
            "--home",
            str(home),
            "--scope",
            "project",
            "--target",
            "default",
            "--json",
            json.dumps(
                {
                    "max_items": 4,
                    "memory_scopes": ["project", "task"],
                    "retrieve_top_k": 10,
                    "min_memory_confidence": 0.5,
                    "trusted_memory_sources": ["manual"],
                },
                sort_keys=True,
            ),
        ],
    )
    shown = runner.invoke(app, ["context", "policy", "show", "--home", str(home), "--scope", "project", "--target", "default"])
    assert global_policy.exit_code == 0, global_policy.output
    assert project_policy.exit_code == 0, project_policy.output
    assert shown.exit_code == 0, shown.output
    assert '"max_items": 4' in shown.output

    for args in [
        ["--scope", "project", "--confidence", "1.0", "--content", "policy anchor trusted project memory"],
        ["--scope", "project", "--confidence", "0.1", "--content", "policy anchor low confidence memory"],
        ["--scope", "project", "--ttl", "2000-01-01T00:00:00+00:00", "--content", "policy anchor expired memory"],
        ["--scope", "task", "--source", "web", "--content", "policy anchor untrusted web memory"],
    ]:
        wrote = runner.invoke(app, ["memory", "write", "--home", str(home), *args])
        assert wrote.exit_code == 0, wrote.output

    preview = runner.invoke(app, ["context", "preview", "--home", str(home), "--workflow", "context_policy_budget", "--input", "policy anchor"])
    run = runner.invoke(app, ["workflows", "run", "context_policy_budget", "--home", str(home), "--input", "policy anchor"])

    assert preview.exit_code == 0, preview.output
    assert '"redact_secrets": true' in preview.output
    assert '"max_items": 4' in preview.output
    assert run.exit_code == 0, run.output
    with session_scope(home) as session:
        ledger = session.execute(select(ContextLedger).order_by(ContextLedger.created_at.desc())).scalars().first()
        assert ledger is not None
        items = session.execute(select(ContextPackItem).where(ContextPackItem.ledger_id == ledger.id)).scalars().all()
        dropped = session.execute(select(ContextDroppedItem).where(ContextDroppedItem.ledger_id == ledger.id)).scalars().all()
        policy_item = next(item for item in items if item.item_type == "context_policy")
        merged_policy = json.loads(policy_item.summary)
        assert merged_policy["redact_secrets"] is True
        assert merged_policy["max_items"] == 4
        assert len(items) <= 4
        selected_text = "\n".join(item.summary for item in items)
        assert "low confidence memory" not in selected_text
        assert "expired memory" not in selected_text
        assert "untrusted web memory" not in selected_text
        reasons = {row.reason for row in dropped}
        assert {"max_items_exceeded", "memory_low_confidence", "memory_expired", "memory_untrusted_source"} <= reasons

    shown_ledger = runner.invoke(app, ["context", "ledger", "show", ledger.id, "--home", str(home)])
    assert shown_ledger.exit_code == 0, shown_ledger.output
    assert "Dropped context items" in shown_ledger.output
    assert "memory_low_confidence" in shown_ledger.output


def test_skill_policy_cannot_relax_global_redaction(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    assert runner.invoke(app, ["llm", "set", "--home", str(home), "--provider", "fake", "--model", "fake-model"]).exit_code == 0
    assert runner.invoke(
        app,
        [
            "context",
            "policy",
            "set",
            "--home",
            str(home),
            "--scope",
            "global",
            "--target",
            "default",
            "--json",
            '{"redact_secrets": true, "max_items": 10}',
        ],
    ).exit_code == 0
    assert runner.invoke(
        app,
        [
            "context",
            "policy",
            "set",
            "--home",
            str(home),
            "--scope",
            "skill",
            "--target",
            "file.workspace_ops",
            "--json",
            '{"redact_secrets": false, "max_items": 3}',
        ],
    ).exit_code == 0

    result = runner.invoke(app, ["skills", "run", "file.workspace_ops", "--home", str(home), "--input", '{"task":"policy merge"}'])

    assert result.exit_code == 0, result.output
    with session_scope(home) as session:
        ledger = session.execute(select(ContextLedger).order_by(ContextLedger.created_at.desc())).scalars().first()
        assert ledger is not None
        items = session.execute(select(ContextPackItem).where(ContextPackItem.ledger_id == ledger.id)).scalars().all()
        policy_item = next(item for item in items if item.item_type == "context_policy")
        merged_policy = json.loads(policy_item.summary)
        assert merged_policy["redact_secrets"] is True
        assert merged_policy["max_items"] == 3
        assert len(items) <= 3


def test_workflow_budget_limits_are_enforced(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    assert runner.invoke(app, ["llm", "set", "--home", str(home), "--provider", "fake", "--model", "fake-model"]).exit_code == 0
    workflow_dir = home / "workflows" / "definitions"
    (workflow_dir / "budget_calls.yaml").write_text(
        """id: budget_calls
name: Budget Calls
nodes:
  - id: first
    type: llm
    prompt: "first {input}"
  - id: second
    type: llm
    depends_on: [first]
    prompt: "second {first}"
  - id: final
    type: final
    depends_on: [second]
""",
        encoding="utf-8",
    )
    (workflow_dir / "budget_input.yaml").write_text(
        """id: budget_input
name: Budget Input
nodes:
  - id: answer
    type: llm
    prompt: "answer {input}"
  - id: final
    type: final
    depends_on: [answer]
""",
        encoding="utf-8",
    )
    (workflow_dir / "budget_output.yaml").write_text(
        """id: budget_output
name: Budget Output
nodes:
  - id: answer
    type: llm
    prompt: "answer {input}"
  - id: final
    type: final
    depends_on: [answer]
""",
        encoding="utf-8",
    )
    assert runner.invoke(app, ["budget", "set", "--home", str(home), "--workflow", "budget_calls", "--max-llm-calls", "1"]).exit_code == 0
    assert runner.invoke(app, ["budget", "set", "--home", str(home), "--workflow", "budget_input", "--max-input-tokens", "1"]).exit_code == 0
    assert runner.invoke(app, ["budget", "set", "--home", str(home), "--workflow", "budget_output", "--max-output-tokens", "1"]).exit_code == 0

    calls = runner.invoke(app, ["workflows", "run", "budget_calls", "--home", str(home), "--input", "budget anchor"])
    input_tokens = runner.invoke(app, ["workflows", "run", "budget_input", "--home", str(home), "--input", "budget anchor"])
    output_tokens = runner.invoke(app, ["workflows", "run", "budget_output", "--home", str(home), "--input", "budget anchor"])

    assert calls.exit_code == 1
    assert input_tokens.exit_code == 1
    assert output_tokens.exit_code == 1
    with session_scope(home) as session:
        runs = session.execute(select(Run).where(Run.workflow_id.in_(["budget_calls", "budget_input", "budget_output"]))).scalars().all()
        errors = session.execute(select(ErrorRecord).where(ErrorRecord.error_code == "budget_exceeded")).scalars().all()
        assert {run.workflow_id for run in runs if run.status == "failed"} == {"budget_calls", "budget_input", "budget_output"}
        assert len(errors) == 3
        assert any("max_llm_calls" in error.message for error in errors)
        assert any("max_input_tokens" in error.message for error in errors)
        assert any("max_output_tokens" in error.message for error in errors)


def test_workflow_llm_step_uses_model_route_and_records_cost_usage(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    assert runner.invoke(app, ["llm", "set", "--home", str(home), "--provider", "fake", "--model", "default-model"]).exit_code == 0
    route = runner.invoke(
        app,
        [
            "models",
            "routes",
            "set",
            "workflow_step",
            "--home",
            str(home),
            "--provider",
            "fake",
            "--model",
            "route-model",
        ],
    )

    result = runner.invoke(app, ["workflows", "run", "simple_chat", "--home", str(home), "--input", "route check"])

    assert route.exit_code == 0, route.output
    assert result.exit_code == 0, result.output
    with session_scope(home) as session:
        ledger = session.execute(select(ContextLedger)).scalar_one()
        usage = session.execute(select(CostUsage)).scalar_one()
        assert ledger.model_provider == "fake"
        assert ledger.model_model == "route-model"
        assert usage.provider == "fake"
        assert usage.model == "route-model"
        assert usage.model_stage == "workflow_step"
        assert usage.input_tokens > 0
        assert usage.output_tokens > 0
        assert usage.total_tokens == usage.input_tokens + usage.output_tokens
        assert usage.usage_source == "estimated"

    budget = runner.invoke(app, ["budget", "set", "--home", str(home), "--workflow", "simple_chat", "--max-llm-calls", "3"])
    shown = runner.invoke(app, ["budget", "show", "--home", str(home), "--workflow", "simple_chat"])
    assert budget.exit_code == 0, budget.output
    assert shown.exit_code == 0, shown.output
    assert "usage_calls=1" in shown.output


def test_context_long_eval_covers_policy_budget_and_source_paths(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    assert runner.invoke(app, ["llm", "set", "--home", str(home), "--provider", "fake", "--model", "fake-model"]).exit_code == 0

    listed = runner.invoke(app, ["eval", "list"])
    result = runner.invoke(app, ["eval", "run", "context-long", "--home", str(home)])

    assert listed.exit_code == 0
    assert "context-long" in listed.output
    assert result.exit_code == 0, result.output
    payload = _report(runner, home, _eval_id(result.output))
    cases = {case["id"]: case for case in payload["cases"]}

    assert payload["suite"] == "context-long"
    assert payload["status"] == "passed"
    assert {
        "long-input-retained",
        "multi-workflow-ledgers",
        "real-search-provider",
        "skill-policy-merge",
        "memory-guard-dropped-reasons",
    } <= set(cases)
    for case in cases.values():
        assert case["status"] == "passed"
        assert case["assertions"]
        assert all(assertion["status"] == "passed" for assertion in case["assertions"])
