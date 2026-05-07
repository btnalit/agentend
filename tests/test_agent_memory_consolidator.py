import re
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import MemoryCandidate, MemoryItem, MemoryLink
from agentend.db.session import session_scope


def _agent_run_id(output: str) -> str:
    match = re.search(r"AgentRun:\s+([0-9a-f-]+)", output)
    assert match is not None, output
    return match.group(1)


def test_memory_candidates_and_consolidation_are_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    ran = runner.invoke(app, ["agent", "run", "--home", str(home), "List project test command."])
    assert ran.exit_code == 0, ran.output
    agent_run_id = _agent_run_id(ran.output)

    candidates = runner.invoke(app, ["memory", "candidates", "--home", str(home), "--agent-run", agent_run_id])
    first = runner.invoke(app, ["memory", "consolidate", "--home", str(home), "--agent-run", agent_run_id])
    second = runner.invoke(app, ["memory", "consolidate", "--home", str(home), "--agent-run", agent_run_id])

    assert candidates.exit_code == 0, candidates.output
    assert "successful_procedure" in candidates.output
    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    with session_scope(home) as session:
        rows = session.execute(
            select(MemoryCandidate).where(MemoryCandidate.agent_run_id == agent_run_id)
        ).scalars().all()
        assert rows
        memory_items = session.execute(select(MemoryItem).where(MemoryItem.source == "agent_consolidator")).scalars().all()
        assert len(memory_items) == 1
        assert memory_items[0].created_by_run_id == agent_run_id
        assert "merge:" in memory_items[0].tags_json


def test_memory_supersede_marks_old_memory_inactive_and_links_replacement(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    old = runner.invoke(
        app,
        [
            "memory",
            "write",
            "--home",
            str(home),
            "--scope",
            "project",
            "--content",
            "Project test command is pytest -q",
            "--tags",
            "merge:project:project_fact:test-command",
        ],
    )
    assert old.exit_code == 0, old.output
    with session_scope(home) as session:
        old_memory = session.execute(select(MemoryItem)).scalar_one()
        candidate = MemoryCandidate(
            id="candidate-supersede",
            type="project_fact",
            scope="project",
            content="Project test command is python -m pytest tests -q",
            merge_key="project:project_fact:test-command-v2",
            confidence="0.95",
            status="pending",
            tags_json=f'["supersedes:{old_memory.id}", "merge:project:project_fact:test-command-v2"]',
        )
        session.add(candidate)

    consolidated = runner.invoke(app, ["memory", "consolidate", "--home", str(home)])
    search_old = runner.invoke(app, ["memory", "search", "pytest -q", "--home", str(home)])
    search_new = runner.invoke(app, ["memory", "search", "python pytest", "--home", str(home)])

    assert consolidated.exit_code == 0, consolidated.output
    assert "superseded=1" in consolidated.output
    assert "Project test command is pytest -q" not in search_old.output
    assert "python -m pytest tests -q" in search_new.output
    with session_scope(home) as session:
        refreshed_old = session.get(MemoryItem, old_memory.id)
        assert refreshed_old.status == "superseded"
        replacement = session.execute(
            select(MemoryItem).where(MemoryItem.content == "Project test command is python -m pytest tests -q")
        ).scalar_one()
        link = session.execute(
            select(MemoryLink).where(MemoryLink.memory_id == replacement.id).where(MemoryLink.relation == "supersedes")
        ).scalar_one()
        assert link.source_id == old_memory.id


def test_memory_conflict_and_reinforce_do_not_create_duplicate_active_memories(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    wrote = runner.invoke(
        app,
        [
            "memory",
            "write",
            "--home",
            str(home),
            "--scope",
            "project",
            "--content",
            "Use python -m pytest tests -q for full verification.",
            "--tags",
            "merge:project:successful_procedure:test-command",
        ],
    )
    assert wrote.exit_code == 0, wrote.output
    with session_scope(home) as session:
        active = session.execute(select(MemoryItem)).scalar_one()
        session.add(
            MemoryCandidate(
                id="candidate-conflict",
                type="project_fact",
                scope="project",
                content="Use npm test for full verification.",
                merge_key="project:project_fact:test-command-conflict",
                confidence="0.55",
                status="pending",
                tags_json=f'["conflicts:{active.id}"]',
            )
        )
        session.add(
            MemoryCandidate(
                id="candidate-reinforce",
                type="successful_procedure",
                scope="project",
                content="Use python -m pytest tests -q for full verification.",
                merge_key="project:successful_procedure:test-command",
                confidence="0.9",
                status="pending",
                tags_json='["merge:project:successful_procedure:test-command"]',
            )
        )

    consolidated = runner.invoke(app, ["memory", "consolidate", "--home", str(home)])

    assert consolidated.exit_code == 0, consolidated.output
    assert "conflicts=1" in consolidated.output
    assert "reinforced=1" in consolidated.output
    with session_scope(home) as session:
        active_rows = session.execute(select(MemoryItem).where(MemoryItem.status == "active")).scalars().all()
        assert len(active_rows) == 1
        conflict = session.get(MemoryCandidate, "candidate-conflict")
        reinforce = session.get(MemoryCandidate, "candidate-reinforce")
        assert conflict.status == "conflict"
        assert reinforce.status == "reinforced"
