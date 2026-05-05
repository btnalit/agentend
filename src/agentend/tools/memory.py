import json
from uuid import uuid4

from sqlalchemy import select

from agentend.db.models import Memory
from agentend.tools.base import ToolContext, ToolResult


class MemoryWriteTool:
    name = "memory.write"
    description = "Write a local memory row."
    input_schema = {"type": "object", "required": ["key", "content"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        memory = Memory(
            id=str(uuid4()),
            scope=str(input_data.get("scope", "default")),
            key=str(input_data["key"]),
            content=str(input_data["content"]),
            tags_json=json.dumps(input_data.get("tags", []), ensure_ascii=False),
        )
        context.session.add(memory)
        return ToolResult(content=memory.content, data={"memory_id": memory.id})


class MemorySearchTool:
    name = "memory.search"
    description = "Search local memories by substring."
    input_schema = {"type": "object", "required": ["query"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        query = str(input_data["query"])
        memories = context.session.execute(select(Memory)).scalars().all()
        matches = [memory for memory in memories if query.lower() in memory.content.lower() or query.lower() in memory.key.lower()]
        content = "\n".join(memory.content for memory in matches)
        return ToolResult(content=content, data={"count": len(matches)})
