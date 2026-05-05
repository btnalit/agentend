import json
import time
from typing import Any
from uuid import uuid4

from agentend.db.models import MCPServer, MCPToolCall
from agentend.mcp.client import MCPClient
from agentend.tools.base import ToolContext, ToolResult


class MCPRegisteredTool:
    description = "MCP registered tool."

    def __init__(self, local_name: str, server_id: str, tool_name: str, input_schema: dict[str, Any]):
        self.name = local_name
        self.server_id = server_id
        self.tool_name = tool_name
        self.input_schema = input_schema
        self.client = MCPClient()

    def call(self, input_data: dict[str, Any], context: ToolContext) -> ToolResult:
        server = context.session.get(MCPServer, self.server_id)
        if server is None:
            raise ValueError(f"MCP server not found for tool {self.name}")
        started = time.perf_counter()
        call = MCPToolCall(
            id=str(uuid4()),
            run_id=context.run_id,
            step_id=context.step_id,
            server_name=server.name,
            tool_name=self.tool_name,
            input_json=json.dumps(input_data, ensure_ascii=False, sort_keys=True),
            output_json="{}",
            status="running",
        )
        context.session.add(call)
        try:
            result = self.client.call_tool(server, self.tool_name, input_data)
            call.status = "completed"
            call.output_json = json.dumps(result.data, ensure_ascii=False, sort_keys=True)
            call.latency_ms = int((time.perf_counter() - started) * 1000)
            return ToolResult(content=result.content, data=result.data)
        except Exception as exc:
            call.status = "failed"
            call.error = str(exc)
            call.latency_ms = int((time.perf_counter() - started) * 1000)
            raise
