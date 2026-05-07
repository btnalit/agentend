from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class GoalRequirement:
    id: str
    kind: str
    description: str
    required: bool = True
    evidence_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def infer_goal_requirements(goal: str, goal_analysis: dict[str, Any] | None = None) -> list[GoalRequirement]:
    existing = _requirements_from_analysis(goal_analysis or {})
    if existing:
        if not any(requirement.id == "non_empty_observation" for requirement in existing):
            return [_non_empty_requirement()] + existing
        return existing
    lowered = goal.lower()
    requirements = [_non_empty_requirement()]
    if _goal_requires_test_command(lowered):
        requirements.append(
            GoalRequirement(
                id="test_command_evidence",
                kind="test_command_evidence",
                description="Output must include concrete test command evidence.",
                evidence_hint="pytest, python -m pytest, unittest, tox, py.test, or nox",
            )
        )
    if any(term in lowered for term in ["evidence", "source", "依据", "来源"]):
        requirements.append(
            GoalRequirement(
                id="source_or_evidence_explanation",
                kind="source_or_evidence_explanation",
                description="Output should explain the evidence or source used.",
                required=False,
                evidence_hint="evidence/source wording or structured tool output",
            )
        )
    if any(term in lowered for term in ["artifact", "progress", "产物", "进度"]):
        requirements.append(
            GoalRequirement(
                id="artifact_or_progress",
                kind="artifact_or_progress",
                description="Run should preserve a progress artifact or artifact reference.",
                required=False,
                evidence_hint="progress_artifact_id or artifact id",
            )
        )
    return requirements


def _non_empty_requirement() -> GoalRequirement:
    return GoalRequirement(
        id="non_empty_observation",
        kind="non_empty_observation",
        description="Action must produce a completed non-empty observation.",
        evidence_hint="completed output",
    )


def evaluate_goal_observation(
    goal: str,
    observation: dict[str, Any],
    *,
    iteration_index: int,
    max_iterations: int,
    goal_analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    requirements = infer_goal_requirements(goal, goal_analysis)
    output = str(observation.get("output", "")).strip()
    satisfied: list[str] = []
    missing: list[str] = []
    evidence_refs: list[str] = []
    for requirement in requirements:
        ok, refs = _validate_requirement(requirement, observation, output)
        if ok:
            satisfied.append(requirement.id)
            evidence_refs.extend(refs)
        elif requirement.required:
            missing.append(requirement.id)
    complete = not missing
    if observation.get("status") != "completed":
        complete = False
        if "non_empty_observation" not in missing:
            missing.append("non_empty_observation")
    required_count = max(1, len([item for item in requirements if item.required]))
    confidence = round((required_count - len(set(missing))) / required_count, 4)
    next_probe = _next_probe_for_missing(missing)
    incomplete_conditions = [_description_for(req_id, requirements) for req_id in missing]
    return {
        "complete": complete,
        "confidence": max(0.0, min(1.0, confidence)),
        "goal_type": "code" if any(req.id == "test_command_evidence" for req in requirements) else "general",
        "requirements": [requirement.to_dict() for requirement in requirements],
        "satisfied_requirements": sorted(set(satisfied)),
        "missing_requirements": sorted(set(missing)),
        "evidence_refs": sorted(set(evidence_refs)),
        "next_probe": next_probe,
        "next_action": "finish" if complete else "replan",
        "incomplete_conditions": [] if complete else incomplete_conditions,
        "remaining_iterations": max(0, max_iterations - iteration_index),
    }


def structured_evaluator_adapter(raw_json: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError:
        return {"complete": False, "confidence": 0.0, "missing_requirements": ["structured_judge_invalid_json"]}
    return payload if isinstance(payload, dict) else {"complete": False, "confidence": 0.0}


def _requirements_from_analysis(goal_analysis: dict[str, Any]) -> list[GoalRequirement]:
    rows = goal_analysis.get("requirements", [])
    if not isinstance(rows, list):
        return []
    result: list[GoalRequirement] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        req_id = str(row.get("id") or row.get("kind") or "")
        kind = str(row.get("kind") or req_id)
        if not req_id or not kind:
            continue
        result.append(
            GoalRequirement(
                id=req_id,
                kind=kind,
                description=str(row.get("description") or req_id),
                required=bool(row.get("required", True)),
                evidence_hint=str(row.get("evidence_hint") or ""),
            )
        )
    return result


def _validate_requirement(requirement: GoalRequirement, observation: dict[str, Any], output: str) -> tuple[bool, list[str]]:
    if requirement.kind == "non_empty_observation":
        return observation.get("status") == "completed" and bool(output), []
    if requirement.kind == "test_command_evidence":
        return _output_has_test_command_evidence(output), _test_evidence_refs(output)
    if requirement.kind == "source_or_evidence_explanation":
        lowered = output.lower()
        ok = any(
            term in lowered
            for term in [
                "evidence:",
                "source:",
                "according to",
                "because",
                "stdout",
                "stderr",
                "exit_code",
            ]
        )
        return ok, ["evidence_explanation"] if ok else []
    if requirement.kind == "artifact_or_progress":
        ok = bool(observation.get("progress_artifact_id") or observation.get("artifact_id"))
        return ok, ["artifact_reference"] if ok else []
    return False, []


def _goal_requires_test_command(lowered_goal: str) -> bool:
    return any(term in lowered_goal for term in ["test command", "pytest", "tests", "test", "测试命令", "测试"])


def _output_has_test_command_evidence(output: str) -> bool:
    lowered = _evidence_text(output).lower()
    return any(term in lowered for term in ["pytest", "python -m pytest", "unittest", "tox", "py.test", "nox"])


def _test_evidence_refs(output: str) -> list[str]:
    lowered = _evidence_text(output).lower()
    return [term for term in ["python -m pytest", "pytest", "unittest", "tox", "py.test", "nox"] if term in lowered]


def _evidence_text(output: str) -> str:
    ignored_prefixes = ("goal:", "task:", "request:")
    lines = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith(ignored_prefixes):
            continue
        lines.append(line)
    return "\n".join(lines)


def _next_probe_for_missing(missing: list[str]) -> str | None:
    if "test_command_evidence" in missing:
        return "shell.run"
    if "source_or_evidence_explanation" in missing:
        return "evidence.inspect"
    if "artifact_or_progress" in missing:
        return "artifact.inspect"
    if missing:
        return "replan"
    return None


def _description_for(requirement_id: str, requirements: list[GoalRequirement]) -> str:
    for requirement in requirements:
        if requirement.id == requirement_id:
            return requirement.description
    return requirement_id
