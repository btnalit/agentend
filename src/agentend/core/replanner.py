from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from agentend.core.errors import classify_exception
from agentend.core.events import record_event
from agentend.db.models import ReplanSuggestion


def replan_failure(
    *,
    failed_step: str,
    error: str,
    error_code: str | None = None,
    goal: str = "",
    current_workflow: str = "",
    observations: list[str] | None = None,
) -> dict[str, Any]:
    code = error_code or classify_exception(RuntimeError(error)).code
    lowered = f"{code} {error}".lower()
    if code == "tool_not_found" or "unknown tool" in lowered:
        action = "alternative_tool"
        reason = "Tool is not registered; discover available tools before retrying."
        alternative_tool = "tools.discover"
        next_steps = ["Run tools.discover with the goal text.", "Replace the missing tool with a registered tool."]
    elif code == "missing_config" or "provider" in lowered or "not configured" in lowered:
        action = "ask_user"
        reason = "Required provider or configuration is missing."
        alternative_tool = "tools.discover"
        next_steps = ["Ask the user for the missing provider or secret.", "Use a configured alternative if available."]
    elif code == "timeout":
        action = "retry"
        reason = "The failed step timed out and is marked retryable."
        alternative_tool = None
        next_steps = ["Retry once with a higher timeout.", "Reduce input size if the retry also times out."]
    elif code in {"schema_error", "permission_error"}:
        action = "fix_input" if code == "schema_error" else "ask_user"
        reason = "The failure requires corrected input or explicit user confirmation."
        alternative_tool = None
        next_steps = ["Inspect the tool schema and failed input.", "Resume only after the corrected input is available."]
    else:
        action = "fail_with_reason"
        reason = "No deterministic recovery path is known for this error."
        alternative_tool = None
        next_steps = ["Inspect the run log and tool output.", "Ask the user before trying another high-impact action."]
    return {
        "goal": goal,
        "current_workflow": current_workflow,
        "failed_step": failed_step,
        "error_code": code,
        "action": action,
        "reason": reason,
        "alternative_tool": alternative_tool,
        "next_steps": next_steps,
        "observations": observations or [],
    }


def record_replan_suggestion(
    session: Session,
    *,
    run_id: str | None,
    step_id: str | None,
    failed_step: str,
    error_code: str,
    error_message: str,
    suggestion: dict[str, Any],
) -> ReplanSuggestion:
    row = ReplanSuggestion(
        id=str(uuid4()),
        run_id=run_id,
        step_id=step_id,
        failed_step=failed_step,
        error_code=error_code,
        error_message=error_message,
        suggestion_json=json.dumps(suggestion, ensure_ascii=False, sort_keys=True),
    )
    session.add(row)
    record_event(session, "plan.replanned", suggestion, run_id=run_id)
    return row


def replan_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
