import contextlib
import io

from agentend.tools.base import ToolContext, ToolResult


class PythonExecTool:
    name = "python.exec"
    description = "Execute a small Python snippet with restricted builtins."
    input_schema = {"type": "object", "required": ["code"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        code = str(input_data["code"])
        stdout = io.StringIO()
        safe_builtins = {"len": len, "range": range, "str": str, "int": int, "float": float, "print": print}
        globals_dict = {"__builtins__": safe_builtins}
        with contextlib.redirect_stdout(stdout):
            exec(code, globals_dict, {})
        content = stdout.getvalue()
        return ToolResult(content=content, data={"stdout": content})
