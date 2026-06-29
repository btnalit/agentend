"""负向断言：守 hermes-integration-spec §5 接缝纪律。"""


def test_recall_untrusted_trust_level() -> None:
    from agentend.core.context_runtime import ContextItem
    item = ContextItem(item_type="memory", source="hermes_recall", summary="x")
    assert item.trust_level == "external_recall"
    assert item.can_override_policy is False
    assert set(item.allowed_use).issubset({"answer_context", "evidence"})


def test_recall_untrusted_no_system_instruction() -> None:
    from agentend.core.context_runtime import ContextItem
    item = ContextItem(item_type="memory", source="hermes_recall", summary="x")
    # system_instruction 是 allowed_use 中不允许的值
    assert "system_instruction" not in item.allowed_use
    assert "override_policy" not in item.allowed_use


def test_exec_authority_hermes_adapter_has_no_action_policy() -> None:
    """retain_agent_run 不得调用 ActionPolicy。"""
    import inspect
    from agentend.hermes import adapter
    source = inspect.getsource(adapter)
    assert "ActionPolicy" not in source
    assert "decide_action" not in source


def test_exec_authority_hermes_adapter_has_no_tool_registry() -> None:
    """retain_agent_run 不得调用 ToolRegistry（无执行权）。"""
    import inspect
    from agentend.hermes import adapter
    source = inspect.getsource(adapter)
    assert "ToolRegistry" not in source
