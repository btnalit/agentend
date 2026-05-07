from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentend.core.memory_store import remove_memory_fts, sync_memory_fts
from agentend.core.memory_relation import (
    MemoryRelationClassifier,
    MemoryRelationDecision,
    has_direct_evidence,
)
from agentend.db.models import AgentIteration, AgentRun, MemoryCandidate, MemoryItem, MemoryLink, Run


@dataclass(frozen=True)
class ConsolidationResult:
    candidate_count: int
    created_count: int
    merged_count: int
    skipped_count: int
    superseded_count: int
    conflict_count: int
    reinforced_count: int
    needs_review_count: int
    conflict_candidate_count: int
    memory_ids: list[str]


def extract_memory_candidates(
    session: Session,
    *,
    agent_run_id: str | None = None,
    run_id: str | None = None,
) -> list[MemoryCandidate]:
    existing = _candidate_query(session, agent_run_id=agent_run_id, run_id=run_id)
    if not agent_run_id and (existing or not run_id):
        return list(existing)
    if run_id and existing:
        return list(existing)

    candidates: list[MemoryCandidate] = []
    if agent_run_id:
        agent_run = session.get(AgentRun, agent_run_id)
        if agent_run is None:
            raise ValueError(f"Unknown agent run: {agent_run_id}")
        desired_type = "successful_procedure" if agent_run.status == "completed" else "failure_lesson"
        if any(candidate.type == desired_type for candidate in existing):
            return list(existing)
        candidates.extend(_candidates_from_agent_run(session, agent_run))
    elif run_id:
        run = session.get(Run, run_id)
        if run is None:
            raise ValueError(f"Unknown run: {run_id}")
        candidates.extend(_candidates_from_workflow_run(run))

    for candidate in candidates:
        session.add(candidate)
    return list(existing) + candidates


def consolidate_memory_candidates(
    session: Session,
    *,
    agent_run_id: str | None = None,
    run_id: str | None = None,
    auto_relations: bool = True,
    home: Path | None = None,
) -> ConsolidationResult:
    candidates = extract_memory_candidates(session, agent_run_id=agent_run_id, run_id=run_id)
    classifier = MemoryRelationClassifier(home)
    created = 0
    merged = 0
    skipped = 0
    superseded = 0
    conflicts = 0
    reinforced = 0
    needs_review = 0
    conflict_candidates = 0
    memory_ids: list[str] = []
    for candidate in candidates:
        if candidate.status in {
            "created",
            "merged",
            "skipped",
            "reinforced",
            "conflict",
            "needs_review",
            "conflict_candidate",
            "superseded",
        } and candidate.memory_id:
            memory_ids.append(candidate.memory_id)
            skipped += 1
            continue
        tags = _json_list(candidate.tags_json)
        superseded_memory_ids = _tagged_memory_ids(tags, "supersedes:")
        conflict_memory_ids = _tagged_memory_ids(tags, "conflicts:")
        if superseded_memory_ids:
            old_memory = session.get(MemoryItem, superseded_memory_ids[0])
            if old_memory is None:
                candidate.status = "skipped"
                candidate.decision_reason = f"superseded memory not found: {superseded_memory_ids[0]}"
                skipped += 1
                continue
            memory = _supersede_memory(session, candidate, old_memory, reason=f"explicitly supersedes memory {old_memory.id}")
            created += 1
            superseded += 1
            memory_ids.append(memory.id)
            continue
        if conflict_memory_ids:
            old_memory = session.get(MemoryItem, conflict_memory_ids[0])
            candidate.status = "conflict"
            candidate.memory_id = old_memory.id if old_memory is not None else None
            candidate.decision_reason = (
                f"conflicts with active memory {old_memory.id}; not promoted"
                if old_memory is not None
                else f"conflicting memory not found: {conflict_memory_ids[0]}"
            )
            if old_memory is not None:
                session.add(
                    MemoryLink(
                        id=str(uuid4()),
                        memory_id=old_memory.id,
                        source_type="memory_candidate",
                        source_id=candidate.id,
                        relation="conflicts_with",
                    )
                )
            conflicts += 1
            continue
        if auto_relations:
            decision = classifier.classify(session, candidate)
            handled = _apply_auto_relation(session, candidate, decision)
            if handled is not None:
                status, memory_id = handled
                if memory_id:
                    memory_ids.append(memory_id)
                if status == "superseded":
                    created += 1
                    superseded += 1
                elif status == "reinforced":
                    reinforced += 1
                elif status == "needs_review":
                    needs_review += 1
                elif status == "conflict_candidate":
                    conflict_candidates += 1
                continue
        if candidate.scope in {"project", "user"} and _confidence(candidate.confidence) < 0.7:
            candidate.status = "skipped"
            candidate.decision_reason = "low confidence candidate is not promoted to long-term memory"
            skipped += 1
            continue
        existing = _memory_by_merge_key(session, candidate.merge_key)
        if existing is None:
            memory = _create_memory_from_candidate(session, candidate)
            candidate.status = "created"
            candidate.memory_id = memory.id
            candidate.decision_reason = "created new memory from candidate"
            session.add(
                MemoryLink(
                    id=str(uuid4()),
                    memory_id=memory.id,
                    source_type="memory_candidate",
                    source_id=candidate.id,
                    relation="created_from",
                )
            )
            created += 1
            memory_ids.append(memory.id)
        else:
            compatible = _content_compatible(existing.content, candidate.content)
            existing.content = _merge_content(existing.content, candidate.content)
            existing.confidence = str(max(_confidence(existing.confidence), _confidence(candidate.confidence)))
            existing.tags_json = json.dumps(sorted(set(_json_list(existing.tags_json) + _merged_tags(candidate))), ensure_ascii=False)
            if candidate.evidence_artifact_id and not existing.evidence_artifact_id:
                existing.evidence_artifact_id = candidate.evidence_artifact_id
            sync_memory_fts(session, existing)
            candidate.status = "reinforced" if compatible else "merged"
            candidate.memory_id = existing.id
            candidate.decision_reason = (
                "reinforced existing memory with compatible evidence"
                if compatible
                else "merged into existing memory with same merge_key"
            )
            session.add(
                MemoryLink(
                    id=str(uuid4()),
                    memory_id=existing.id,
                    source_type="memory_candidate",
                    source_id=candidate.id,
                    relation="reinforced_by" if compatible else "updated_by",
                )
            )
            if compatible:
                reinforced += 1
            else:
                merged += 1
            memory_ids.append(existing.id)
    return ConsolidationResult(
        candidate_count=len(candidates),
        created_count=created,
        merged_count=merged,
        skipped_count=skipped,
        superseded_count=superseded,
        conflict_count=conflicts,
        reinforced_count=reinforced,
        needs_review_count=needs_review,
        conflict_candidate_count=conflict_candidates,
        memory_ids=sorted(set(memory_ids)),
    )


def memory_candidate_to_dict(candidate: MemoryCandidate) -> dict[str, object]:
    return {
        "id": candidate.id,
        "agent_run_id": candidate.agent_run_id,
        "run_id": candidate.run_id,
        "memory_id": candidate.memory_id,
        "type": candidate.type,
        "scope": candidate.scope,
        "content": candidate.content,
        "merge_key": candidate.merge_key,
        "confidence": candidate.confidence,
        "status": candidate.status,
        "decision_reason": candidate.decision_reason,
        "tags": _json_list(candidate.tags_json),
        "evidence_artifact_id": candidate.evidence_artifact_id,
    }


def _apply_auto_relation(
    session: Session,
    candidate: MemoryCandidate,
    decision: MemoryRelationDecision,
) -> tuple[str, str | None] | None:
    if decision.relation == "unrelated":
        return None
    if decision.confidence < 0.65:
        candidate.status = "needs_review"
        candidate.memory_id = decision.target_memory_id
        candidate.decision_reason = f"auto relation needs review: {decision.reason}"
        _add_relation_link(session, decision.target_memory_id, candidate.id, "needs_review_for")
        return candidate.status, candidate.memory_id
    if not decision.target_memory_id:
        return None
    target = session.get(MemoryItem, decision.target_memory_id)
    if target is None:
        candidate.status = "needs_review"
        candidate.decision_reason = f"auto relation target not found: {decision.target_memory_id}"
        return candidate.status, None
    if decision.relation == "reinforces":
        _merge_into_existing(session, target, candidate, relation="reinforced_by", status="reinforced")
        candidate.decision_reason = f"auto relation reinforced memory {target.id}: {decision.reason}"
        _add_relation_link(session, target.id, candidate.id, "relation_decision")
        return candidate.status, target.id
    if decision.relation == "updates" and decision.confidence >= 0.85:
        replacement = _supersede_memory(session, candidate, target, reason=f"auto relation update: {decision.reason}")
        _add_relation_link(session, replacement.id, candidate.id, "relation_decision")
        return candidate.status, replacement.id
    if decision.relation == "updates":
        candidate.status = "needs_review"
        candidate.memory_id = target.id
        candidate.decision_reason = f"auto relation update needs review for memory {target.id}: {decision.reason}"
        _add_relation_link(session, target.id, candidate.id, "needs_review_for")
        _add_relation_link(session, target.id, candidate.id, "relation_decision")
        return candidate.status, target.id
    if decision.relation == "conflicts":
        if decision.confidence >= 0.85 and has_direct_evidence(candidate):
            replacement = _supersede_memory(session, candidate, target, reason=f"auto relation conflict with direct evidence: {decision.reason}")
            _add_relation_link(session, replacement.id, candidate.id, "relation_decision")
            return candidate.status, replacement.id
        candidate.status = "conflict_candidate"
        candidate.memory_id = target.id
        candidate.decision_reason = f"auto relation conflict candidate for memory {target.id}: {decision.reason}"
        _add_relation_link(session, target.id, candidate.id, "conflicts_with")
        _add_relation_link(session, target.id, candidate.id, "relation_decision")
        return candidate.status, target.id
    return None


def _supersede_memory(session: Session, candidate: MemoryCandidate, old_memory: MemoryItem, *, reason: str) -> MemoryItem:
    memory = _create_memory_from_candidate(session, candidate)
    old_memory.status = "superseded"
    remove_memory_fts(session, old_memory.id)
    candidate.status = "superseded"
    candidate.memory_id = memory.id
    candidate.decision_reason = f"created replacement and superseded memory {old_memory.id}; {reason}"
    session.add(
        MemoryLink(
            id=str(uuid4()),
            memory_id=memory.id,
            source_type="memory_item",
            source_id=old_memory.id,
            relation="supersedes",
        )
    )
    session.add(
        MemoryLink(
            id=str(uuid4()),
            memory_id=old_memory.id,
            source_type="memory_item",
            source_id=memory.id,
            relation="superseded_by",
        )
    )
    return memory


def _merge_into_existing(
    session: Session,
    existing: MemoryItem,
    candidate: MemoryCandidate,
    *,
    relation: str,
    status: str,
) -> None:
    existing.content = _merge_content(existing.content, candidate.content)
    existing.confidence = str(max(_confidence(existing.confidence), _confidence(candidate.confidence)))
    existing.tags_json = json.dumps(sorted(set(_json_list(existing.tags_json) + _merged_tags(candidate))), ensure_ascii=False)
    if candidate.evidence_artifact_id and not existing.evidence_artifact_id:
        existing.evidence_artifact_id = candidate.evidence_artifact_id
    sync_memory_fts(session, existing)
    candidate.status = status
    candidate.memory_id = existing.id
    session.add(
        MemoryLink(
            id=str(uuid4()),
            memory_id=existing.id,
            source_type="memory_candidate",
            source_id=candidate.id,
            relation=relation,
        )
    )


def _add_relation_link(
    session: Session,
    memory_id: str | None,
    source_id: str,
    relation: str,
) -> None:
    if not memory_id:
        return
    session.add(
        MemoryLink(
            id=str(uuid4()),
            memory_id=memory_id,
            source_type="memory_candidate",
            source_id=source_id,
            relation=relation,
        )
    )


def _candidates_from_agent_run(session: Session, agent_run: AgentRun) -> list[MemoryCandidate]:
    iterations = (
        session.execute(
            select(AgentIteration)
            .where(AgentIteration.agent_run_id == agent_run.id)
            .order_by(AgentIteration.iteration_index)
        )
        .scalars()
        .all()
    )
    final_result = _json_dict(agent_run.final_result_json)
    content = _candidate_content(agent_run, final_result, iterations)
    candidate_type = "successful_procedure" if agent_run.status == "completed" else "failure_lesson"
    scope = "project" if agent_run.status == "completed" else "episode"
    confidence = "0.85" if agent_run.status == "completed" else "0.65"
    merge_key = f"{scope}:{candidate_type}:{_slug(agent_run.goal)}"
    tags = [
        "agent-run",
        f"merge:{merge_key}",
        f"agent_run:{agent_run.id}",
        f"type:{candidate_type}",
    ]
    linked_run_id = ""
    evidence_artifact_id = None
    for iteration in iterations:
        if iteration.linked_run_id and not linked_run_id:
            linked_run_id = iteration.linked_run_id
        if iteration.progress_artifact_id and evidence_artifact_id is None:
            evidence_artifact_id = iteration.progress_artifact_id
    return [
        MemoryCandidate(
            id=str(uuid4()),
            agent_run_id=agent_run.id,
            run_id=linked_run_id or None,
            type=candidate_type,
            scope=scope,
            content=content,
            merge_key=merge_key,
            confidence=confidence,
            status="pending",
            tags_json=json.dumps(tags, ensure_ascii=False, sort_keys=True),
            evidence_artifact_id=evidence_artifact_id,
        )
    ]


def _candidates_from_workflow_run(run: Run) -> list[MemoryCandidate]:
    result = _json_dict(run.result_json)
    status = "successful_procedure" if run.status == "completed" else "failure_lesson"
    scope = "project" if run.status == "completed" else "episode"
    content = f"Workflow {run.workflow_id or 'unknown'} finished with status {run.status}: {_preview(result)}"
    merge_key = f"{scope}:{status}:{_slug(run.workflow_id or run.id)}"
    return [
        MemoryCandidate(
            id=str(uuid4()),
            run_id=run.id,
            type=status,
            scope=scope,
            content=content,
            merge_key=merge_key,
            confidence="0.75" if run.status == "completed" else "0.6",
            status="pending",
            tags_json=json.dumps(["workflow-run", f"merge:{merge_key}", f"run:{run.id}", f"type:{status}"], sort_keys=True),
        )
    ]


def _candidate_query(
    session: Session,
    *,
    agent_run_id: str | None = None,
    run_id: str | None = None,
) -> list[MemoryCandidate]:
    stmt = select(MemoryCandidate).order_by(MemoryCandidate.created_at)
    if agent_run_id:
        stmt = stmt.where(MemoryCandidate.agent_run_id == agent_run_id)
    if run_id:
        stmt = stmt.where(MemoryCandidate.run_id == run_id)
    return list(session.execute(stmt).scalars().all())


def _memory_by_merge_key(session: Session, merge_key: str) -> MemoryItem | None:
    tag = f"merge:{merge_key}"
    rows = session.execute(select(MemoryItem).where(MemoryItem.status == "active")).scalars().all()
    for row in rows:
        if tag in _json_list(row.tags_json):
            return row
    return None


def _create_memory_from_candidate(session: Session, candidate: MemoryCandidate) -> MemoryItem:
    memory = MemoryItem(
        id=str(uuid4()),
        scope=candidate.scope,
        content=candidate.content,
        source="agent_consolidator",
        confidence=candidate.confidence,
        ttl=None,
        tags_json=json.dumps(_merged_tags(candidate), ensure_ascii=False, sort_keys=True),
        status="active",
        created_by_run_id=candidate.agent_run_id or candidate.run_id,
        evidence_artifact_id=candidate.evidence_artifact_id,
    )
    session.add(memory)
    sync_memory_fts(session, memory)
    return memory


def _tagged_memory_ids(tags: list[str], prefix: str) -> list[str]:
    return [tag[len(prefix) :] for tag in tags if tag.startswith(prefix) and tag[len(prefix) :]]


def _candidate_content(agent_run: AgentRun, final_result: dict[str, object], iterations: list[AgentIteration]) -> str:
    actions: list[str] = []
    for iteration in iterations:
        action = _json_dict(iteration.selected_action_json)
        if action:
            actions.append(f"{action.get('type')}:{action.get('name')}")
    content = str(final_result.get("content") or final_result.get("error") or agent_run.stop_reason or "")
    prefix = "Successful procedure" if agent_run.status == "completed" else "Failure lesson"
    return (
        f"{prefix} for goal '{agent_run.goal}': "
        f"actions={', '.join(actions) or 'none'}; result={content[:500]}"
    )


def _merged_tags(candidate: MemoryCandidate) -> list[str]:
    tags = _json_list(candidate.tags_json)
    merge_tag = f"merge:{candidate.merge_key}"
    if merge_tag not in tags:
        tags.append(merge_tag)
    return sorted(set(tags))


def _merge_content(existing: str, new: str) -> str:
    if new in existing:
        return existing
    if existing in new:
        return new
    return f"{existing}\n\nUpdated evidence: {new}"


def _content_compatible(existing: str, new: str) -> bool:
    normalized_existing = _normalize_content(existing)
    normalized_new = _normalize_content(new)
    return normalized_existing == normalized_new or normalized_new in normalized_existing or normalized_existing in normalized_new


def _normalize_content(value: str) -> str:
    return " ".join(value.lower().split())


def _slug(value: str) -> str:
    ascii_terms = re.findall(r"[A-Za-z0-9_]+", value.lower())
    if ascii_terms:
        return "-".join(ascii_terms[:8])[:96]
    return str(abs(hash(value)))[:24]


def _preview(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)[:500]


def _json_dict(raw_json: str) -> dict[str, object]:
    try:
        payload = json.loads(raw_json or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


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
