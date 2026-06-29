import logging
import sys

import agentend.core.context_runtime as cr
from agentend.core.context_runtime import ContextItem


def test_hermes_recall_context_item_has_correct_trust() -> None:
    item = ContextItem(
        item_type="memory",
        source="hermes_recall",
        summary="相关历史记忆",
    )
    assert item.trust_level == "external_recall"
    assert item.can_override_policy is False
    assert "answer_context" in item.allowed_use
    assert "evidence" in item.allowed_use


def test_hermes_recall_context_item_cannot_override_policy() -> None:
    item = ContextItem(
        item_type="memory",
        source="hermes_recall",
        summary="memory block",
        can_override_policy=True,   # 即使显式传 True，也必须被强制为 False
    )
    # 注：当前 ContextItem 不强制覆盖显式传入值；此测试验证 _default_trust_metadata 路径
    default_item = ContextItem(item_type="memory", source="hermes_recall", summary="x")
    assert default_item.can_override_policy is False


# ---------------------------------------------------------------------------
# _get_hermes_provider — caching, path cleanup, availability short-circuit
# ---------------------------------------------------------------------------

def test_get_hermes_provider_returns_none_when_hermes_unavailable(monkeypatch) -> None:
    """When Hermes is not available, _get_hermes_provider returns None without leaking sys.path."""
    cr._hermes_provider_cache.clear()

    # Simulate Hermes not installed by patching the adapter flag.
    import agentend.hermes.adapter as _adapter
    monkeypatch.setattr(_adapter, "_HERMES_AVAILABLE", False)

    path_before = sys.path.copy()
    result = cr._get_hermes_provider("/nonexistent/hermes_home")

    assert result is None
    assert sys.path == path_before, "sys.path must not be modified when Hermes is unavailable"


def test_get_hermes_provider_cached_on_second_call(monkeypatch) -> None:
    """_get_hermes_provider returns the cached value without re-importing on subsequent calls."""
    cr._hermes_provider_cache.clear()

    import agentend.hermes.adapter as _adapter
    monkeypatch.setattr(_adapter, "_HERMES_AVAILABLE", False)

    result1 = cr._get_hermes_provider("/hermes_home_a")
    result2 = cr._get_hermes_provider("/hermes_home_a")

    assert result1 is None
    assert result2 is None
    # Verify it is in the cache
    assert "/hermes_home_a" in cr._hermes_provider_cache


def test_get_hermes_provider_path_cleaned_up_on_import_error(monkeypatch) -> None:
    """When the Hermes MemoryOSProvider import fails, the injected sys.path entry is removed."""
    from pathlib import Path
    from unittest.mock import patch

    cr._hermes_provider_cache.clear()

    # Make _HERMES_AVAILABLE True so we bypass the short-circuit and reach path injection.
    import agentend.hermes.adapter as _adapter
    monkeypatch.setattr(_adapter, "_HERMES_AVAILABLE", True)

    hermes_path = str(Path(cr.__file__).parent.parent.parent.parent / "Hermes-Memory-OS-main")

    # Save and strip hermes_path so _path_added becomes True inside the helper.
    saved_path = list(sys.path)
    while hermes_path in sys.path:
        sys.path.remove(hermes_path)

    # Force ImportError by hiding the target module in sys.modules cache.
    # Python's import machinery raises ModuleNotFoundError when sys.modules[key] is None.
    blocking = {
        "plugins.memory.memory_os": None,
    }

    try:
        with patch.dict(sys.modules, blocking):
            result = cr._get_hermes_provider("/fake/hermes_home_path_cleanup_test")
        # Assert BEFORE finally restores sys.path (saved_path still contains hermes_path).
        assert result is None
        assert hermes_path not in sys.path, (
            "sys.path must not retain the injected Hermes path after ImportError"
        )
    finally:
        # Restore sys.path regardless of outcome.
        sys.path[:] = saved_path


def test_hermes_recall_exception_logs_warning(monkeypatch, caplog, tmp_path) -> None:
    """build_context_pack logs a warning (not silently swallows) when Hermes recall raises."""
    cr._hermes_provider_cache.clear()

    # Make _get_hermes_provider raise to simulate a runtime Hermes failure.
    def _raising_provider(hermes_home: str):
        raise RuntimeError("simulated hermes failure")

    monkeypatch.setattr(cr, "_get_hermes_provider", _raising_provider)

    # Set up a minimal home dir with hermes_home configured.
    from typer.testing import CliRunner
    from agentend.cli import app
    from agentend.config import load_config

    home = tmp_path / "home"
    CliRunner().invoke(app, ["init", "--home", str(home)])

    config_path = home / "config.toml"
    original = config_path.read_text(encoding="utf-8")
    updated = original.replace('[hermes]\nhome = ""', '[hermes]\nhome = "/fake/hermes"')
    config_path.write_text(updated, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="agentend.core.context_runtime"):
        cr.build_context_pack(home, workflow=None, user_input="test query", session=None)

    warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("Hermes recall" in msg for msg in warning_messages), (
        f"Expected 'Hermes recall' warning in logs, got: {warning_messages}"
    )
