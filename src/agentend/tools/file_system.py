from __future__ import annotations

import json
import shutil
from pathlib import Path

from agentend.tools.base import ToolContext, ToolResult


def _resolve(home: Path, value: object) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else (home / path).resolve()


class FsListTool:
    name = "fs.list"
    description = "List files in a local directory."
    input_schema = {"type": "object", "required": ["path"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        path = _resolve(context.home, input_data["path"])
        entries = sorted(child.name for child in path.iterdir())
        return ToolResult(content="\n".join(entries), data={"path": str(path), "entries": entries})


class FsGlobTool:
    name = "fs.glob"
    description = "Glob local files under the AgentEnd home."
    input_schema = {"type": "object", "required": ["pattern"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        matches = sorted(str(path.relative_to(context.home)) for path in context.home.glob(str(input_data["pattern"])))
        return ToolResult(content="\n".join(matches), data={"matches": matches})


class FsStatTool:
    name = "fs.stat"
    description = "Stat a local file or directory."
    input_schema = {"type": "object", "required": ["path"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        path = _resolve(context.home, input_data["path"])
        stat = path.stat()
        data = {"path": str(path), "is_dir": path.is_dir(), "size_bytes": stat.st_size}
        return ToolResult(content=json.dumps(data, ensure_ascii=False, sort_keys=True), data=data)


class FsReadTextTool:
    name = "fs.read_text"
    description = "Read a UTF-8 text file."
    input_schema = {"type": "object", "required": ["path"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        path = _resolve(context.home, input_data["path"])
        content = path.read_text(encoding="utf-8")
        return ToolResult(content=content, data={"path": str(path)})


class FsWriteTextTool:
    name = "fs.write_text"
    description = "Write a UTF-8 text file."
    input_schema = {"type": "object", "required": ["path", "content"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        path = _resolve(context.home, input_data["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        content = str(input_data.get("content", ""))
        path.write_text(content, encoding="utf-8")
        return ToolResult(content=content, data={"path": str(path), "size_bytes": path.stat().st_size}, artifact_path=path)


class FsCopyTool:
    name = "fs.copy"
    description = "Copy a local file."
    input_schema = {"type": "object", "required": ["src", "dst"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        src = _resolve(context.home, input_data["src"])
        dst = _resolve(context.home, input_data["dst"])
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return ToolResult(content=str(dst), data={"src": str(src), "dst": str(dst)})


class FsMoveTool:
    name = "fs.move"
    description = "Move a local file."
    input_schema = {"type": "object", "required": ["src", "dst"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        src = _resolve(context.home, input_data["src"])
        dst = _resolve(context.home, input_data["dst"])
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return ToolResult(content=str(dst), data={"src": str(src), "dst": str(dst)})


class FsDeleteTool:
    name = "fs.delete"
    description = "Delete a local file or an explicitly recursive directory."
    input_schema = {"type": "object", "required": ["path"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        path = _resolve(context.home, input_data["path"])
        if path.is_dir():
            if not bool(input_data.get("recursive", False)):
                raise ValueError("recursive=true is required to delete a directory")
            shutil.rmtree(path)
        else:
            path.unlink()
        return ToolResult(content=str(path), data={"path": str(path), "deleted": True})


class FsMkdirTool:
    name = "fs.mkdir"
    description = "Create a local directory."
    input_schema = {"type": "object", "required": ["path"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        path = _resolve(context.home, input_data["path"])
        path.mkdir(parents=True, exist_ok=True)
        return ToolResult(content=str(path), data={"path": str(path)})


FS_TOOLS = [
    FsListTool(),
    FsGlobTool(),
    FsStatTool(),
    FsReadTextTool(),
    FsWriteTextTool(),
    FsCopyTool(),
    FsMoveTool(),
    FsDeleteTool(),
    FsMkdirTool(),
]
