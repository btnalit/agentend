from agentend.core.memory_store import search_memory_items, write_memory_item
from agentend.tools.base import ToolContext, ToolResult


class MemoryWriteTool:
    name = "memory.write"
    description = "Write a local memory row."
    input_schema = {"type": "object", "required": ["key", "content"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        memory = write_memory_item(
            context.session,
            context.home,
            content=str(input_data["content"]),
            scope=str(input_data.get("scope", "default")),
            source=str(input_data.get("source", "tool")),
            confidence=str(input_data.get("confidence", "1.0")),
            ttl=input_data.get("ttl"),
            tags=[str(item) for item in input_data.get("tags", [])],
        )
        return ToolResult(content=memory.content, data={"memory_id": memory.id})


class MemorySearchTool:
    name = "memory.search"
    description = "Search local memories by substring."
    input_schema = {"type": "object", "required": ["query"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        query = str(input_data["query"])
        matches = search_memory_items(context.session, query, scope=input_data.get("scope"))
        content = "\n".join(memory.content for memory in matches)
        return ToolResult(content=content, data={"count": len(matches)})
