import json
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.core.memory_gate import decide_memory_read, decide_memory_write
from agentend.core.memory_store import write_memory_item
from agentend.db.models import EventLog, MemoryItem
from agentend.db.session import session_scope


def test_memory_write_gate_rejects_untrusted_long_term_and_allows_task_scope() -> None:
    rejected = decide_memory_write(scope="project", source="web", confidence="0.9")
    allowed = decide_memory_write(scope="task", source="web", confidence="0.9")

    assert rejected.decision == "reject"
    assert rejected.reason_code == "memory_write_untrusted_long_term"
    assert rejected.trust_level == "external_untrusted"
    assert allowed.decision == "allow"
    assert allowed.reason_code == "memory_write_short_term_untrusted_allowed"
    assert allowed.allowed_use == ("answer_context", "not_instruction")


def test_memory_read_gate_classifies_strong_weak_and_dropped_context() -> None:
    strong = decide_memory_read(
        _memory("manual-project", source="manual", confidence="0.95"),
        scope=None,
        min_confidence=0.5,
        trusted_sources={"manual"},
    )
    weak = decide_memory_read(
        _memory("generated-project", source="agent_consolidator", confidence="0.85"),
        scope=None,
        min_confidence=0.5,
        trusted_sources={"manual", "agent_consolidator"},
    )
    low_confidence = decide_memory_read(
        _memory("manual-low", source="manual", confidence="0.1"),
        scope=None,
        min_confidence=0.5,
        trusted_sources={"manual"},
    )
    untrusted = decide_memory_read(
        _memory("web-task", source="web", confidence="0.9", scope="task"),
        scope=None,
        min_confidence=0.5,
        trusted_sources={"manual"},
    )

    assert strong.decision == "strong"
    assert strong.reason_code == "memory_read_strong"
    assert strong.trust_level == "trusted"
    assert weak.decision == "weak"
    assert weak.reason_code == "memory_read_weak"
    assert weak.trust_level == "generated"
    assert low_confidence.decision == "drop"
    assert low_confidence.reason_code == "memory_low_confidence"
    assert untrusted.decision == "drop"
    assert untrusted.reason_code == "memory_untrusted_source"


def test_write_memory_item_records_gate_decision_event(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    with session_scope(home) as session:
        memory = write_memory_item(
            session,
            home,
            content="Project stable preference",
            scope="project",
            source="manual",
            confidence="0.4",
            tags=["preference"],
        )
        session.flush()
        event = session.execute(select(EventLog).where(EventLog.event_type == "memory.write_gate_decided")).scalar_one()
        payload = json.loads(event.payload_json)

    assert payload["decision"] == "allow"
    assert payload["reason_code"] == "memory_write_allowed"
    assert payload["memory_id"] == memory.id
    assert payload["scope"] == "project"
    assert payload["source"] == "manual"


def _memory(memory_id: str, *, source: str, confidence: str, scope: str = "project") -> MemoryItem:
    return MemoryItem(
        id=memory_id,
        scope=scope,
        content=f"{memory_id} content",
        source=source,
        confidence=confidence,
        ttl=None,
        tags_json="[]",
        status="active",
    )
