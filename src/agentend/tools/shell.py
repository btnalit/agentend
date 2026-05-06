from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from agentend.tools.base import ToolContext, ToolResult


class ShellRunTool:
    name = "shell.run"
    description = "Run a local shell command."
    input_schema = {"type": "object", "required": ["command"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        command = str(input_data["command"])
        cwd_value = input_data.get("cwd", context.home)
        cwd = Path(str(cwd_value))
        if not cwd.is_absolute():
            cwd = context.home / cwd
        env = os.environ.copy()
        env.update({str(key): str(value) for key, value in dict(input_data.get("env", {})).items()})
        timeout = int(input_data.get("timeout_seconds", 120))
        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            data = {
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "exit_code": completed.returncode,
                "cwd": str(cwd),
            }
        except subprocess.TimeoutExpired as exc:
            data = {
                "stdout": exc.stdout or "",
                "stderr": exc.stderr or "timeout",
                "exit_code": -1,
                "cwd": str(cwd),
                "timed_out": True,
            }
        return ToolResult(content=json.dumps(data, ensure_ascii=False, sort_keys=True), data=data)
