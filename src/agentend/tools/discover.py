from __future__ import annotations

import json

from sqlalchemy import select

from agentend.db.models import Capability, ToolManifest
from agentend.tools.base import ToolContext, ToolResult


class ToolsDiscoverTool:
    name = "tools.discover"
    description = "Discover available tools by query."
    input_schema = {"type": "object", "required": ["query"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        query = str(input_data["query"]).lower()
        capabilities = context.session.execute(select(Capability)).scalars().all()
        if capabilities:
            matches = [
                capability
                for capability in capabilities
                if query in capability.name.lower() or query in capability.action_summary.lower()
            ]
            payload = [{"name": item.name, "source": item.source, "side_effect": item.side_effect} for item in matches]
        else:
            manifests = context.session.execute(select(ToolManifest).where(ToolManifest.enabled == "true")).scalars().all()
            payload = [
                {"name": item.name, "source": item.source, "side_effect": item.side_effect}
                for item in manifests
                if query in item.name.lower() or query in item.description.lower()
            ]
        return ToolResult(content=json.dumps(payload, ensure_ascii=False, indent=2), data={"matches": payload})


class ToolsDescribeTool:
    name = "tools.describe"
    description = "Describe one available tool."
    input_schema = {"type": "object", "required": ["name"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        name = str(input_data["name"])
        manifest = context.session.get(ToolManifest, name)
        if manifest is None:
            raise ValueError(f"Unknown tool: {name}")
        payload = {"name": manifest.name, "description": manifest.description, "side_effect": manifest.side_effect}
        return ToolResult(content=json.dumps(payload, ensure_ascii=False, indent=2), data=payload)


DISCOVER_TOOLS = [ToolsDiscoverTool(), ToolsDescribeTool()]
