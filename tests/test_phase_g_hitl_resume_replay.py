import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import Checkpoint, ClarificationRequest, Conversation, Run, ToolCall
from agentend.db.session import session_scope
from agentend.telegram_bot import TelegramMessageRouter


def _run_id(output: str) -> str:
    match = re.search(r"Run:\s+([0-9a-f-]+)", output)
    assert match is not None, output
    return match.group(1)


def test_human_input_creates_clarification_and_resume_answer_continues_same_run(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    (home / "workflows" / "definitions" / "ask_name.yaml").write_text(
        """id: ask_name
name: Ask Name
nodes:
  - id: ask
    type: human_input
    input:
      type: missing_input
      prompt: "Name?"
      reason: "Need a name before final output."
  - id: final
    type: final
    depends_on: [ask]
""",
        encoding="utf-8",
    )

    started = runner.invoke(app, ["workflows", "run", "ask_name", "--home", str(home), "--input", "start"])
    run_id = _run_id(started.output)
    listed = runner.invoke(app, ["clarifications", "list", "--home", str(home)])

    assert started.exit_code == 0
    assert "Name?" in started.output
    assert listed.exit_code == 0
    assert "Name?" in listed.output
    with session_scope(home) as session:
        run = session.get(Run, run_id)
        request = session.execute(select(ClarificationRequest).where(ClarificationRequest.run_id == run_id)).scalar_one()
        assert run.status == "waiting_input"
        assert request.status == "pending"
        assert request.request_type == "missing_input"
        assert request.resume_token

    resumed = runner.invoke(app, ["runs", "resume", run_id, "--home", str(home), "--answer", "Alice"])

    assert resumed.exit_code == 0
    assert "Status: completed" in resumed.output
    assert "Alice" in resumed.output
    with session_scope(home) as session:
        run = session.get(Run, run_id)
        request = session.execute(select(ClarificationRequest).where(ClarificationRequest.run_id == run_id)).scalar_one()
        checkpoint = session.execute(select(Checkpoint).where(Checkpoint.run_id == run_id).where(Checkpoint.node_id == "ask")).scalar_one()
        assert run.status == "completed"
        assert json.loads(run.result_json)["content"] == "Alice"
        assert request.status == "answered"
        assert request.answer == "Alice"
        assert checkpoint.step_id == request.step_id


def test_expired_clarification_cannot_resume_run(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    (home / "workflows" / "definitions" / "ask_expiring.yaml").write_text(
        """id: ask_expiring
name: Ask Expiring
nodes:
  - id: ask
    type: human_input
    input:
      type: missing_input
      prompt: "Need value?"
  - id: final
    type: final
    depends_on: [ask]
""",
        encoding="utf-8",
    )
    started = runner.invoke(app, ["workflows", "run", "ask_expiring", "--home", str(home), "--input", "start"])
    run_id = _run_id(started.output)
    with session_scope(home) as session:
        request = session.execute(select(ClarificationRequest).where(ClarificationRequest.run_id == run_id)).scalar_one()
        request.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    resumed = runner.invoke(app, ["runs", "resume", run_id, "--home", str(home), "--answer", "late"])

    assert resumed.exit_code == 1
    assert "expired" in resumed.output.lower()
    with session_scope(home) as session:
        run = session.get(Run, run_id)
        request = session.execute(select(ClarificationRequest).where(ClarificationRequest.run_id == run_id)).scalar_one()
        assert run.status == "waiting_input"
        assert request.status == "expired"


def test_telegram_router_answers_pending_clarification_with_shared_resume_path(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    (home / "workflows" / "definitions" / "ask_telegram.yaml").write_text(
        """id: ask_telegram
name: Ask Telegram
nodes:
  - id: ask
    type: human_input
    input:
      type: missing_input
      prompt: "Telegram value?"
  - id: final
    type: final
    depends_on: [ask]
""",
        encoding="utf-8",
    )
    router = TelegramMessageRouter(home)

    started = router.handle_text("chat-1", "user-1", "/run ask_telegram start")
    run_id = _run_id(started)
    answered = router.handle_text("chat-1", "user-1", "telegram answer")

    assert "Telegram value?" in started
    assert "telegram answer" in answered
    with session_scope(home) as session:
        request = session.execute(select(ClarificationRequest).where(ClarificationRequest.run_id == run_id)).scalar_one()
        run = session.get(Run, run_id)
        assert request.status == "answered"
        assert run.status == "completed"


def test_resume_from_checkpoint_after_fixing_workflow_does_not_rerun_completed_steps(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    workflow = home / "workflows" / "definitions" / "resumable_failure.yaml"
    workflow.write_text(
        """id: resumable_failure
name: Resumable Failure
nodes:
  - id: save
    type: tool
    tool: fs.write_text
    input:
      path: marker.txt
      content: "first:{input}"
  - id: fail
    type: tool
    tool: missing.tool
    depends_on: [save]
  - id: final
    type: final
    depends_on: [fail]
""",
        encoding="utf-8",
    )

    failed = runner.invoke(app, ["workflows", "run", "resumable_failure", "--home", str(home), "--input", "value"])
    run_id = _run_id(failed.output)
    assert failed.exit_code == 1
    with session_scope(home) as session:
        checkpoint = session.execute(select(Checkpoint).where(Checkpoint.run_id == run_id).where(Checkpoint.node_id == "save")).scalar_one()
        assert session.execute(select(ToolCall).where(ToolCall.run_id == run_id).where(ToolCall.tool_name == "fs.write_text")).scalar_one()

    workflow.write_text(
        """id: resumable_failure
name: Resumable Failure
nodes:
  - id: save
    type: tool
    tool: fs.write_text
    input:
      path: marker.txt
      content: "first:{input}"
  - id: fail
    type: tool
    tool: fs.read_text
    depends_on: [save]
    input:
      path: marker.txt
  - id: final
    type: final
    depends_on: [fail]
""",
        encoding="utf-8",
    )

    resumed = runner.invoke(app, ["runs", "resume", run_id, "--home", str(home), "--checkpoint", checkpoint.id])

    assert resumed.exit_code == 0
    assert "Status: completed" in resumed.output
    assert "first:value" in resumed.output
    with session_scope(home) as session:
        run = session.get(Run, run_id)
        writes = session.execute(select(ToolCall).where(ToolCall.run_id == run_id).where(ToolCall.tool_name == "fs.write_text")).scalars().all()
        reads = session.execute(select(ToolCall).where(ToolCall.run_id == run_id).where(ToolCall.tool_name == "fs.read_text")).scalars().all()
        assert run.status == "completed"
        assert len(writes) == 1
        assert len(reads) == 1


def test_runs_replay_creates_new_safe_run_and_blocks_external_write(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    safe = runner.invoke(app, ["workflows", "run", "simple_chat", "--home", str(home), "--input", "replay-safe"])
    source_run_id = _run_id(safe.output)

    replayed = runner.invoke(app, ["runs", "replay", source_run_id, "--home", str(home)])
    replay_run_id = _run_id(replayed.output)

    assert replayed.exit_code == 0
    assert replay_run_id != source_run_id
    assert "replay-safe" in replayed.output
    with session_scope(home) as session:
        replay_run = session.get(Run, replay_run_id)
        conversation = session.get(Conversation, replay_run.conversation_id)
        assert replay_run.status == "completed"
        assert conversation.channel == "replay"

    (home / "workflows" / "definitions" / "send_message.yaml").write_text(
        """id: send_message
name: Send Message
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
    sent = runner.invoke(app, ["workflows", "run", "send_message", "--home", str(home), "--input", "x"])
    blocked = runner.invoke(app, ["runs", "replay", _run_id(sent.output), "--home", str(home)])

    assert sent.exit_code == 0
    assert blocked.exit_code == 1
    assert "external_write is blocked during replay" in blocked.output
