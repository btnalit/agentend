import json
import re
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import ActionPolicyDecision, Run, ToolContractSnapshot
from agentend.db.session import session_scope


def _run_id(output: str) -> str:
    match = re.search(r"Run:\s+([0-9a-f-]+)", output)
    assert match is not None, output
    return match.group(1)


def test_replay_dry_run_and_actual_replay_block_local_write(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    (home / "workflows" / "definitions" / "replay_reuse.yaml").write_text(
        """id: replay_reuse
name: Replay Reuse
nodes:
  - id: save
    type: tool
    tool: fs.write_text
    input:
      path: replay-marker.txt
      content: "first:{input}"
  - id: final
    type: final
    depends_on: [save]
""",
        encoding="utf-8",
    )
    source = runner.invoke(app, ["workflows", "run", "replay_reuse", "--home", str(home), "--input", "value"])
    source_run_id = _run_id(source.output)

    dry_run = runner.invoke(app, ["runs", "replay", source_run_id, "--home", str(home), "--dry-run"])

    assert dry_run.exit_code == 0
    plan = json.loads(dry_run.output)
    assert plan["dry_run"] is True
    assert plan["source_run_id"] == source_run_id
    save_step = next(step for step in plan["steps"] if step["node_id"] == "save")
    assert plan["status"] == "blocked"
    assert save_step["strategy"] == "block"
    assert save_step["tool_name"] == "fs.write_text"
    assert save_step["side_effect"] == "local_write"

    replayed = runner.invoke(app, ["runs", "replay", source_run_id, "--home", str(home)])

    assert replayed.exit_code == 1
    assert "local_write is blocked during replay" in replayed.output
    with session_scope(home) as session:
        replay_run = session.execute(select(Run).where(Run.workflow_id == "replay_reuse").where(Run.id != source_run_id)).scalar_one()
        decision = session.execute(
            select(ActionPolicyDecision)
            .where(ActionPolicyDecision.run_id == replay_run.id)
            .where(ActionPolicyDecision.tool_name == "fs.write_text")
        ).scalar_one()
        assert replay_run.status == "failed"
        assert decision.decision == "block"
        assert decision.side_effect == "local_write"


def test_replay_dry_run_reports_contract_drift_and_actual_replay_blocks(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    (home / "input.txt").write_text("stable", encoding="utf-8")
    (home / "workflows" / "definitions" / "replay_drift.yaml").write_text(
        """id: replay_drift
name: Replay Drift
nodes:
  - id: read
    type: tool
    tool: fs.read_text
    input:
      path: input.txt
  - id: final
    type: final
    depends_on: [read]
""",
        encoding="utf-8",
    )
    source = runner.invoke(app, ["workflows", "run", "replay_drift", "--home", str(home), "--input", "x"])
    source_run_id = _run_id(source.output)
    with session_scope(home) as session:
        snapshot = session.execute(
            select(ToolContractSnapshot)
            .where(ToolContractSnapshot.run_id == source_run_id)
            .where(ToolContractSnapshot.tool_name == "fs.read_text")
        ).scalar_one()
        contract = json.loads(snapshot.contract_json)
        contract["input_schema"] = {"type": "object", "required": ["path", "encoding"]}
        snapshot.contract_json = json.dumps(contract, ensure_ascii=False, sort_keys=True)

    dry_run = runner.invoke(app, ["runs", "replay", source_run_id, "--home", str(home), "--dry-run"])

    assert dry_run.exit_code == 0
    plan = json.loads(dry_run.output)
    read_step = next(step for step in plan["steps"] if step["node_id"] == "read")
    assert plan["status"] == "blocked"
    assert read_step["strategy"] == "skip"
    assert read_step["contract_drift"] is True
    assert "input_schema" in read_step["contract_diff"]

    replayed = runner.invoke(app, ["runs", "replay", source_run_id, "--home", str(home)])

    assert replayed.exit_code == 1
    assert "contract drift" in replayed.output.lower()


def test_replay_dry_run_and_actual_replay_block_external_write(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    (home / "workflows" / "definitions" / "replay_external_write.yaml").write_text(
        """id: replay_external_write
name: Replay External Write
nodes:
  - id: send
    type: tool
    tool: im.telegram.send_message
    input:
      chat_id: "1"
      text: "hello"
      dry_run: true
  - id: final
    type: final
    depends_on: [send]
""",
        encoding="utf-8",
    )
    source = runner.invoke(app, ["workflows", "run", "replay_external_write", "--home", str(home), "--input", "x"])
    source_run_id = _run_id(source.output)

    dry_run = runner.invoke(app, ["runs", "replay", source_run_id, "--home", str(home), "--dry-run"])

    assert dry_run.exit_code == 0
    plan = json.loads(dry_run.output)
    send_step = next(step for step in plan["steps"] if step["node_id"] == "send")
    assert plan["status"] == "blocked"
    assert send_step["strategy"] == "block"
    assert "external_write is blocked during replay" in send_step["skip_reason"]

    replayed = runner.invoke(app, ["runs", "replay", source_run_id, "--home", str(home)])

    assert replayed.exit_code == 1
    assert "external_write is blocked during replay" in replayed.output
