from __future__ import annotations

import json
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from agentend.db.models import Artifact
from agentend.tools.base import ToolContext, ToolResult


class PythonExecTool:
    name = "python.exec"
    description = "Execute Python code in a local subprocess workspace."
    input_schema = {"type": "object", "required": ["code"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        code = str(input_data["code"])
        workspace = context.home / "data" / "sandboxes" / context.run_id / str(uuid4())
        workspace.mkdir(parents=True, exist_ok=True)
        script = workspace / "script.py"
        script.write_text(code, encoding="utf-8")
        timeout = int(input_data.get("timeout_seconds", 120))
        timed_out = False
        try:
            completed = subprocess.run(
                [sys.executable, str(script)],
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            stdout = completed.stdout
            stderr = completed.stderr
            exit_code = completed.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or "timeout"
            exit_code = -1
            timed_out = True

        artifacts = []
        for path in workspace.rglob("*"):
            if not path.is_file() or path == script:
                continue
            digest = sha256(path.read_bytes()).hexdigest()
            artifacts.append(str(path))
            context.session.add(
                Artifact(
                    id=str(uuid4()),
                    run_id=context.run_id,
                    path=str(path),
                    kind="file",
                    mime="application/octet-stream",
                    size_bytes=path.stat().st_size,
                    sha256=digest,
                    metadata_json=json.dumps({"tool": self.name, "workspace": str(workspace)}, ensure_ascii=False),
                )
            )
        data = {
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "workspace": str(workspace),
            "artifacts": artifacts,
        }
        return ToolResult(content=stdout if stdout else json.dumps(data, ensure_ascii=False, sort_keys=True), data=data)
