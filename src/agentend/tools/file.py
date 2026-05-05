from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from agentend.config import load_config
from agentend.tools.base import ToolContext, ToolResult


class ReadTextTool:
    name = "file.read_text"
    description = "Read a UTF-8 text file."
    input_schema = {"type": "object", "required": ["path"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        path = Path(str(input_data["path"]))
        if not path.is_absolute():
            path = context.home / path
        content = path.read_text(encoding="utf-8")
        return ToolResult(content=content, data={"path": str(path)})


class WriteTextTool:
    name = "file.write_text"
    description = "Write a UTF-8 text artifact under the run artifact directory."
    input_schema = {"type": "object", "required": ["path", "content"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        config = load_config(context.home)
        artifact_root = config.resolve_home_path(config.data.artifact_dir) / context.run_id
        artifact_root.mkdir(parents=True, exist_ok=True)
        relative = Path(str(input_data["path"]))
        safe_relative = Path(relative.name) if relative.is_absolute() or ".." in relative.parts else relative
        path = (artifact_root / safe_relative).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        content = str(input_data.get("content", ""))
        path.write_text(content, encoding="utf-8")
        digest = sha256(path.read_bytes()).hexdigest()
        return ToolResult(
            content=content,
            data={"path": str(path), "sha256": digest, "size_bytes": path.stat().st_size},
            artifact_path=path,
        )
