from __future__ import annotations

import asyncio
import json
from typing import Any

from agentend.db.models import MCPServer
from agentend.mcp.schemas import DiscoveredMCPTool, MCPCallResult


class MCPClient:
    def list_tools(self, server: MCPServer) -> list[DiscoveredMCPTool]:
        return asyncio.run(self._list_tools(server))

    def call_tool(self, server: MCPServer, tool_name: str, arguments: dict[str, Any]) -> MCPCallResult:
        return asyncio.run(self._call_tool(server, tool_name, arguments))

    async def _list_tools(self, server: MCPServer) -> list[DiscoveredMCPTool]:
        if server.transport == "stdio" and server.command == "mock:echo":
            return [
                DiscoveredMCPTool(
                    name="echo",
                    description="Mock echo MCP tool.",
                    input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
                )
            ]

        if server.transport == "stdio":
            return await self._list_stdio_tools(server)
        if server.transport == "http":
            return await self._list_http_tools(server)
        raise ValueError(f"Unsupported MCP transport: {server.transport}")

    async def _call_tool(self, server: MCPServer, tool_name: str, arguments: dict[str, Any]) -> MCPCallResult:
        if server.transport == "stdio" and server.command == "mock:echo":
            text = str(arguments.get("text", ""))
            return MCPCallResult(content=text, data={"content": text})

        if server.transport == "stdio":
            return await self._call_stdio_tool(server, tool_name, arguments)
        if server.transport == "http":
            return await self._call_http_tool(server, tool_name, arguments)
        raise ValueError(f"Unsupported MCP transport: {server.transport}")

    async def _list_stdio_tools(self, server: MCPServer) -> list[DiscoveredMCPTool]:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:
            raise RuntimeError("Install the mcp package to use real stdio MCP servers") from exc

        params = StdioServerParameters(command=str(server.command), args=json.loads(server.args_json))
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                response = await session.list_tools()
                return [self._normalize_tool(tool) for tool in response.tools]

    async def _call_stdio_tool(self, server: MCPServer, tool_name: str, arguments: dict[str, Any]) -> MCPCallResult:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:
            raise RuntimeError("Install the mcp package to use real stdio MCP servers") from exc

        params = StdioServerParameters(command=str(server.command), args=json.loads(server.args_json))
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments=arguments)
                return self._normalize_call_result(result)

    async def _list_http_tools(self, server: MCPServer) -> list[DiscoveredMCPTool]:
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client
        except ImportError as exc:
            raise RuntimeError("Install the mcp package to use streamable HTTP MCP servers") from exc

        async with streamable_http_client(str(server.url)) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                response = await session.list_tools()
                return [self._normalize_tool(tool) for tool in response.tools]

    async def _call_http_tool(self, server: MCPServer, tool_name: str, arguments: dict[str, Any]) -> MCPCallResult:
        try:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client
        except ImportError as exc:
            raise RuntimeError("Install the mcp package to use streamable HTTP MCP servers") from exc

        async with streamable_http_client(str(server.url)) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments=arguments)
                return self._normalize_call_result(result)

    def _normalize_tool(self, tool: Any) -> DiscoveredMCPTool:
        schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {}
        return DiscoveredMCPTool(
            name=str(getattr(tool, "name")),
            description=getattr(tool, "description", None),
            input_schema=schema,
        )

    def _normalize_call_result(self, result: Any) -> MCPCallResult:
        structured = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None) or {}
        texts: list[str] = []
        for item in getattr(result, "content", []) or []:
            text = getattr(item, "text", None)
            if text is not None:
                texts.append(str(text))
        content = "\n".join(texts) if texts else json.dumps(structured, ensure_ascii=False)
        return MCPCallResult(content=content, data={"content": content, "structured": structured})
