from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.core.memory_relation import MemoryRelationClassifier
from agentend.db.models import MemoryCandidate, MemoryItem, MemoryLink
from agentend.db.session import session_scope


def _write_project_memory(runner: CliRunner, home: Path, content: str) -> str:
    result = runner.invoke(
        app,
        [
            "memory",
            "write",
            "--home",
            str(home),
            "--scope",
            "project",
            "--content",
            content,
            "--tags",
            "subject:test-command,merge:project:project_fact:test-command",
        ],
    )
    assert result.exit_code == 0, result.output
    with session_scope(home) as session:
        return session.execute(select(MemoryItem)).scalar_one().id


def test_auto_relation_update_supersedes_without_explicit_tag(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    old_id = _write_project_memory(runner, home, "Project test command is pytest -q.")
    with session_scope(home) as session:
        session.add(
            MemoryCandidate(
                id="candidate-auto-update",
                type="project_fact",
                scope="project",
                content="Project test command is python -m pytest tests -q.",
                merge_key="project:project_fact:test-command-v2",
                confidence="0.92",
                status="pending",
                tags_json='["subject:test-command", "evidence:test"]',
            )
        )

    consolidated = runner.invoke(app, ["memory", "consolidate", "--home", str(home)])

    assert consolidated.exit_code == 0, consolidated.output
    assert "superseded=1" in consolidated.output
    with session_scope(home) as session:
        old = session.get(MemoryItem, old_id)
        candidate = session.get(MemoryCandidate, "candidate-auto-update")
        replacement = session.execute(
            select(MemoryItem).where(MemoryItem.content == "Project test command is python -m pytest tests -q.")
        ).scalar_one()
        link = session.execute(
            select(MemoryLink)
            .where(MemoryLink.memory_id == replacement.id)
            .where(MemoryLink.relation == "supersedes")
        ).scalar_one()
        assert old.status == "superseded"
        assert candidate.status == "superseded"
        assert candidate.memory_id == replacement.id
        assert link.source_id == old_id


def test_auto_relation_low_confidence_conflict_needs_review(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    old_id = _write_project_memory(runner, home, "Use python -m pytest tests -q for verification.")
    with session_scope(home) as session:
        session.add(
            MemoryCandidate(
                id="candidate-low-conflict",
                type="project_fact",
                scope="project",
                content="Use npm test for verification.",
                merge_key="project:project_fact:test-command-alt",
                confidence="0.55",
                status="pending",
                tags_json='["subject:test-command"]',
            )
        )

    consolidated = runner.invoke(app, ["memory", "consolidate", "--home", str(home)])

    assert consolidated.exit_code == 0, consolidated.output
    assert "needs_review=1" in consolidated.output
    with session_scope(home) as session:
        active = session.get(MemoryItem, old_id)
        candidate = session.get(MemoryCandidate, "candidate-low-conflict")
        active_rows = session.execute(select(MemoryItem).where(MemoryItem.status == "active")).scalars().all()
        assert active.status == "active"
        assert candidate.status == "needs_review"
        assert candidate.memory_id == old_id
        assert len(active_rows) == 1


def test_no_auto_relations_preserves_explicit_only_behavior(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    old_id = _write_project_memory(runner, home, "Project test command is pytest -q.")
    with session_scope(home) as session:
        session.add(
            MemoryCandidate(
                id="candidate-no-auto",
                type="project_fact",
                scope="project",
                content="Project test command is python -m pytest tests -q.",
                merge_key="project:project_fact:test-command-v2",
                confidence="0.92",
                status="pending",
                tags_json='["subject:test-command", "evidence:test"]',
            )
        )

    consolidated = runner.invoke(app, ["memory", "consolidate", "--home", str(home), "--no-auto-relations"])

    assert consolidated.exit_code == 0, consolidated.output
    assert "superseded=0" in consolidated.output
    with session_scope(home) as session:
        old = session.get(MemoryItem, old_id)
        candidate = session.get(MemoryCandidate, "candidate-no-auto")
        active_rows = session.execute(select(MemoryItem).where(MemoryItem.status == "active")).scalars().all()
        assert old.status == "active"
        assert candidate.status == "created"
        assert len(active_rows) == 2


def test_relation_classifier_uses_structured_llm_decision(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    old_id = _write_project_memory(runner, home, "Project test command is pytest -q.")
    with session_scope(home) as session:
        candidate = MemoryCandidate(
            id="candidate-llm-update",
            type="project_fact",
            scope="project",
            content="Project test command is python -m pytest tests -q.",
            merge_key="project:project_fact:test-command-v2",
            confidence="0.91",
            status="pending",
            tags_json='["subject:test-command", "evidence:test"]',
        )
        session.add(candidate)
        session.flush()
        classifier = MemoryRelationClassifier(
            llm_complete=lambda _prompt: (
                '{"relation":"updates","target_memory_id":"'
                + old_id
                + '","confidence":0.91,"replacement_content":"Project test command is python -m pytest tests -q.",'
                + '"reason":"structured llm update","evidence_refs":["test-fixture"]}'
            )
        )

        decision = classifier.classify(session, candidate)

    assert decision.relation == "updates"
    assert decision.target_memory_id == old_id
    assert decision.confidence == 0.91
    assert decision.reason == "structured llm update"
