from __future__ import annotations

from agentend.core.goal_analyzer import analyze_goal, goal_analysis_text
from agentend.core.replanner import record_replan_suggestion, replan_failure, replan_text
from agentend.tools.base import ToolContext, ToolResult


class GoalAnalyzeTool:
    name = "goal.analyze"
    description = "Analyze user text and recommend candidate skills, tools, and workflows."
    input_schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        payload = analyze_goal(context.home, context.session, str(input_data.get("text", "")))
        return ToolResult(content=goal_analysis_text(payload), data=payload)


class PlanReplanTool:
    name = "plan.replan"
    description = "Generate a deterministic next-step suggestion from a failed workflow step."
    input_schema = {
        "type": "object",
        "properties": {
            "goal": {"type": "string"},
            "current_workflow": {"type": "string"},
            "failed_step": {"type": "string"},
            "error": {"type": "string"},
            "error_code": {"type": "string"},
            "observations": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["failed_step", "error"],
    }

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        payload = replan_failure(
            goal=str(input_data.get("goal", "")),
            current_workflow=str(input_data.get("current_workflow", "")),
            failed_step=str(input_data.get("failed_step", "")),
            error=str(input_data.get("error", "")),
            error_code=str(input_data.get("error_code") or "") or None,
            observations=[str(item) for item in input_data.get("observations", [])],
        )
        record_replan_suggestion(
            context.session,
            run_id=context.run_id,
            step_id=context.step_id,
            failed_step=payload["failed_step"],
            error_code=payload["error_code"],
            error_message=str(input_data.get("error", "")),
            suggestion=payload,
        )
        return ToolResult(content=replan_text(payload), data=payload)


PLANNING_TOOLS = [GoalAnalyzeTool(), PlanReplanTool()]
