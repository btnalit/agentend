"""Tests for agentend.hermes.adapter — Hermes retain adapter (H2+H4)."""
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4


def _make_mock_run(run_id: str | None = None):
    run = MagicMock()
    run.id = run_id or str(uuid4())
    run.goal = "test goal"
    run.status = "completed"
    run.channel = "task"
    run.external_user_id = "local"
    run.created_at = datetime.now(timezone.utc)
    run.completed_at = datetime.now(timezone.utc)
    return run


def test_retain_agent_run_writes_event_to_hermes(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()

    mock_store = MagicMock()

    with patch("agentend.hermes.adapter.MemoryOSRoots"), \
         patch("agentend.hermes.adapter.MemoryOSStore", return_value=mock_store):
        from agentend.hermes.adapter import retain_agent_run
        result = retain_agent_run(_make_mock_run(), [], hermes_home)

    assert result is True
    mock_store.initialize.assert_called_once()
    mock_store.append_event.assert_called_once()
    event = mock_store.append_event.call_args[0][0]
    assert event.schema_version == "memory-os.event.v0"
    assert event.body_policy == "summary_only"
    # NOTE: can_override_policy is NOT an EventEnvelope field — assertion removed.
    assert event.profile == "agentend"


def test_retain_agent_run_degrades_gracefully_on_import_error(tmp_path: Path) -> None:
    """retain_agent_run must return False (not crash) when Hermes is unavailable.

    We patch MemoryOSRoots.from_hermes_home (the factory used by the adapter)
    to raise ImportError, simulating a missing Hermes installation.  No module
    reload is needed: the adapter's broad try/except catches the error.
    """
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()

    with patch("agentend.hermes.adapter.MemoryOSRoots") as mock_cls:
        mock_cls.from_hermes_home.side_effect = ImportError("no hermes")
        from agentend.hermes.adapter import retain_agent_run
        result = retain_agent_run(_make_mock_run(), [], hermes_home)

    assert result is False  # degrades, does not crash


def test_retain_agent_run_sets_correct_kind_for_failed_run(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    failed_run = _make_mock_run()
    failed_run.status = "failed"

    mock_store = MagicMock()
    with patch("agentend.hermes.adapter.MemoryOSRoots"), \
         patch("agentend.hermes.adapter.MemoryOSStore", return_value=mock_store):
        from agentend.hermes.adapter import retain_agent_run
        retain_agent_run(failed_run, [], hermes_home)

    event = mock_store.append_event.call_args[0][0]
    assert event.kind == "agent_run_failed"
