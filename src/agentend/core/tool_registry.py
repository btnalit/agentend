import json
import time
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from agentend.core.events import record_event
from agentend.db.models import Artifact, MCPTool, ToolCall
from agentend.db.session import session_scope
from agentend.mcp.adapter import MCPRegisteredTool
from agentend.tools.base import Tool, ToolContext, ToolResult
from agentend.tools.file import ReadTextTool, WriteTextTool
from agentend.tools.http import HttpRequestTool
from agentend.tools.memory import MemorySearchTool, MemoryWriteTool
from agentend.tools.python_exec import PythonExecTool


class ToolRegistry:
    def __init__(self, home: Path | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in [
            ReadTextTool(),
            WriteTextTool(),
            HttpRequestTool(),
            PythonExecTool(),
            MemorySearchTool(),
            MemoryWriteTool(),
        ]:
            self.register(tool)
        if home is not None:
            self._register_mcp_tools(home)

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return sorted(self._tools)

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise ValueError(f"Unknown tool: {name}")
        return self._tools[name]

    def call(self, name: str, input_data: dict[str, Any], context: ToolContext) -> ToolResult:
        started = time.perf_counter()
        call = ToolCall(
            id=str(uuid4()),
            run_id=context.run_id,
            step_id=context.step_id,
            tool_name=name,
            input_json=json.dumps(input_data, ensure_ascii=False, sort_keys=True),
            output_json="{}",
            status="running",
        )
        context.session.add(call)
        record_event(context.session, "tool.called", {"tool_name": name}, run_id=context.run_id)
        try:
            result = self.get(name).call(input_data, context)
            call.status = "completed"
            call.output_json = json.dumps(result.data | {"content": result.content}, ensure_ascii=False, sort_keys=True)
            call.latency_ms = int((time.perf_counter() - started) * 1000)
            if result.artifact_path is not None:
                self._record_artifact(result.artifact_path, result, context)
            return result
        except Exception as exc:
            call.status = "failed"
            call.error = str(exc)
            call.latency_ms = int((time.perf_counter() - started) * 1000)
            raise

    def _record_artifact(self, path: Path, result: ToolResult, context: ToolContext) -> None:
        digest = result.data.get("sha256")
        if digest is None and path.exists():
            digest = sha256(path.read_bytes()).hexdigest()
        context.session.add(
            Artifact(
                id=str(uuid4()),
                run_id=context.run_id,
                path=str(path),
                kind="file",
                mime="text/plain",
                size_bytes=int(result.data.get("size_bytes", path.stat().st_size if path.exists() else 0)),
                sha256=digest,
                metadata_json=json.dumps(result.data, ensure_ascii=False, sort_keys=True),
            )
        )

    def _register_mcp_tools(self, home: Path) -> None:
        with session_scope(home) as session:
            rows = session.execute(select(MCPTool).where(MCPTool.enabled == "true")).scalars().all()
            for row in rows:
                self.register(
                    MCPRegisteredTool(
                        local_name=row.local_name,
                        server_id=row.server_id,
                        tool_name=row.name,
                        input_schema=json.loads(row.input_schema_json),
                    )
                )
