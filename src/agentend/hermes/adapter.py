"""Fail-closed adapter bridging agentend AgentRun → Hermes EventEnvelope."""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Inject Hermes source directory so plugins.memory.memory_os.* imports work in-process.
_hermes_path = Path(__file__).parent.parent.parent.parent / "Hermes-Memory-OS-main"
if str(_hermes_path) not in sys.path:
    sys.path.insert(0, str(_hermes_path))

try:
    from plugins.memory.memory_os.roots import MemoryOSRoots
    from plugins.memory.memory_os.store import MemoryOSStore
    from plugins.memory.memory_os.schema import EventEnvelope, EVENT_SCHEMA_VERSION
    from plugins.memory.memory_os.ids import new_event_id
    _HERMES_AVAILABLE = True
except ImportError:
    _HERMES_AVAILABLE = False
    MemoryOSRoots = None  # type: ignore[assignment]
    MemoryOSStore = None  # type: ignore[assignment]
    EventEnvelope = None  # type: ignore[assignment]
    EVENT_SCHEMA_VERSION = "memory-os.event.v0"

    def new_event_id() -> str:  # type: ignore[misc]
        from uuid import uuid4
        return str(uuid4())


def retain_agent_run(agent_run: Any, iterations: list[Any], hermes_home: Path) -> bool:
    """Write a completed/failed AgentRun as an EventEnvelope to the Hermes store.

    Returns True on success, False on any failure (degrade not crash).
    body_policy is always "summary_only" — raw message bodies are never retained.
    """
    try:
        if MemoryOSRoots is None or MemoryOSStore is None or EventEnvelope is None:
            logger.warning(
                "Hermes not available, skipping retain for run %s",
                getattr(agent_run, "id", "?"),
            )
            return False

        # MemoryOSRoots is a frozen dataclass; use the factory classmethod.
        roots = MemoryOSRoots.from_hermes_home(hermes_home, profile="agentend")
        store = MemoryOSStore(roots)
        store.initialize()

        kind = (
            "agent_run_completed"
            if getattr(agent_run, "status", "") == "completed"
            else "agent_run_failed"
        )
        summary = _summarize_run(agent_run, iterations)

        event = EventEnvelope(
            schema_version=EVENT_SCHEMA_VERSION,
            id=new_event_id(),
            ts=datetime.now(timezone.utc).isoformat(),
            profile="agentend",
            source="agentend.agent_run",
            kind=kind,
            summary=summary,
            sensitivity="private",
            body_policy="summary_only",
            promotion_state="raw",
            safe_ref={"agent_run_id": str(agent_run.id)},
            tags=["agentend"],
        )
        store.append_event(event)
        logger.debug("Hermes retain succeeded for run %s", getattr(agent_run, "id", "?"))
        return True

    except Exception:
        logger.warning(
            "Hermes retain failed for run %s",
            getattr(agent_run, "id", "?"),
            exc_info=True,
        )
        return False


def _summarize_run(agent_run: Any, iterations: list[Any]) -> str:
    """Build a short summary string — never includes raw message bodies."""
    goal = str(getattr(agent_run, "goal", ""))[:200]
    status = str(getattr(agent_run, "status", ""))
    iter_count = len(iterations)
    return f"[{status}] goal={goal!r} iterations={iter_count}"
