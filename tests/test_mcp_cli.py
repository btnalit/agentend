from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import MCPTool, MCPToolCall
from agentend.db.session import session_scope


def test_mcp_server_refresh_registers_tools_and_workflow_can_call_them(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()

    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    add = runner.invoke(app, ["mcp", "add", "demo", "--home", str(home), "--stdio", "mock:echo"])
    refresh = runner.invoke(app, ["mcp", "refresh", "demo", "--home", str(home)])
    tools = runner.invoke(app, ["mcp", "tools", "demo", "--home", str(home)])

    assert add.exit_code == 0
    assert refresh.exit_code == 0
    assert tools.exit_code == 0
    assert "mcp.demo.echo" in tools.output

    workflow_path = home / "workflows" / "definitions" / "mcp_demo.yaml"
    workflow_path.write_text(
        """id: mcp_demo
name: MCP Demo
nodes:
  - id: echo
    type: tool
    tool: mcp.demo.echo
    input:
      text: "MCP says {input}"
  - id: final
    type: final
    depends_on: [echo]
""",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["workflows", "run", "mcp_demo", "--home", str(home), "--input", "hello"])

    assert result.exit_code == 0
    assert "MCP says hello" in result.output
    with session_scope(home) as session:
        tool = session.execute(select(MCPTool).where(MCPTool.local_name == "mcp.demo.echo")).scalar_one()
        call = session.execute(select(MCPToolCall).where(MCPToolCall.tool_name == "echo")).scalar_one()
        assert tool.enabled == "true"
        assert call.status == "completed"
