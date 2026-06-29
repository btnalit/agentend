# tests/test_enqueue_resume_intent.py
from pathlib import Path
from uuid import uuid4
from typer.testing import CliRunner
from sqlalchemy import select
from agentend.cli import app
from agentend.core.tasks import TaskManager
from agentend.db.models import AgentRun, TaskItem
from agentend.db.session import session_scope


def _init_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    CliRunner().invoke(app, ["init", "--home", str(home)])
    return home


def _make_run(home: Path, status: str = "waiting_input") -> str:
    run_id = str(uuid4())
    with session_scope(home) as session:
        session.add(AgentRun(
            id=run_id, channel="task", external_user_id="test",
            goal="g", status=status, max_iterations=3,
        ))
    return run_id


def test_enqueue_resume_intent_creates_task(tmp_path: Path) -> None:
    home = _init_home(tmp_path)
    run_id = _make_run(home)
    mgr = TaskManager(home)

    task = mgr.enqueue_resume_intent(run_id, run_mode="normal", answer_text="approve")

    assert task is not None
    assert task.agent_run_id == run_id
    assert task.workflow_id == "agent.resume"
    assert task.status == "pending"
    assert task.source == "approval"
    with session_scope(home) as session:
        stored = session.get(TaskItem, task.id)
        assert stored is not None


def test_enqueue_resume_intent_idempotent(tmp_path: Path) -> None:
    home = _init_home(tmp_path)
    run_id = _make_run(home)
    mgr = TaskManager(home)

    first = mgr.enqueue_resume_intent(run_id)
    second = mgr.enqueue_resume_intent(run_id)

    assert first is not None
    assert second is None  # 幂等：已有 pending，跳过
    with session_scope(home) as session:
        count = len(session.execute(
            select(TaskItem).where(TaskItem.agent_run_id == run_id)
        ).scalars().all())
    assert count == 1
