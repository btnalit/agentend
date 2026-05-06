from __future__ import annotations

import json
import subprocess
from pathlib import Path

from agentend.tools.base import ToolContext, ToolResult


def _cwd(context: ToolContext, input_data: dict) -> Path:
    path = Path(str(input_data.get("cwd", context.home)))
    return path if path.is_absolute() else (context.home / path).resolve()


def _run(args: list[str], cwd: Path) -> ToolResult:
    completed = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    data = {"stdout": completed.stdout, "stderr": completed.stderr, "exit_code": completed.returncode, "cwd": str(cwd)}
    return ToolResult(content=json.dumps(data, ensure_ascii=False, sort_keys=True), data=data)


class GitStatusTool:
    name = "git.status"
    description = "Show git status."
    input_schema = {"type": "object"}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        return _run(["status", "--short", "--branch"], _cwd(context, input_data))


class GitDiffTool:
    name = "git.diff"
    description = "Show git diff."
    input_schema = {"type": "object"}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        args = ["diff"]
        if input_data.get("path"):
            args.extend(["--", str(input_data["path"])])
        return _run(args, _cwd(context, input_data))


class GitShowTool:
    name = "git.show"
    description = "Show a git revision."
    input_schema = {"type": "object"}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        return _run(["show", str(input_data.get("rev", "HEAD"))], _cwd(context, input_data))


class GitLogTool:
    name = "git.log"
    description = "Show git log."
    input_schema = {"type": "object"}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        limit = str(int(input_data.get("limit", 10)))
        return _run(["log", "--oneline", "-n", limit], _cwd(context, input_data))


class GitBranchTool:
    name = "git.branch"
    description = "Show git branch."
    input_schema = {"type": "object"}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        return _run(["branch", "--show-current"], _cwd(context, input_data))


class GitCommitTool:
    name = "git.commit"
    description = "Create a git commit from an explicit file list."
    input_schema = {"type": "object", "required": ["message", "files"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        cwd = _cwd(context, input_data)
        files = [str(item) for item in input_data.get("files", [])]
        if not files:
            raise ValueError("files must contain at least one path")
        add = subprocess.run(["git", "add", "--", *files], cwd=cwd, capture_output=True, text=True)
        if add.returncode != 0:
            data = {"stdout": add.stdout, "stderr": add.stderr, "exit_code": add.returncode, "cwd": str(cwd)}
            return ToolResult(content=json.dumps(data, ensure_ascii=False, sort_keys=True), data=data)
        completed = subprocess.run(
            [
                "git",
                "-c",
                "user.name=AgentEnd",
                "-c",
                "user.email=agentend@example.local",
                "commit",
                "-m",
                str(input_data["message"]),
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
        )
        data = {"stdout": completed.stdout, "stderr": completed.stderr, "exit_code": completed.returncode, "cwd": str(cwd)}
        return ToolResult(content=json.dumps(data, ensure_ascii=False, sort_keys=True), data=data)


GIT_TOOLS = [GitStatusTool(), GitDiffTool(), GitShowTool(), GitLogTool(), GitBranchTool(), GitCommitTool()]
