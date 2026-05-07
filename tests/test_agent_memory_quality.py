import json
from pathlib import Path

from typer.testing import CliRunner
from sqlalchemy import select

from agentend.cli import app
from agentend.core.agent_run import AgentRunController
from agentend.core.memory_quality import compile_project_memory_digest, lint_memory_items
from agentend.core.memory_store import search_memory_candidates, search_memory_items, write_memory_item
from agentend.db.models import MemoryItem, MemoryUseEvent
from agentend.db.session import session_scope


def test_memory_use_events_record_run_outcome(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    with session_scope(home) as session:
        memory = write_memory_item(
            session,
            home,
            content="list project test command explain evidence pytest python -m pytest",
            scope="project",
            source="agent_consolidator",
            confidence="0.95",
            tags=["subject:test-command", "type:procedure"],
        )

    result = AgentRunController(home).run(
        "List project test command and explain evidence.",
        max_iterations=2,
    )

    assert result.status == "completed"
    with session_scope(home) as session:
        events = session.execute(select(MemoryUseEvent).where(MemoryUseEvent.agent_run_id == result.agent_run_id)).scalars().all()
        assert any(event.memory_id == memory.id and event.outcome == "helped" for event in events)


def test_memory_use_event_updates_after_resume_success(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    with session_scope(home) as session:
        memory = write_memory_item(
            session,
            home,
            content="list project test command explain evidence pytest python -m pytest",
            scope="project",
            source="agent_consolidator",
            confidence="0.95",
            tags=["subject:test-command", "type:procedure"],
        )
    original_execute_action = AgentRunController._execute_action

    def irrelevant_completed_action(self, selected, request, *, agent_run_id: str, iteration_id: str) -> dict:
        return {
            "status": "completed",
            "run_id": None,
            "output": "Goal: List project test command and explain evidence.\nPython 3.13.7",
            "error": None,
        }

    monkeypatch.setattr(AgentRunController, "_execute_action", irrelevant_completed_action)
    failed = AgentRunController(home).run(
        "List project test command and explain evidence.",
        max_iterations=1,
    )
    assert failed.status == "failed"
    with session_scope(home) as session:
        event = session.execute(select(MemoryUseEvent).where(MemoryUseEvent.memory_id == memory.id)).scalar_one()
        assert event.outcome == "not_enough"

    monkeypatch.setattr(AgentRunController, "_execute_action", original_execute_action)
    resumed = AgentRunController(home).resume(failed.agent_run_id, max_iterations=1)

    assert resumed.status == "completed"
    with session_scope(home) as session:
        events = session.execute(select(MemoryUseEvent).where(MemoryUseEvent.memory_id == memory.id)).scalars().all()
        assert events
        assert {event.run_status for event in events} == {"completed"}
        assert {event.outcome for event in events} == {"helped"}


def test_memory_digest_compiles_and_lint_reports_issues(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    with session_scope(home) as session:
        write_memory_item(
            session,
            home,
            content="Project test command: python -m pytest tests -q.",
            scope="project",
            source="agent_consolidator",
            confidence="0.9",
            tags=["subject:test-command"],
        )
        write_memory_item(
            session,
            home,
            content="x" * 2200,
            scope="project",
            source="agent_consolidator",
            confidence="0.6",
            tags=[],
        )
        digest = compile_project_memory_digest(session, max_items=5, max_chars=800)
        assert digest.scope == "project"
        assert len(digest.content) <= 800
        assert {"memory-digest", "compiled", "scope:project"}.issubset(set(json.loads(digest.tags_json)))

        issues = lint_memory_items(session, max_content_chars=1000)
        issue_codes = {issue["issue"] for issue in issues}
        assert "memory_overlong" in issue_codes
        assert "memory_untagged" in issue_codes
        assert "memory_low_confidence" in issue_codes

        found = search_memory_items(session, "pytest", scope="project", limit=5)
        assert any(row.id == digest.id for row in found)
        assert session.get(MemoryItem, digest.id).status == "active"


def test_memory_search_keeps_unicode_terms_after_stopword_filter(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    with session_scope(home) as session:
        memory = write_memory_item(
            session,
            home,
            content="项目测试命令: python -m pytest tests -q.",
            scope="project",
            source="agent_consolidator",
            confidence="0.9",
            tags=["subject:test-command"],
        )

        found = search_memory_items(session, "项目 测试命令", scope="project", limit=5)

        assert any(row.id == memory.id for row in found)


def test_memory_candidate_search_respects_scope_when_fts_matches(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    with session_scope(home) as session:
        user_memory = write_memory_item(
            session,
            home,
            content="pytest command belongs to user memory",
            scope="user",
            source="agent_consolidator",
            confidence="0.99",
            tags=["subject:test-command"],
        )
        write_memory_item(
            session,
            home,
            content="project memory without the searched term",
            scope="project",
            source="agent_consolidator",
            confidence="0.9",
            tags=["subject:project"],
        )

        found = search_memory_candidates(session, "pytest", scope="project", limit=5)

        assert all(row.scope == "project" for row in found)
        assert all(row.id != user_memory.id for row in found)
