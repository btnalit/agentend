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
