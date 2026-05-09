from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentend.core.capabilities import refresh_capabilities
from agentend.core.effectiveness import effectiveness_for
from agentend.core.skills import ensure_builtin_skills
from agentend.core.tool_contracts import sync_tool_manifests
from agentend.core.tool_registry import ToolRegistry
from agentend.db.models import CapabilityEffectivenessEvent, Skill, ToolManifest


@dataclass(frozen=True)
class SelectedAction:
    type: str
    name: str
    input_data: dict[str, Any]
    reason: str
    no_tool_reason: str | None = None
    score: float = 0.0
    score_breakdown: dict[str, float] | None = None
    rejected_reasons: list[str] | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SelectionResult:
    selected: SelectedAction
    trace: dict[str, Any]


def select_next_action(
    home: Path,
    session: Session,
    goal: str,
    goal_analysis: dict[str, Any] | None,
    previous_observations: list[dict[str, Any]] | None = None,
) -> SelectedAction:
    return select_next_action_with_trace(home, session, goal, goal_analysis, previous_observations).selected


def select_next_action_with_trace(
    home: Path,
    session: Session,
    goal: str,
    goal_analysis: dict[str, Any] | None,
    previous_observations: list[dict[str, Any]] | None = None,
) -> SelectionResult:
    resolved_home = home.expanduser().resolve()
    ensure_builtin_skills(resolved_home, session)
    sync_tool_manifests(session, ToolRegistry(resolved_home).manifests())
    refresh_capabilities(session)
    analysis = goal_analysis or {}
    intent = _intent_decision(analysis)
    intent_slots = dict(intent.get("slots", {})) if isinstance(intent.get("slots"), dict) else {}
    allowed_tools = {str(item) for item in intent.get("allowed_tools", []) if item}
    allowed_tools_explicit = "allowed_tools" in intent
    allowed_capabilities, allowed_capabilities_explicit = _allowed_capability_policy(analysis)
    previous = previous_observations or []
    failed_names = {
        str(observation.get("action_name"))
        for observation in previous
        if observation.get("status") != "completed"
    }
    goal_type = _goal_type(goal, analysis)
    needs_command_probe = _needs_test_command_probe(goal, goal_type, previous)
    requirements = _requirements_from_analysis(analysis)
    missing_requirements = _missing_requirements(previous)
    required_probe_tool = _probe_tool_for_missing(missing_requirements)
    candidates: list[dict[str, Any]] = []

    skill_candidates = _ordered_skill_candidates(session, goal, analysis)
    for skill in skill_candidates:
        if skill.enabled != "true":
            candidates.append(_candidate_trace("skill_run", skill.id, {}, -1.0, ["skill disabled"], {"task": goal, "goal": goal}))
            continue
        contract = capability_contract_for("skill_run", skill.id)
        breakdown, rejected_reasons = _score_skill(
            session,
            skill,
            goal,
            goal_type,
            failed_names,
            analysis,
            requirements,
            missing_requirements,
            contract,
        )
        if allowed_capabilities_explicit and skill.id not in allowed_capabilities:
            if "capability not allowed by allowed_capabilities" not in rejected_reasons:
                rejected_reasons.append("capability not allowed by allowed_capabilities")
            breakdown = dict(breakdown)
            score = -1.0
        else:
            score = sum(breakdown.values())
        candidates.append(
            _candidate_trace("skill_run", skill.id, breakdown, score, rejected_reasons, {"task": goal, "goal": goal}, contract)
        )

    tool_candidates = _ordered_tool_candidates(session, goal, analysis)
    for tool in tool_candidates:
        input_data = _tool_input(tool.name, goal, intent_slots)
        contract = capability_contract_for("tool_call", tool.name)
        if tool.enabled != "true":
            breakdown: dict[str, float] = {}
            rejected_reasons = ["tool disabled"]
            score = -1.0
        else:
            breakdown, rejected_reasons = _score_tool(
                session,
                tool,
                goal,
                goal_type,
                failed_names,
                analysis,
                needs_command_probe,
                requirements,
                missing_requirements,
                contract,
            )
            score = sum(breakdown.values())
        if allowed_tools_explicit and tool.name not in allowed_tools and tool.name != required_probe_tool:
            if "tool not allowed by intent allowed_tools" not in rejected_reasons:
                rejected_reasons.append("tool not allowed by intent allowed_tools")
            score = -1.0
        if allowed_capabilities_explicit and tool.name not in allowed_capabilities and tool.name != required_probe_tool:
            if "capability not allowed by allowed_capabilities" not in rejected_reasons:
                rejected_reasons.append("capability not allowed by allowed_capabilities")
            score = -1.0
        candidates.append(_candidate_trace("tool_call", tool.name, breakdown, score, rejected_reasons, input_data, contract))

    viable = [candidate for candidate in candidates if float(candidate["score"]) > 0]
    if viable:
        viable.sort(key=lambda item: (float(item["score"]), str(item["name"])), reverse=True)
        selected_candidate = viable[0]
        selected = SelectedAction(
            type=str(selected_candidate["type"]),
            name=str(selected_candidate["name"]),
            input_data=dict(selected_candidate["input_data"]),
            reason=f"selected highest scoring {selected_candidate['type']} for {goal_type} goal",
            score=float(selected_candidate["score"]),
            score_breakdown=dict(selected_candidate["score_breakdown"]),
            rejected_reasons=list(selected_candidate["rejected_reasons"]),
        )
    else:
        selected = SelectedAction(
            type="workflow_run",
            name="simple_chat",
            input_data={"input": goal},
            reason="no higher scoring tool or skill matched",
            no_tool_reason="no enabled matching capability",
            score=0.1,
            score_breakdown={"fallback": 0.1},
            rejected_reasons=[],
        )
        candidates.append(
            _candidate_trace(
                "workflow_run",
                "simple_chat",
                {"fallback": 0.1},
                0.1,
                ["no enabled matching tool or skill scored above zero"],
                {"input": goal},
                capability_contract_for("workflow_run", "simple_chat"),
            )
        )

    candidates.sort(key=lambda item: (float(item["score"]), str(item["name"])), reverse=True)
    trace_candidates = candidates[:8]
    trace_keys = {(item["type"], item["name"]) for item in trace_candidates}
    for item in candidates[8:]:
        rejected = item.get("rejected_reasons", [])
        if (
            "tool not allowed by intent allowed_tools" not in rejected
            and "capability not allowed by allowed_capabilities" not in rejected
        ):
            continue
        trace_key = (item["type"], item["name"])
        if trace_key in trace_keys:
            continue
        trace_candidates.append(item)
        trace_keys.add(trace_key)

    trace = {
        "goal_type": goal_type,
        "intent": intent,
        "selected": {
            "type": selected.type,
            "name": selected.name,
            "score": selected.score,
            "reason": selected.reason,
        },
        "requirements": requirements,
        "candidates": trace_candidates,
    }
    return SelectionResult(selected=selected, trace=trace)


def _ordered_skill_candidates(session: Session, goal: str, analysis: dict[str, Any]) -> list[Skill]:
    intent = _intent_decision(analysis)
    intent_candidates = [
        str(item.get("name"))
        for item in intent.get("candidate_actions", [])
        if isinstance(item, dict) and item.get("type") == "skill_run" and item.get("name")
    ]
    capability_candidates = _capability_candidate_ids(analysis, "skill")
    candidate_ids = [*intent_candidates, *capability_candidates, *[str(item) for item in analysis.get("candidate_skills", []) if item]]
    rows = {row.id: row for row in session.execute(select(Skill).order_by(Skill.id)).scalars().all()}
    ordered: list[Skill] = [rows[item] for item in candidate_ids if item in rows]
    lowered = goal.lower()
    for row in rows.values():
        if row in ordered:
            continue
        triggers = _json_list(row.triggers_json)
        haystack = " ".join([row.id, row.description, *triggers]).lower()
        if any(term in haystack for term in _terms(lowered)):
            ordered.append(row)
    if not ordered:
        for fallback in _fallback_skill_ids(lowered):
            row = rows.get(fallback)
            if row is not None and row not in ordered:
                ordered.append(row)
    return ordered


def _ordered_tool_candidates(session: Session, goal: str, analysis: dict[str, Any]) -> list[ToolManifest]:
    intent = _intent_decision(analysis)
    intent_candidates = [
        str(item.get("name"))
        for item in intent.get("candidate_actions", [])
        if isinstance(item, dict) and item.get("type") == "tool_call" and item.get("name")
    ]
    capability_candidates = _capability_candidate_ids(analysis, "tool")
    candidate_ids = [*intent_candidates, *capability_candidates, *[str(item) for item in analysis.get("candidate_tools", []) if item]]
    rows = {row.name: row for row in session.execute(select(ToolManifest).order_by(ToolManifest.name)).scalars().all()}
    ordered: list[ToolManifest] = [rows[item] for item in candidate_ids if item in rows]
    lowered = goal.lower()
    for fallback in _fallback_tool_ids(lowered):
        row = rows.get(fallback)
        if row is not None and row not in ordered:
            ordered.append(row)
    for row in rows.values():
        if row in ordered:
            continue
        haystack = f"{row.name} {row.description}".lower()
        if any(term in haystack for term in _terms(lowered)):
            ordered.append(row)
    return ordered


def _score_skill(
    session: Session,
    skill: Skill,
    goal: str,
    goal_type: str,
    failed_names: set[str],
    analysis: dict[str, Any],
    requirements: list[str],
    missing_requirements: set[str],
    contract: dict[str, list[str]],
) -> tuple[dict[str, float], list[str]]:
    lowered = goal.lower()
    breakdown = _base_breakdown()
    rejected_reasons: list[str] = []
    if skill.id in [str(item) for item in analysis.get("candidate_skills", [])]:
        breakdown["goal_analyzer_candidate"] = 2.0
    if _intent_candidate_score(analysis, "skill_run", skill.id) > 0:
        breakdown["intent_candidate"] = 2.5 * _intent_candidate_score(analysis, "skill_run", skill.id)
    if skill.id in failed_names:
        breakdown["previous_iteration_penalty"] = -5.0
        rejected_reasons.append("failed in previous iteration")
    if skill.id in _fallback_skill_ids(lowered):
        breakdown["fallback_match"] = 2.0
    _apply_requirement_match(breakdown, requirements, missing_requirements, contract)
    for term in _terms(lowered):
        if term and (term in skill.id.lower() or term in skill.description.lower()):
            breakdown["text_match"] += 0.4
    triggers = _json_list(skill.triggers_json)
    if any(trigger.lower() in lowered for trigger in triggers):
        breakdown["trigger_match"] = 1.5
    signal, signal_reasons = _effectiveness_signal(session, "skill", skill.id, goal_type)
    breakdown.update(signal)
    rejected_reasons.extend(signal_reasons)
    return breakdown, rejected_reasons


def _score_tool(
    session: Session,
    tool: ToolManifest,
    goal: str,
    goal_type: str,
    failed_names: set[str],
    analysis: dict[str, Any],
    needs_command_probe: bool,
    requirements: list[str],
    missing_requirements: set[str],
    contract: dict[str, list[str]],
) -> tuple[dict[str, float], list[str]]:
    lowered = goal.lower()
    breakdown = _base_breakdown(base=1.5)
    rejected_reasons: list[str] = []
    if tool.name in [str(item) for item in analysis.get("candidate_tools", [])]:
        breakdown["goal_analyzer_candidate"] = 2.0
    if _intent_candidate_score(analysis, "tool_call", tool.name) > 0:
        breakdown["intent_candidate"] = 2.5 * _intent_candidate_score(analysis, "tool_call", tool.name)
    if tool.name in failed_names:
        breakdown["previous_iteration_penalty"] = -5.0
        rejected_reasons.append("failed in previous iteration")
    if tool.name in _fallback_tool_ids(lowered):
        breakdown["fallback_match"] = 1.5
    if needs_command_probe and tool.name == "shell.run":
        breakdown["replan_probe"] = 3.0
    _apply_requirement_match(breakdown, requirements, missing_requirements, contract)
    for term in _terms(lowered):
        if term and (term in tool.name.lower() or term in tool.description.lower()):
            breakdown["text_match"] += 0.3
    signal, signal_reasons = _effectiveness_signal(session, "tool", tool.name, goal_type)
    breakdown.update(signal)
    rejected_reasons.extend(signal_reasons)
    return breakdown, rejected_reasons


def _base_breakdown(base: float = 2.0) -> dict[str, float]:
    return {
        "base": base,
        "goal_analyzer_candidate": 0.0,
        "trigger_match": 0.0,
        "text_match": 0.0,
        "fallback_match": 0.0,
        "input_fit": 1.0,
        "side_effect_fit": 0.5,
        "previous_iteration_penalty": 0.0,
        "recent_failure_penalty": 0.0,
        "effectiveness": 0.0,
        "replan_probe": 0.0,
        "requirement_match": 0.0,
        "intent_candidate": 0.0,
    }


def _effectiveness_signal(
    session: Session,
    capability_type: str,
    capability_id: str,
    goal_type: str,
) -> tuple[dict[str, float], list[str]]:
    events = (
        session.execute(
            select(CapabilityEffectivenessEvent)
            .where(CapabilityEffectivenessEvent.capability_type == capability_type)
            .where(CapabilityEffectivenessEvent.capability_id == capability_id)
            .where(CapabilityEffectivenessEvent.goal_type.in_([goal_type, "general"]))
            .order_by(CapabilityEffectivenessEvent.created_at.desc())
            .limit(8)
        )
        .scalars()
        .all()
    )
    if not events:
        events = (
            session.execute(
                select(CapabilityEffectivenessEvent)
                .where(CapabilityEffectivenessEvent.capability_type == capability_type)
                .where(CapabilityEffectivenessEvent.capability_id == capability_id)
                .order_by(CapabilityEffectivenessEvent.created_at.desc())
                .limit(8)
            )
            .scalars()
            .all()
        )
    if events:
        successes = sum(1 for event in events if event.status == "success")
        failures = sum(1 for event in events if event.status == "failure")
        blocked = sum(1 for event in events if event.status == "blocked")
        signal = successes * 0.4 - blocked * 0.4
        recent_failure_penalty = failures * -1.2
        reasons = ["recent failures lower confidence"] if failures >= 2 else []
        return {"effectiveness": signal, "recent_failure_penalty": recent_failure_penalty}, reasons

    effectiveness = effectiveness_for(session, capability_type, capability_id, goal_type)
    if effectiveness is None or not effectiveness.attempts:
        return {"effectiveness": 0.0, "recent_failure_penalty": 0.0}, []
    success_rate = effectiveness.successes / effectiveness.attempts
    failure_rate = effectiveness.failures / effectiveness.attempts
    return {"effectiveness": (success_rate * 1.2) - (failure_rate * 0.8), "recent_failure_penalty": 0.0}, []


def _candidate_trace(
    action_type: str,
    name: str,
    breakdown: dict[str, float],
    score: float,
    rejected_reasons: list[str],
    input_data: dict[str, Any],
    contract: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    return {
        "type": action_type,
        "name": name,
        "score": round(float(score), 4),
        "score_breakdown": {key: round(float(value), 4) for key, value in breakdown.items()},
        "rejected_reasons": rejected_reasons,
        "input_data": input_data,
        "contract": contract or capability_contract_for(action_type, name),
    }


def capability_contract_for(action_type: str, name: str) -> dict[str, list[str]]:
    if name == "shell.run":
        return {
            "evidence_produced": ["command_output", "test_command_evidence"],
            "verification_hints": ["Use stdout/stderr and exit code as command evidence."],
            "failure_modes": ["command_failed", "timeout", "goal_incomplete"],
        }
    if name == "git.status":
        return {
            "evidence_produced": ["workspace_state"],
            "verification_hints": ["Use git status output as workspace state evidence."],
            "failure_modes": ["command_failed"],
        }
    if action_type == "skill_run" and name == "code.local_task":
        return {
            "evidence_produced": ["workspace_state", "command_output"],
            "verification_hints": ["Use skill workflow outputs as code/workspace evidence."],
            "failure_modes": ["workflow_failed", "goal_incomplete"],
        }
    if action_type == "skill_run" and name == "file.workspace_ops":
        return {
            "evidence_produced": ["workspace_listing", "file_evidence"],
            "verification_hints": ["Use file listing or file content outputs as workspace evidence."],
            "failure_modes": ["workflow_failed", "file_not_found"],
        }
    if name in {"web.search", "web.fetch", "research.report"}:
        return {
            "evidence_produced": ["source_or_evidence_explanation"],
            "verification_hints": ["Use fetched source records and snippets as evidence."],
            "failure_modes": ["network_failed", "source_missing"],
        }
    return {
        "evidence_produced": ["general_observation"],
        "verification_hints": ["Use completed non-empty output as general evidence."],
        "failure_modes": ["action_failed", "goal_incomplete"],
    }


def _tool_input(name: str, goal: str, slots: dict[str, Any] | None = None) -> dict[str, Any]:
    slots = slots or {}
    lowered = goal.lower()
    if name == "fs.read_text":
        if slots.get("path"):
            return {"path": str(slots["path"])}
        if "readme" in lowered:
            return {"path": "README.md"}
        if "agent" in lowered:
            return {"path": "agent.md"}
        return {"path": "agent.md"}
    if name == "fs.list":
        return {"path": "."}
    if name == "fs.glob":
        return {"pattern": "*"}
    if name == "git.status":
        return {"cwd": "."}
    if name == "shell.run":
        if "pytest" in lowered or "test" in lowered or "测试" in goal:
            return {"command": "python -m pytest --version", "cwd": ".", "timeout_seconds": 30}
        return {"command": "python --version", "cwd": ".", "timeout_seconds": 30}
    if name == "web.search":
        return {"query": goal, "provider": "fake", "limit": 3}
    if name == "web.fetch":
        return {"url": "https://example.com/search/1"}
    if name == "tools.discover":
        return {"query": goal}
    return {"input": goal}


def _intent_decision(analysis: dict[str, Any]) -> dict[str, Any]:
    payload = analysis.get("intent_decision")
    return payload if isinstance(payload, dict) else {}


def _allowed_capability_policy(analysis: dict[str, Any]) -> tuple[set[str], bool]:
    if "allowed_capabilities" not in analysis:
        return set(), False
    raw_values = analysis.get("allowed_capabilities", [])
    if not isinstance(raw_values, list):
        return set(), True
    return {str(item) for item in raw_values if item}, True


def _capability_candidate_ids(analysis: dict[str, Any], capability_type: str) -> list[str]:
    rows = analysis.get("candidate_capabilities", [])
    if not isinstance(rows, list):
        return []
    result: list[str] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        if item.get("type") != capability_type:
            continue
        if item.get("executable") is False:
            continue
        capability_id = item.get("id") or item.get("capability_id") or item.get("name")
        if capability_id is not None and str(capability_id) not in result:
            result.append(str(capability_id))
    return result


def _intent_candidate_score(analysis: dict[str, Any], action_type: str, name: str) -> float:
    intent = _intent_decision(analysis)
    for item in intent.get("candidate_actions", []):
        if not isinstance(item, dict):
            continue
        if item.get("type") != action_type or item.get("name") != name:
            continue
        try:
            return float(item.get("score", 0.0))
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _goal_type(goal: str, analysis: dict[str, Any]) -> str:
    lowered = goal.lower()
    candidates = [str(item) for item in analysis.get("candidate_skills", [])]
    if "code.local_task" in candidates or _contains_any(lowered, ["test", "pytest", "code", "bug", "测试", "代码"]):
        return "code"
    if "research.report" in candidates or _contains_any(lowered, ["research", "search", "report", "搜索", "调研", "报告"]):
        return "research"
    if "file.workspace_ops" in candidates or _contains_any(lowered, ["file", "workspace", "read", "list", "文件", "读取"]):
        return "workspace"
    return "general"


def _needs_test_command_probe(goal: str, goal_type: str, previous_observations: list[dict[str, Any]]) -> bool:
    if goal_type != "code" or not _contains_any(goal.lower(), ["test", "pytest", "测试"]):
        return False
    return any(observation.get("status") != "completed" for observation in previous_observations)


def _requirements_from_analysis(analysis: dict[str, Any]) -> list[str]:
    rows = analysis.get("requirements", [])
    if not isinstance(rows, list):
        return []
    result: list[str] = []
    for row in rows:
        if isinstance(row, dict) and row.get("id"):
            result.append(str(row["id"]))
    return result


def _missing_requirements(previous_observations: list[dict[str, Any]]) -> set[str]:
    missing: set[str] = set()
    for observation in previous_observations:
        values = observation.get("missing_requirements", [])
        if isinstance(values, list):
            missing.update(str(item) for item in values)
    return missing


def _probe_tool_for_missing(missing_requirements: set[str]) -> str | None:
    if "test_command_evidence" in missing_requirements:
        return "shell.run"
    return None


def _apply_requirement_match(
    breakdown: dict[str, float],
    requirements: list[str],
    missing_requirements: set[str],
    contract: dict[str, list[str]],
) -> None:
    if not missing_requirements:
        return
    evidence = set(contract.get("evidence_produced", []))
    expected_requirements = set(requirements) if requirements else set(missing_requirements)
    matches = expected_requirements.intersection(missing_requirements).intersection(evidence)
    if matches:
        breakdown["requirement_match"] = float(len(matches) * 2.5)


def _fallback_skill_ids(lowered_goal: str) -> list[str]:
    result: list[str] = []
    if _contains_any(lowered_goal, ["research", "search", "report", "搜索", "调研", "报告"]):
        result.append("research.report")
    if _contains_any(lowered_goal, ["test", "pytest", "code", "bug", "测试", "代码", "修复"]):
        result.append("code.local_task")
    if _contains_any(lowered_goal, ["file", "workspace", "read", "list", "文件", "读取", "目录", "项目"]):
        result.append("file.workspace_ops")
    return result


def _fallback_tool_ids(lowered_goal: str) -> list[str]:
    result: list[str] = []
    if _contains_any(lowered_goal, ["search", "research", "report", "搜索", "调研"]):
        result.extend(["web.search", "web.fetch"])
    if _contains_any(lowered_goal, ["readme", "read", "file", "读取", "文件"]):
        result.extend(["fs.read_text", "fs.list"])
    if _contains_any(lowered_goal, ["test", "pytest", "code", "bug", "测试", "代码"]):
        result.extend(["shell.run", "git.status"])
    if not result:
        result.append("tools.discover")
    return result


def _terms(lowered_goal: str) -> list[str]:
    return [term.strip(".,:;!?()[]{}") for term in lowered_goal.split() if len(term.strip(".,:;!?()[]{}")) >= 3]


def _contains_any(text: str, needles: list[str]) -> bool:
    return any(needle in text for needle in needles)


def _json_list(raw_json: str) -> list[str]:
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in payload] if isinstance(payload, list) else []
