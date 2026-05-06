import json
import time
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from agentend.core.action_policy import record_action_decision
from agentend.core.context_runtime import compact_tool_result
from agentend.core.evidence import rehydrate_cached_evidence
from agentend.core.errors import record_error
from agentend.core.events import record_event
from agentend.core.result_cache import get_cached_result, store_cached_result
from agentend.core.secrets import redact_text
from agentend.core.tool_contracts import ToolContract, contract_for_tool, snapshot_tool_contracts, sync_tool_manifests
from agentend.db.models import Artifact, MCPTool, ToolCall, ToolManifest
from agentend.db.session import session_scope
from agentend.mcp.adapter import MCPRegisteredTool
from agentend.tools.base import Tool, ToolContext, ToolResult
from agentend.tools.browser import BROWSER_TOOLS
from agentend.tools.db import DB_TOOLS
from agentend.tools.discover import DISCOVER_TOOLS
from agentend.tools.file import ReadTextTool, WriteTextTool
from agentend.tools.file_system import FS_TOOLS
from agentend.tools.git import GIT_TOOLS
from agentend.tools.generator import GENERATOR_TOOLS
from agentend.tools.http import HttpRequestTool
from agentend.tools.im import IM_TOOLS
from agentend.tools.memory import MemorySearchTool, MemoryWriteTool
from agentend.tools.planning import PLANNING_TOOLS
from agentend.tools.python_exec import PythonExecTool
from agentend.tools.shell import ShellRunTool
from agentend.tools.vision import VISION_TOOLS
from agentend.tools.web import WebFetchTool, WebSearchTool


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
            ShellRunTool(),
            WebFetchTool(),
            WebSearchTool(),
            *BROWSER_TOOLS,
            *DB_TOOLS,
            *IM_TOOLS,
            *VISION_TOOLS,
            *FS_TOOLS,
            *GIT_TOOLS,
            *DISCOVER_TOOLS,
            *PLANNING_TOOLS,
            *GENERATOR_TOOLS,
        ]:
            self.register(tool)
        if home is not None:
            self._register_mcp_tools(home)

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return sorted(self._tools)

    def manifests(self) -> list[ToolContract]:
        contracts: list[ToolContract] = []
        for tool in self._tools.values():
            source = "mcp" if tool.name.startswith("mcp.") else "builtin"
            contracts.append(contract_for_tool(tool, source=source))
        return sorted(contracts, key=lambda item: item.name)

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise ValueError(f"Unknown tool: {name}")
        return self._tools[name]

    def call(self, name: str, input_data: dict[str, Any], context: ToolContext) -> ToolResult:
        started = time.perf_counter()
        contracts = self.manifests()
        sync_tool_manifests(context.session, contracts)
        snapshot_tool_contracts(context.session, context.run_id, contracts)
        manifest = context.session.get(ToolManifest, name)
        if manifest is not None and manifest.enabled != "true":
            raise PermissionError(f"Tool is disabled: {name}")
        call = ToolCall(
            id=str(uuid4()),
            run_id=context.run_id,
            step_id=context.step_id,
            tool_name=name,
            input_json=redact_text(context.home, json.dumps(input_data, ensure_ascii=False, sort_keys=True)),
            output_json="{}",
            status="running",
        )
        context.session.add(call)
        record_event(context.session, "tool.called", {"tool_name": name}, run_id=context.run_id)
        try:
            if manifest is not None:
                record_action_decision(
                    context.session,
                    run_id=context.run_id,
                    step_id=context.step_id,
                    tool_name=name,
                    side_effect=manifest.side_effect,
                    run_mode=context.run_mode,
                )
            cached = get_cached_result(context.session, context, name, input_data)
            result = rehydrate_cached_evidence(context.session, context, name, cached) if cached is not None else self.get(name).call(input_data, context)
            if cached is None:
                store_cached_result(context.session, context, name, input_data, result)
            call.status = "completed"
            call.output_json = redact_text(
                context.home,
                json.dumps(result.data | {"content": result.content}, ensure_ascii=False, sort_keys=True),
            )
            call.latency_ms = int((time.perf_counter() - started) * 1000)
            compact_tool_result(
                context.session,
                run_id=context.run_id,
                step_id=context.step_id,
                tool_name=name,
                result=result,
            )
            if result.artifact_path is not None:
                self._record_artifact(result.artifact_path, result, context)
            return result
        except Exception as exc:
            call.status = "failed"
            classified = record_error(context.session, exc, source="tool", run_id=context.run_id, step_id=context.step_id)
            call.error = redact_text(context.home, f"{classified.code}: {classified.message}")
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
