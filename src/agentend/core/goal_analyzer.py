from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from agentend.config import load_config
from agentend.core.capabilities import query_capabilities, refresh_capabilities
from agentend.core.events import record_event
from agentend.core.skills import ensure_builtin_skills
from agentend.core.workspace_indexer import index_workspace, workspace_summary
from agentend.core.workflow_registry import WorkflowRegistry


def analyze_goal(home: Path, session: Session, text: str) -> dict[str, Any]:
    normalized = text.strip()
    ensure_builtin_skills(home, session)
    refresh_capabilities(session)
    workspace_rows = workspace_summary(session)
    if not workspace_rows:
        workspace_rows = index_workspace(home, session)
    workflows, _ = WorkflowRegistry(load_config(home)).list_workflows()
    capability_rows = query_capabilities(session, normalized)

    candidate_skills: list[str] = []
    candidate_tools: list[str] = []
    risk_notes: list[str] = []
    lowered = normalized.lower()

    if _contains_any(lowered, ["调研", "研究", "搜索", "查找", "报告", "research", "search", "report"]):
        _append(candidate_skills, "research.report")
        _append(candidate_tools, "web.search")
        _append(candidate_tools, "web.fetch")
    if _contains_any(lowered, ["测试", "修复", "代码", "pytest", "test", "code", "bug"]):
        _append(candidate_skills, "code.local_task")
        _append(candidate_tools, "shell.run")
        _append(candidate_tools, "git.status")
        risk_notes.append("shell.run can execute local commands; keep commands explicit.")
    if _contains_any(lowered, ["文件", "目录", "整理", "读取", "写入", "workspace", "file"]):
        _append(candidate_skills, "file.workspace_ops")
        _append(candidate_tools, "fs.list")
        _append(candidate_tools, "fs.read_text")

    for capability in capability_rows[:8]:
        if capability.source == "skill":
            _append(candidate_skills, capability.name)
        elif capability.source == "tool":
            _append(candidate_tools, capability.name)
        if capability.risk_level == "high":
            risk_notes.append(f"{capability.name} is high risk: {capability.side_effect}")

    if not candidate_skills and not candidate_tools:
        _append(candidate_workflows := [], "simple_chat")
    else:
        candidate_workflows = [workflow.id for workflow in workflows if workflow.id != "simple_chat"][:3]
        if not candidate_workflows and any(workflow.id == "simple_chat" for workflow in workflows):
            candidate_workflows = ["simple_chat"]

    payload = {
        "goal": normalized,
        "constraints": [],
        "candidate_skills": candidate_skills,
        "candidate_tools": candidate_tools,
        "candidate_workflows": candidate_workflows,
        "missing_inputs": ["goal"] if not normalized else [],
        "risk_notes": sorted(set(risk_notes)),
        "workspace_context": [
            {"path": row.source_path, "summary": row.summary[:240]} for row in workspace_rows[:5]
        ],
    }
    record_event(session, "goal.analyzed", {"goal": normalized, "result": payload})
    return payload


def goal_analysis_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _contains_any(text: str, needles: list[str]) -> bool:
    return any(needle in text for needle in needles)


def _append(values: list[str], item: str) -> None:
    if item not in values:
        values.append(item)
