from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


LONG_TERM_SCOPES = {"project", "user"}
TRUSTED_SOURCES = {"manual", "agent_consolidator"}
UNTRUSTED_ALLOWED_SCOPES = {"session", "task", "episode"}


@dataclass(frozen=True)
class MemoryGateDecision:
    decision: str
    reason_code: str
    scope: str
    source: str
    confidence: float
    trust_level: str
    allowed_use: tuple[str, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "decision": self.decision,
            "reason_code": self.reason_code,
            "scope": self.scope,
            "source": self.source,
            "confidence": self.confidence,
            "trust_level": self.trust_level,
            "allowed_use": list(self.allowed_use),
        }


def decide_memory_write(*, scope: str, source: str, confidence: str | float = "1.0") -> MemoryGateDecision:
    parsed_confidence = _parse_confidence(confidence)
    trust_level = _trust_level_for_source(source)
    if scope in LONG_TERM_SCOPES and source not in TRUSTED_SOURCES:
        return MemoryGateDecision(
            decision="reject",
            reason_code="memory_write_untrusted_long_term",
            scope=scope,
            source=source,
            confidence=parsed_confidence,
            trust_level=trust_level,
            allowed_use=("not_instruction",),
        )
    if source not in TRUSTED_SOURCES and scope not in UNTRUSTED_ALLOWED_SCOPES:
        return MemoryGateDecision(
            decision="reject",
            reason_code="memory_write_untrusted_scope",
            scope=scope,
            source=source,
            confidence=parsed_confidence,
            trust_level=trust_level,
            allowed_use=("not_instruction",),
        )
    if source not in TRUSTED_SOURCES:
        return MemoryGateDecision(
            decision="allow",
            reason_code="memory_write_short_term_untrusted_allowed",
            scope=scope,
            source=source,
            confidence=parsed_confidence,
            trust_level=trust_level,
            allowed_use=("answer_context", "not_instruction"),
        )
    return MemoryGateDecision(
        decision="allow",
        reason_code="memory_write_allowed",
        scope=scope,
        source=source,
        confidence=parsed_confidence,
        trust_level=trust_level,
        allowed_use=("answer_context",),
    )


def decide_memory_read(
    memory: Any,
    *,
    scope: str | None,
    min_confidence: float,
    trusted_sources: set[str],
) -> MemoryGateDecision:
    memory_scope = str(memory.scope)
    source = str(memory.source)
    parsed_confidence = _parse_confidence(memory.confidence)
    trust_level = _trust_level_for_source(source)
    if memory.status != "active":
        return _drop("memory_inactive", memory_scope, source, parsed_confidence, trust_level)
    if scope and memory_scope != scope:
        return _drop("memory_scope_not_allowed", memory_scope, source, parsed_confidence, trust_level)
    if _is_expired(memory.ttl):
        return _drop("memory_expired", memory_scope, source, parsed_confidence, trust_level)
    if trusted_sources and source not in trusted_sources:
        return _drop("memory_untrusted_source", memory_scope, source, parsed_confidence, trust_level)
    if parsed_confidence < min_confidence:
        return _drop("memory_low_confidence", memory_scope, source, parsed_confidence, trust_level)
    if source == "manual":
        return MemoryGateDecision(
            decision="strong",
            reason_code="memory_read_strong",
            scope=memory_scope,
            source=source,
            confidence=parsed_confidence,
            trust_level=trust_level,
            allowed_use=("answer_context",),
        )
    return MemoryGateDecision(
        decision="weak",
        reason_code="memory_read_weak",
        scope=memory_scope,
        source=source,
        confidence=parsed_confidence,
        trust_level=trust_level,
        allowed_use=("answer_context",),
    )


def _drop(reason_code: str, scope: str, source: str, confidence: float, trust_level: str) -> MemoryGateDecision:
    return MemoryGateDecision(
        decision="drop",
        reason_code=reason_code,
        scope=scope,
        source=source,
        confidence=confidence,
        trust_level=trust_level,
        allowed_use=("not_instruction",),
    )


def _trust_level_for_source(source: str) -> str:
    if source == "manual":
        return "trusted"
    if source == "agent_consolidator":
        return "generated"
    return "external_untrusted"


def _is_expired(ttl: str | None) -> bool:
    if not ttl:
        return False
    try:
        expires_at = datetime.fromisoformat(ttl)
    except ValueError:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= datetime.now(timezone.utc)


def _parse_confidence(value: str | float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
