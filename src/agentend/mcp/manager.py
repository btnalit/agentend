import json
import shlex
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from agentend.core.events import record_event
from agentend.db.models import MCPServer, MCPTool
from agentend.db.session import init_database, session_scope
from agentend.mcp.client import MCPClient
from agentend.mcp.schemas import DiscoveredMCPTool


class MCPManager:
    def __init__(self, home: Path, client: MCPClient | None = None):
        self.home = home.expanduser().resolve()
        self.client = client or MCPClient()
        init_database(self.home)

    def add_stdio_server(self, name: str, stdio: str) -> MCPServer:
        parts = shlex.split(stdio, posix=False)
        if not parts:
            raise ValueError("stdio command cannot be empty")
        return self._upsert_server(name=name, transport="stdio", command=parts[0], args=parts[1:], url=None)

    def add_http_server(self, name: str, url: str) -> MCPServer:
        return self._upsert_server(name=name, transport="http", command=None, args=[], url=url)

    def list_servers(self) -> list[MCPServer]:
        with session_scope(self.home) as session:
            return list(session.execute(select(MCPServer).order_by(MCPServer.name)).scalars().all())

    def refresh(self, name: str) -> list[str]:
        with session_scope(self.home) as session:
            server = self._get_server(session, name)
            try:
                discovered = self.client.list_tools(server)
                server.status = "healthy"
                server.last_error = None
                local_names = self._upsert_tools(session, server, discovered)
                record_event(session, "mcp.server_refreshed", {"server": name, "tool_count": len(local_names)})
                return local_names
            except Exception as exc:
                server.status = "unhealthy"
                server.last_error = str(exc)
                raise

    def list_tools(self, name: str) -> list[MCPTool]:
        with session_scope(self.home) as session:
            server = self._get_server(session, name)
            return list(session.execute(select(MCPTool).where(MCPTool.server_id == server.id).order_by(MCPTool.local_name)).scalars())

    def remove(self, name: str) -> None:
        with session_scope(self.home) as session:
            server = self._get_server(session, name)
            tools = session.execute(select(MCPTool).where(MCPTool.server_id == server.id)).scalars().all()
            for tool in tools:
                session.delete(tool)
            session.delete(server)

    def test(self, name: str) -> int:
        return len(self.refresh(name))

    def _upsert_server(self, name: str, transport: str, command: str | None, args: list[str], url: str | None) -> MCPServer:
        with session_scope(self.home) as session:
            existing = session.execute(select(MCPServer).where(MCPServer.name == name)).scalar_one_or_none()
            if existing is None:
                existing = MCPServer(
                    id=str(uuid4()),
                    name=name,
                    transport=transport,
                    command=command,
                    args_json=json.dumps(args),
                    url=url,
                    status="unknown",
                )
                session.add(existing)
            else:
                existing.transport = transport
                existing.command = command
                existing.args_json = json.dumps(args)
                existing.url = url
                existing.enabled = "true"
            return existing

    def _upsert_tools(self, session, server: MCPServer, discovered: list[DiscoveredMCPTool]) -> list[str]:
        local_names: list[str] = []
        for tool in discovered:
            local_name = f"mcp.{server.name}.{tool.name}"
            existing = session.execute(select(MCPTool).where(MCPTool.local_name == local_name)).scalar_one_or_none()
            if existing is None:
                existing = MCPTool(
                    id=str(uuid4()),
                    server_id=server.id,
                    name=tool.name,
                    local_name=local_name,
                    description=tool.description,
                    input_schema_json=json.dumps(tool.input_schema, ensure_ascii=False, sort_keys=True),
                    enabled="true",
                )
                session.add(existing)
            else:
                existing.description = tool.description
                existing.input_schema_json = json.dumps(tool.input_schema, ensure_ascii=False, sort_keys=True)
                existing.enabled = "true"
            local_names.append(local_name)
            record_event(session, "mcp.tool_registered", {"server": server.name, "tool": tool.name})
        return local_names

    def _get_server(self, session, name: str) -> MCPServer:
        server = session.execute(select(MCPServer).where(MCPServer.name == name)).scalar_one_or_none()
        if server is None:
            raise ValueError(f"Unknown MCP server: {name}")
        return server
