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
    failed_names = {
        str(observation.get("action_name"))
        for observation in (previous_observations or [])
        if observation.get("status") != "completed"
    }
    goal_type = _goal_type(goal, analysis)
    candidates: list[dict[str, Any]] = []

    skill_candidates = _ordered_skill_candidates(session, goal, analysis)
    for skill in skill_candidates:
        if skill.enabled != "true":
            candidates.append(_candidate_trace("skill_run", skill.id, {}, -1.0, ["skill disabled"], {"task": goal, "goal": goal}))
            continue
        breakdown, rejected_reasons = _score_skill(session, skill, goal, goal_type, failed_names, analysis)
        candidates.append(
            _candidate_trace("skill_run", skill.id, breakdown, sum(breakdown.values()), rejected_reasons, {"task": goal, "goal": goal})
        )

    tool_candidates = _ordered_tool_candidates(session, goal, analysis)
    for tool in tool_candidates:
        if tool.enabled != "true":
            candidates.append(_candidate_trace("tool_call", tool.name, {}, -1.0, ["tool disabled"], _tool_input(tool.name, goal)))
            continue
        breakdown, rejected_reasons = _score_tool(session, tool, goal, goal_type, failed_names, analysis)
        candidates.append(
            _candidate_trace("tool_call", tool.name, breakdown, sum(breakdown.values()), rejected_reasons, _tool_input(tool.name, goal))
        )

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
            )
        )

    candidates.sort(key=lambda item: (float(item["score"]), str(item["name"])), reverse=True)
    trace = {
        "goal_type": goal_type,
        "selected": {
            "type": selected.type,
            "name": selected.name,
            "score": selected.score,
            "reason": selected.reason,
        },
        "candidates": candidates[:8],
    }
    return SelectionResult(selected=selected, trace=trace)


def _ordered_skill_candidates(session: Session, goal: str, analysis: dict[str, Any]) -> list[Skill]:
    candidate_ids = [str(item) for item in analysis.get("candidate_skills", []) if item]
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
    candidate_ids = [str(item) for item in analysis.get("candidate_tools", []) if item]
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
) -> tuple[dict[str, float], list[str]]:
    lowered = goal.lower()
    breakdown = _base_breakdown()
    rejected_reasons: list[str] = []
    if skill.id in [str(item) for item in analysis.get("candidate_skills", [])]:
        breakdown["goal_analyzer_candidate"] = 2.0
    if skill.id in failed_names:
        breakdown["previous_iteration_penalty"] = -2.0
        rejected_reasons.append("failed in previous iteration")
    if skill.id in _fallback_skill_ids(lowered):
        breakdown["fallback_match"] = 2.0
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
) -> tuple[dict[str, float], list[str]]:
    lowered = goal.lower()
    breakdown = _base_breakdown(base=1.5)
    rejected_reasons: list[str] = []
    if tool.name in [str(item) for item in analysis.get("candidate_tools", [])]:
        breakdown["goal_analyzer_candidate"] = 2.0
    if tool.name in failed_names:
        breakdown["previous_iteration_penalty"] = -2.0
        rejected_reasons.append("failed in previous iteration")
    if tool.name in _fallback_tool_ids(lowered):
        breakdown["fallback_match"] = 1.5
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
) -> dict[str, Any]:
    return {
        "type": action_type,
        "name": name,
        "score": round(float(score), 4),
        "score_breakdown": {key: round(float(value), 4) for key, value in breakdown.items()},
        "rejected_reasons": rejected_reasons,
        "input_data": input_data,
    }


def _tool_input(name: str, goal: str) -> dict[str, Any]:
    lowered = goal.lower()
    if name == "fs.read_text":
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
