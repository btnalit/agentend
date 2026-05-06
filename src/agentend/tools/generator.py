from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from agentend.core.events import record_event
from agentend.core.skills import upsert_extension
from agentend.db.models import GeneratedTool
from agentend.tools.base import ToolContext, ToolResult


class ToolGenerateTool:
    name = "tools.generate"
    description = "Generate a local draft tool package without enabling or registering it."
    input_schema = {
        "type": "object",
        "properties": {
            "goal": {"type": "string"},
            "name": {"type": "string"},
        },
        "required": ["goal"],
    }

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        goal = str(input_data.get("goal", "")).strip()
        if not goal:
            raise ValueError("goal is required")
        name = _normalize_tool_name(str(input_data.get("name") or _name_from_goal(goal)))
        draft_dir = context.home / "data" / "generated_tools" / name
        draft_dir.mkdir(parents=True, exist_ok=True)

        tool_yaml = {
            "name": name,
            "status": "draft",
            "goal": goal,
            "description": f"Draft generated tool for: {goal}",
            "side_effect": "local_read",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
            "output_schema": {"type": "object"},
        }
        (draft_dir / "tool.yaml").write_text(yaml.safe_dump(tool_yaml, sort_keys=False), encoding="utf-8")
        (draft_dir / "implementation.py").write_text(_implementation_template(name, goal), encoding="utf-8")
        (draft_dir / "test_workflow.yaml").write_text(_workflow_template(name), encoding="utf-8")

        files = ["tool.yaml", "implementation.py", "test_workflow.yaml"]
        metadata = {"files": files, "registered": False}
        row = context.session.get(GeneratedTool, name)
        if row is None:
            row = GeneratedTool(id=name)
            context.session.add(row)
        row.goal = goal
        row.draft_path = str(draft_dir)
        row.status = "draft"
        row.metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        upsert_extension(
            context.session,
            kind="generated_tool",
            name=name,
            status="draft",
            source=str(draft_dir),
            version="0.1.0",
        )
        record_event(
            context.session,
            "tool.generated_draft",
            {"tool_name": name, "draft_path": str(draft_dir)},
            run_id=context.run_id,
        )
        data = {"name": name, "draft_path": str(draft_dir), "status": "draft", "registered": False, "files": files}
        return ToolResult(content=json.dumps(data, ensure_ascii=False, sort_keys=True), data=data)


def _normalize_tool_name(value: str) -> str:
    name = value.strip()
    if not name.startswith("generated."):
        name = f"generated.{name}"
    if "/" in name or "\\" in name or ".." in name:
        raise ValueError("Generated tool name must not contain path separators or parent directory references")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError("Generated tool name may only contain letters, numbers, dot, underscore, and hyphen")
    return name


def _name_from_goal(goal: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", goal.lower()).strip("_")
    return slug[:48] or "draft_tool"


def _class_name(name: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", name) if part) + "Tool"


def _implementation_template(name: str, goal: str) -> str:
    class_name = _class_name(name)
    return f'''from __future__ import annotations

from agentend.tools.base import ToolContext, ToolResult


class {class_name}:
    name = "{name}"
    description = "Draft generated tool for: {goal}"
    input_schema = {{"type": "object"}}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        raise NotImplementedError("Generated tool drafts must be reviewed before enablement.")


def build_tool() -> {class_name}:
    return {class_name}()
'''


def _workflow_template(name: str) -> str:
    return f"""id: test.{name}
name: Test {name}
nodes:
  - id: call_tool
    type: tool
    tool: {name}
    input: {{}}
  - id: final
    type: final
    depends_on: [call_tool]
"""


GENERATOR_TOOLS = [ToolGenerateTool()]
