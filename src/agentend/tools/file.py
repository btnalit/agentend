from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from agentend.config import load_config
from agentend.core.evidence import record_file_read_evidence
from agentend.core.paths import resolve_home_child
from agentend.tools.base import ToolContext, ToolResult


class ReadTextTool:
    name = "file.read_text"
    description = "Read a UTF-8 text file."
    input_schema = {"type": "object", "required": ["path"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        path = resolve_home_child(context.home, input_data["path"])
        content = path.read_text(encoding="utf-8")
        source = record_file_read_evidence(context.session, context.home, run_id=context.run_id, path=path, text=content)
        return ToolResult(content=content, data={"path": str(path), "source_id": source.id})


class WriteTextTool:
    name = "file.write_text"
    description = "Write a UTF-8 text artifact under the run artifact directory."
    input_schema = {"type": "object", "required": ["path", "content"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        config = load_config(context.home)
        artifact_root = config.resolve_home_path(config.data.artifact_dir) / context.run_id
        artifact_root.mkdir(parents=True, exist_ok=True)
        relative = Path(str(input_data["path"]))
        if relative.is_absolute():
            raise ValueError("path must be relative to the run artifact directory")
        if ".." in relative.parts:
            raise ValueError("path must not contain '..'")
        path = (artifact_root / relative).resolve()
        if artifact_root.resolve() not in [path.resolve(), *path.resolve().parents]:
            raise ValueError("path must stay inside the run artifact directory")
        path.parent.mkdir(parents=True, exist_ok=True)
        content = str(input_data.get("content", ""))
        path.write_text(content, encoding="utf-8")
        digest = sha256(path.read_bytes()).hexdigest()
        return ToolResult(
            content=content,
            data={"path": str(path), "sha256": digest, "size_bytes": path.stat().st_size},
            artifact_path=path,
        )
