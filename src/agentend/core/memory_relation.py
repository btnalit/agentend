from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentend.config import load_config
from agentend.core.llm_router import LLMRouter
from agentend.db.models import MemoryCandidate, MemoryItem


CONTROL_TAG_PREFIXES = (
    "agent_run:",
    "conflicts:",
    "evidence:",
    "merge:",
    "run:",
    "supersedes:",
    "type:",
)

RELATION_REINFORCES = "reinforces"
RELATION_UPDATES = "updates"
RELATION_CONFLICTS = "conflicts"
RELATION_UNRELATED = "unrelated"


@dataclass(frozen=True)
class MemoryRelationDecision:
    relation: str
    target_memory_id: str | None
    confidence: float
    replacement_content: str | None
    reason: str
    evidence_refs: list[str]


class MemoryRelationClassifier:
    """Classify one candidate against a small metadata-derived memory shortlist."""

    def __init__(
        self,
        home: Path | None = None,
        *,
        llm_complete: Callable[[str], str] | None = None,
    ) -> None:
        self.home = home
        self.llm_complete = llm_complete

    def shortlist(self, session: Session, candidate: MemoryCandidate, *, limit: int = 5) -> list[MemoryItem]:
        candidate_tags = _json_list(candidate.tags_json)
        candidate_subjects = _subject_tags(candidate_tags)
        candidate_plain_tags = _plain_tags(candidate_tags)
        merge_tag = f"merge:{candidate.merge_key}"
        scored: list[tuple[int, MemoryItem]] = []
        active_memories = (
            session.execute(select(MemoryItem).where(MemoryItem.status == "active").where(MemoryItem.scope == candidate.scope))
            .scalars()
            .all()
        )
        for memory in active_memories:
            tags = _json_list(memory.tags_json)
            score = 0
            if merge_tag in tags:
                score += 100
            if candidate_subjects & _subject_tags(tags):
                score += 60
            if f"type:{candidate.type}" in tags:
                score += 20
            score += min(len(candidate_plain_tags & _plain_tags(tags)), 5) * 5
            if score > 0:
                scored.append((score, memory))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [memory for _, memory in scored[:limit]]

    def classify(self, session: Session, candidate: MemoryCandidate) -> MemoryRelationDecision:
        shortlist = self.shortlist(session, candidate)
        if not shortlist:
            return MemoryRelationDecision(
                relation=RELATION_UNRELATED,
                target_memory_id=None,
                confidence=_confidence(candidate.confidence),
                replacement_content=None,
                reason="no active memory matched candidate metadata",
                evidence_refs=_evidence_refs(candidate),
            )
        llm_decision = self._classify_with_structured_llm(candidate, shortlist)
        if llm_decision is not None:
            return llm_decision
        target = shortlist[0]
        confidence = _confidence(candidate.confidence)
        if _content_compatible(target.content, candidate.content):
            return MemoryRelationDecision(
                relation=RELATION_REINFORCES,
                target_memory_id=target.id,
                confidence=confidence,
                replacement_content=None,
                reason="candidate content reinforces the shortlisted memory",
                evidence_refs=_evidence_refs(candidate),
            )
        relation = RELATION_UPDATES if _has_direct_evidence(candidate) and confidence >= 0.85 else RELATION_CONFLICTS
        return MemoryRelationDecision(
            relation=relation,
            target_memory_id=target.id,
            confidence=confidence,
            replacement_content=candidate.content if relation == RELATION_UPDATES else None,
            reason=(
                "candidate has direct evidence and updates the shortlisted memory"
                if relation == RELATION_UPDATES
                else "candidate differs from the shortlisted memory and needs a safe conflict gate"
            ),
            evidence_refs=_evidence_refs(candidate),
        )

    def _classify_with_structured_llm(
        self,
        candidate: MemoryCandidate,
        shortlist: list[MemoryItem],
    ) -> MemoryRelationDecision | None:
        complete = self.llm_complete or self._configured_llm_complete()
        if complete is None:
            return None
        prompt = _relation_prompt(candidate, shortlist)
        try:
            payload = _json_object(complete(prompt))
        except Exception:
            return None
        decision = _decision_from_payload(payload)
        if decision is None:
            return None
        shortlist_ids = {memory.id for memory in shortlist}
        if decision.target_memory_id and decision.target_memory_id not in shortlist_ids:
            return None
        return decision

    def _configured_llm_complete(self) -> Callable[[str], str] | None:
        if self.home is None:
            return None
        config = load_config(self.home)
        if config.llm.provider == "fake":
            return None
        return LLMRouter(config).complete


def decision_to_dict(decision: MemoryRelationDecision) -> dict[str, object]:
    return {
        "relation": decision.relation,
        "target_memory_id": decision.target_memory_id,
        "confidence": decision.confidence,
        "replacement_content": decision.replacement_content,
        "reason": decision.reason,
        "evidence_refs": decision.evidence_refs,
    }


def _relation_prompt(candidate: MemoryCandidate, shortlist: list[MemoryItem]) -> str:
    payload = {
        "task": "Classify the relationship between one memory candidate and the shortlisted active memories.",
        "allowed_relations": [RELATION_REINFORCES, RELATION_UPDATES, RELATION_CONFLICTS, RELATION_UNRELATED],
        "output_schema": {
            "relation": "reinforces | updates | conflicts | unrelated",
            "target_memory_id": "string | null",
            "confidence": "number from 0 to 1",
            "replacement_content": "string | null",
            "reason": "short explanation",
            "evidence_refs": ["string"],
        },
        "candidate": {
            "id": candidate.id,
            "type": candidate.type,
            "scope": candidate.scope,
            "content": candidate.content,
            "merge_key": candidate.merge_key,
            "confidence": candidate.confidence,
            "tags": _json_list(candidate.tags_json),
            "evidence_artifact_id": candidate.evidence_artifact_id,
        },
        "active_memories": [
            {
                "id": memory.id,
                "scope": memory.scope,
                "content": memory.content,
                "confidence": memory.confidence,
                "tags": _json_list(memory.tags_json),
            }
            for memory in shortlist
        ],
    }
    return (
        "Return only one JSON object matching output_schema. "
        "Do not write prose outside JSON.\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True)
    )


def _decision_from_payload(payload: dict[str, object]) -> MemoryRelationDecision | None:
    relation = str(payload.get("relation") or "")
    if relation not in {RELATION_REINFORCES, RELATION_UPDATES, RELATION_CONFLICTS, RELATION_UNRELATED}:
        return None
    target_raw = payload.get("target_memory_id")
    target_memory_id = str(target_raw) if target_raw not in {None, ""} else None
    try:
        confidence = float(payload.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    replacement_raw = payload.get("replacement_content")
    evidence_raw = payload.get("evidence_refs")
    return MemoryRelationDecision(
        relation=relation,
        target_memory_id=target_memory_id,
        confidence=max(0.0, min(confidence, 1.0)),
        replacement_content=str(replacement_raw) if replacement_raw not in {None, ""} else None,
        reason=str(payload.get("reason") or "structured relation decision"),
        evidence_refs=[str(item) for item in evidence_raw] if isinstance(evidence_raw, list) else [],
    )


def _json_object(content: str) -> dict[str, object]:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").strip()
        if stripped.startswith("json"):
            stripped = stripped[4:].strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end >= start:
        stripped = stripped[start : end + 1]
    payload = json.loads(stripped)
    if not isinstance(payload, dict):
        raise ValueError("structured memory relation output must be a JSON object")
    return payload


def has_direct_evidence(candidate: MemoryCandidate) -> bool:
    return _has_direct_evidence(candidate)


def _has_direct_evidence(candidate: MemoryCandidate) -> bool:
    tags = _json_list(candidate.tags_json)
    return bool(candidate.evidence_artifact_id) or any(
        tag in {"evidence:tool", "evidence:test", "evidence:file", "evidence:artifact"} for tag in tags
    )


def _evidence_refs(candidate: MemoryCandidate) -> list[str]:
    refs: list[str] = []
    if candidate.agent_run_id:
        refs.append(f"agent_run:{candidate.agent_run_id}")
    if candidate.run_id:
        refs.append(f"run:{candidate.run_id}")
    if candidate.evidence_artifact_id:
        refs.append(f"artifact:{candidate.evidence_artifact_id}")
    return refs


def _content_compatible(existing: str, new: str) -> bool:
    normalized_existing = _normalize_content(existing)
    normalized_new = _normalize_content(new)
    return normalized_existing == normalized_new or normalized_new in normalized_existing or normalized_existing in normalized_new


def _normalize_content(value: str) -> str:
    return " ".join(value.lower().split())


def _subject_tags(tags: list[str]) -> set[str]:
    return {tag for tag in tags if tag.startswith("subject:") and tag[len("subject:") :]}


def _plain_tags(tags: list[str]) -> set[str]:
    return {tag for tag in tags if tag and not tag.startswith(CONTROL_TAG_PREFIXES)}


def _json_list(raw_json: str) -> list[str]:
    try:
        payload = json.loads(raw_json or "[]")
    except json.JSONDecodeError:
        return []
    return [str(item) for item in payload] if isinstance(payload, list) else []


def _confidence(raw: str) -> float:
    try:
        return float(raw)
    except ValueError:
        return 0.0
