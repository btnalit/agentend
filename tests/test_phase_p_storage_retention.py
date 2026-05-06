import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import (
    Checkpoint,
    Conversation,
    MemoryItem,
    Run,
    RunStep,
    Skill,
    StorageCleanupRun,
)
from agentend.db.session import database_path, session_scope


def test_storage_cleanup_dry_run_plan_and_confirm_delete_only_planned_items(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    old_artifact = home / "data" / "artifacts" / "run-old" / "old.txt"
    recent_artifact = home / "data" / "artifacts" / "run-new" / "new.txt"
    old_sandbox = home / "data" / "sandboxes" / "run-old" / "workspace"
    recent_sandbox = home / "data" / "sandboxes" / "run-new" / "workspace"
    old_cache = home / "data" / "cache" / "old-cache"
    for path in (old_artifact, recent_artifact, old_sandbox / "out.txt", recent_sandbox / "out.txt", old_cache / "cache.txt"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.name, encoding="utf-8")
    _make_old(old_artifact)
    _make_old(old_sandbox.parent)
    _make_old(old_cache)

    dry_run = runner.invoke(app, ["storage", "cleanup", "--home", str(home), "--older-than", "1d", "--dry-run"])
    plan_id = _plan_id(dry_run.output)
    assert dry_run.exit_code == 0
    assert old_artifact.exists()
    assert old_sandbox.exists()
    assert old_cache.exists()

    confirm = runner.invoke(
        app,
        ["storage", "cleanup", "--home", str(home), "--older-than", "1d", "--confirm", "--plan-id", plan_id],
    )

    assert confirm.exit_code == 0
    assert not old_artifact.exists()
    assert not old_sandbox.exists()
    assert not old_cache.exists()
    assert recent_artifact.exists()
    assert recent_sandbox.exists()
    with session_scope(home) as session:
        rows = session.execute(select(StorageCleanupRun).order_by(StorageCleanupRun.created_at)).scalars().all()
        assert [row.mode for row in rows] == ["dry-run", "completed"]
        assert rows[1].source_plan_id == plan_id
        deleted = json.loads(rows[1].deleted_json)
        assert all(item["status"] == "deleted" for item in deleted)
        assert {item["rule_id"] for item in deleted} >= {"artifacts-old", "sandboxes-old", "cache-old"}
        assert rows[1].deleted_count == len(deleted)


def test_storage_cleanup_requires_explicit_confirm_for_actual_deletion(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    old_file = home / "data" / "artifacts" / "run-old" / "old.txt"
    old_file.parent.mkdir(parents=True, exist_ok=True)
    old_file.write_text("old", encoding="utf-8")
    _make_old(old_file)

    result = runner.invoke(app, ["storage", "cleanup", "--home", str(home), "--older-than", "1d"])

    assert result.exit_code != 0
    assert "requires --dry-run or --confirm" in result.output
    assert old_file.exists()


def test_storage_cleanup_preserves_enabled_manual_and_recent_data(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    enabled_draft = home / "data" / "skill_drafts" / "enabled"
    stale_draft = home / "data" / "skill_drafts" / "stale"
    for path in (enabled_draft / "workflow.yaml", stale_draft / "workflow.yaml"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("nodes: []\n", encoding="utf-8")
        _make_old(path.parent)
    with session_scope(home) as session:
        session.add(
            Skill(
                id="enabled.skill",
                version="0.1.0",
                description="enabled",
                workflow_path=str(enabled_draft / "workflow.yaml"),
                required_tools_json="[]",
                required_mcp_json="[]",
                input_schema_json="{}",
                output_schema_json="{}",
                enabled="true",
                source_type="draft",
                source_location=str(enabled_draft),
                manifest_json="{}",
            )
        )
        session.add(
            MemoryItem(
                id="manual-memory",
                scope="project",
                content="keep this manual memory",
                source="manual",
                confidence="1.0",
            )
        )
        run_id, old_checkpoint_id, recent_checkpoint_id = _add_checkpoint_fixture(session)

    result = runner.invoke(app, ["storage", "cleanup", "--home", str(home), "--older-than", "0d", "--confirm"])

    assert result.exit_code == 0
    assert enabled_draft.exists()
    assert not stale_draft.exists()
    with session_scope(home) as session:
        assert session.get(MemoryItem, "manual-memory") is not None
        assert session.get(Checkpoint, old_checkpoint_id) is None
        assert session.get(Checkpoint, recent_checkpoint_id) is not None
        checkpoints = session.execute(select(Checkpoint).where(Checkpoint.run_id == run_id)).scalars().all()
        assert [checkpoint.id for checkpoint in checkpoints] == [recent_checkpoint_id]


def test_storage_restore_to_temp_home_and_refuses_existing_database(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    restored_home = tmp_path / "restored-home"
    backup_dir = tmp_path / "backup"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    backup = runner.invoke(app, ["storage", "backup", "--home", str(home), "--output", str(backup_dir)])

    restored = runner.invoke(app, ["storage", "restore", str(backup_dir), "--home", str(restored_home)])
    refused = runner.invoke(app, ["storage", "restore", str(backup_dir), "--home", str(home)])

    assert backup.exit_code == 0
    assert restored.exit_code == 0
    assert database_path(restored_home).exists()
    assert refused.exit_code != 0
    assert "Refusing to overwrite existing AgentEnd database" in refused.output


def _make_old(path: Path) -> None:
    old_time = datetime.now(timezone.utc) - timedelta(days=2)
    timestamp = old_time.timestamp()
    if path.is_dir():
        for child in path.rglob("*"):
            os.utime(child, (timestamp, timestamp))
    os.utime(path, (timestamp, timestamp))


def _plan_id(output: str) -> str:
    match = re.search(r"plan=([0-9a-f-]+)", output)
    assert match is not None, output
    return match.group(1)


def _add_checkpoint_fixture(session) -> tuple[str, str, str]:
    conversation = Conversation(id="conversation", channel="test", external_user_id="test")
    run = Run(id="run", conversation_id=conversation.id, workflow_id="workflow", status="completed", input_json="{}", result_json="{}")
    step = RunStep(id="step", run_id=run.id, node_id="node", status="completed", input_json="{}", output_json="{}")
    old_created = datetime.now(timezone.utc) - timedelta(days=2)
    recent_created = datetime.now(timezone.utc)
    old_checkpoint = Checkpoint(
        id="old-checkpoint",
        run_id=run.id,
        step_id=step.id,
        node_id="node",
        state_json="{}",
        context_summary_json="{}",
        artifacts_json="[]",
        policy_decisions_json="[]",
        created_at=old_created,
    )
    recent_checkpoint = Checkpoint(
        id="recent-checkpoint",
        run_id=run.id,
        step_id=step.id,
        node_id="node",
        state_json="{}",
        context_summary_json="{}",
        artifacts_json="[]",
        policy_decisions_json="[]",
        created_at=recent_created,
    )
    session.add(conversation)
    session.add(run)
    session.add(step)
    session.add(old_checkpoint)
    session.add(recent_checkpoint)
    return run.id, old_checkpoint.id, recent_checkpoint.id
