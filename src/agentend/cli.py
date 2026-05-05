from pathlib import Path
import shutil
from typing import Optional

import typer
from sqlalchemy import select

from agentend.config import load_config, set_llm_config
from agentend.core.conversation import ConversationService
from agentend.core.init import initialize_home
from agentend.core.llm_router import LLMRouter
from agentend.core.profile import load_agent_profile
from agentend.core.workflow_registry import WorkflowRegistry
from agentend.core.workflow_runner import WorkflowRunFailed, WorkflowRunner
from agentend.core.events import record_event
from agentend.mcp.manager import MCPManager
from agentend.telegram_bot import serve_telegram
from agentend.db.models import EventLog, Message, Run
from agentend.db.session import init_database, session_scope
from agentend.db.session import database_path

app = typer.Typer(
    help="AgentEnd Lite local single-agent workflow runtime.",
    no_args_is_help=True,
)
db_app = typer.Typer(help="Database commands.", no_args_is_help=True)
runs_app = typer.Typer(help="Run inspection commands.", no_args_is_help=True)
llm_app = typer.Typer(help="LLM configuration commands.", no_args_is_help=True)
agent_app = typer.Typer(help="Agent profile commands.", no_args_is_help=True)
workflows_app = typer.Typer(help="Workflow commands.", no_args_is_help=True)
mcp_app = typer.Typer(help="MCP server commands.", no_args_is_help=True)
telegram_app = typer.Typer(help="Telegram bot commands.", no_args_is_help=True)
logs_app = typer.Typer(help="Log inspection commands.", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(runs_app, name="runs")
app.add_typer(llm_app, name="llm")
app.add_typer(agent_app, name="agent")
app.add_typer(workflows_app, name="workflows")
app.add_typer(mcp_app, name="mcp")
app.add_typer(telegram_app, name="telegram")
app.add_typer(logs_app, name="logs")


@app.callback()
def root() -> None:
    """AgentEnd Lite local single-agent workflow runtime."""


@app.command()
def init(
    home: Optional[Path] = typer.Option(
        None,
        "--home",
        "-H",
        help="AgentEnd home directory. Defaults to the current directory.",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite managed template files."),
) -> None:
    """Initialize local configuration, data directories, and starter workflow files."""
    result = initialize_home(home or Path.cwd(), force=force)
    typer.echo(f"Initialized AgentEnd Lite home: {result.home}")


@app.command()
def status(
    home: Optional[Path] = typer.Option(
        None,
        "--home",
        "-H",
        help="AgentEnd home directory. Defaults to the current directory.",
    ),
) -> None:
    """Show local AgentEnd configuration status."""
    resolved_home = (home or Path.cwd()).expanduser().resolve()
    config = load_config(resolved_home)
    profile = load_agent_profile(config)
    typer.echo(f"Home: {resolved_home}")
    typer.echo(f"Database: {database_path(resolved_home)}")
    typer.echo(f"LLM: {config.llm.provider}/{config.llm.model}")
    typer.echo(f"Agent profile: {profile.path}")
    typer.echo(f"Agent profile hash: {profile.digest}")


@db_app.command("init")
def db_init(
    home: Optional[Path] = typer.Option(
        None,
        "--home",
        "-H",
        help="AgentEnd home directory. Defaults to the current directory.",
    ),
) -> None:
    """Create or migrate the local SQLite database."""
    path = init_database(home or Path.cwd())
    typer.echo(f"Initialized database: {path}")


@db_app.command("backup")
def db_backup(
    output: Path = typer.Option(..., "--output", "-o", help="Backup SQLite file path."),
    home: Optional[Path] = typer.Option(
        None,
        "--home",
        "-H",
        help="AgentEnd home directory. Defaults to the current directory.",
    ),
) -> None:
    """Copy the local SQLite database to a backup file."""
    source = database_path(home or Path.cwd())
    if not source.exists():
        init_database(home or Path.cwd())
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, output)
    typer.echo(f"Backed up database: {output}")


@app.command()
def chat(
    home: Optional[Path] = typer.Option(
        None,
        "--home",
        "-H",
        help="AgentEnd home directory. Defaults to the current directory.",
    ),
    message: Optional[str] = typer.Option(None, "--message", "-m", help="Send one message and exit."),
) -> None:
    """Start a local CLI conversation."""
    service = ConversationService(home or Path.cwd())
    if message is not None:
        response = service.handle_message("cli", "local", message)
        typer.echo(f"Run: {response.run_id}")
        typer.echo(response.content)
        return

    typer.echo("AgentEnd chat. Type /exit to quit.")
    while True:
        text = typer.prompt("You")
        if text.strip() in {"/exit", "exit", "quit"}:
            break
        response = service.handle_message("cli", "local", text)
        typer.echo(f"Run: {response.run_id}")
        typer.echo(response.content)


@llm_app.command("list")
def llm_list(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """List configured LLM providers."""
    config = load_config(home or Path.cwd())
    typer.echo(f"* {config.llm.provider}  model={config.llm.model}")


@llm_app.command("set")
def llm_set(
    provider: str = typer.Option(..., "--provider", help="Provider name."),
    model: str = typer.Option(..., "--model", help="Model name."),
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Set the active LLM provider and model."""
    config = set_llm_config(home or Path.cwd(), provider=provider, model=model)
    typer.echo(f"LLM set: {config.llm.provider}/{config.llm.model}")


@llm_app.command("current")
def llm_current(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Show the active LLM provider and model."""
    config = load_config(home or Path.cwd())
    typer.echo(f"Provider: {config.llm.provider}")
    typer.echo(f"Model: {config.llm.model}")
    typer.echo(f"API key env: {config.llm.provider_config.api_key_env}")


@llm_app.command("test")
def llm_test(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Validate the active LLM configuration."""
    result = LLMRouter(load_config(home or Path.cwd())).test()
    typer.echo(result.message)
    if not result.ok:
        raise typer.Exit(1)


@agent_app.command("show")
def agent_show(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Print the current local agent profile."""
    profile = load_agent_profile(load_config(home or Path.cwd()))
    typer.echo(profile.content)


@agent_app.command("reload")
def agent_reload(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Reload and print the current agent profile hash."""
    profile = load_agent_profile(load_config(home or Path.cwd()))
    typer.echo(f"Agent profile: {profile.path}")
    typer.echo(f"Hash: {profile.digest}")


@agent_app.command("edit")
def agent_edit(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Open the agent profile in the configured editor."""
    import os
    import subprocess

    profile = load_agent_profile(load_config(home or Path.cwd()))
    editor = os.environ.get("EDITOR") or ("notepad" if os.name == "nt" else "vi")
    subprocess.run([editor, str(profile.path)], check=False)


@workflows_app.command("list")
def workflows_list(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """List valid workflows."""
    registry = WorkflowRegistry(load_config(home or Path.cwd()))
    workflows, errors = registry.list_workflows()
    for workflow in workflows:
        typer.echo(f"{workflow.id}  {workflow.name}")
    for error in errors:
        typer.echo(f"ERROR {error.path.name}: {error.message}")


@workflows_app.command("show")
def workflows_show(
    workflow_id: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Show one workflow definition."""
    workflow = WorkflowRegistry(load_config(home or Path.cwd())).get(workflow_id)
    typer.echo(workflow.model_dump_json(indent=2))


@workflows_app.command("validate")
def workflows_validate(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Validate all workflow YAML files."""
    workflows, errors = WorkflowRegistry(load_config(home or Path.cwd())).list_workflows()
    for workflow in workflows:
        typer.echo(f"OK {workflow.id}")
    if errors:
        for error in errors:
            typer.echo(f"ERROR {error.path.name}: {error.message}")
        raise typer.Exit(1)


@workflows_app.command("run")
def workflows_run(
    workflow_id: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
    input_text: str = typer.Option("", "--input", help="Workflow input text."),
) -> None:
    """Run a workflow by id."""
    registry = WorkflowRegistry(load_config(home or Path.cwd()))
    workflow = registry.get(workflow_id)
    try:
        result = WorkflowRunner(home or Path.cwd()).run(workflow, input_text)
        typer.echo(f"Run: {result.run_id}")
        typer.echo(result.output)
    except WorkflowRunFailed as exc:
        typer.echo(f"Run: {exc.run_id}")
        typer.echo(f"Error: {exc.message}")
        raise typer.Exit(1) from exc


@mcp_app.command("add")
def mcp_add(
    name: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
    stdio: Optional[str] = typer.Option(None, "--stdio", help="stdio server command."),
    http: Optional[str] = typer.Option(None, "--http", help="streamable HTTP MCP URL."),
) -> None:
    """Add or update an MCP server."""
    manager = MCPManager(home or Path.cwd())
    if stdio:
        server = manager.add_stdio_server(name, stdio)
    elif http:
        server = manager.add_http_server(name, http)
    else:
        raise typer.BadParameter("Provide either --stdio or --http")
    typer.echo(f"Added MCP server: {server.name} ({server.transport})")


@mcp_app.command("list")
def mcp_list(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """List MCP servers."""
    servers = MCPManager(home or Path.cwd()).list_servers()
    for server in servers:
        typer.echo(f"{server.name}  {server.transport}  {server.status}")


@mcp_app.command("refresh")
def mcp_refresh(
    name: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Refresh and register tools from one MCP server."""
    names = MCPManager(home or Path.cwd()).refresh(name)
    for local_name in names:
        typer.echo(local_name)


@mcp_app.command("tools")
def mcp_tools(
    name: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """List registered tools for one MCP server."""
    tools = MCPManager(home or Path.cwd()).list_tools(name)
    for tool in tools:
        typer.echo(f"{tool.local_name}  enabled={tool.enabled}")


@mcp_app.command("test")
def mcp_test(
    name: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Test one MCP server by refreshing its tools."""
    count = MCPManager(home or Path.cwd()).test(name)
    typer.echo(f"MCP server {name} ok; tools={count}")


@mcp_app.command("remove")
def mcp_remove(
    name: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Remove an MCP server and its registered tools."""
    MCPManager(home or Path.cwd()).remove(name)
    typer.echo(f"Removed MCP server: {name}")


@telegram_app.command("serve")
def telegram_serve(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Run the Telegram Bot with long polling."""
    try:
        serve_telegram(home or Path.cwd())
    except RuntimeError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc


@runs_app.command("list")
def runs_list(
    home: Optional[Path] = typer.Option(
        None,
        "--home",
        "-H",
        help="AgentEnd home directory. Defaults to the current directory.",
    ),
) -> None:
    """List recent runs."""
    with session_scope(home or Path.cwd()) as session:
        runs = session.execute(select(Run).order_by(Run.created_at.desc())).scalars().all()
        if not runs:
            typer.echo("No runs.")
            return
        for run in runs:
            typer.echo(f"{run.id}  {run.status}  workflow={run.workflow_id or '-'}")


@runs_app.command("show")
def runs_show(
    run_id: str,
    home: Optional[Path] = typer.Option(
        None,
        "--home",
        "-H",
        help="AgentEnd home directory. Defaults to the current directory.",
    ),
) -> None:
    """Show one run and its conversation messages."""
    with session_scope(home or Path.cwd()) as session:
        run = session.get(Run, run_id)
        if run is None:
            raise typer.BadParameter(f"Unknown run: {run_id}")
        typer.echo(f"Run: {run.id}")
        typer.echo(f"Status: {run.status}")
        typer.echo(f"Workflow: {run.workflow_id or '-'}")
        if run.error:
            typer.echo(f"Error: {run.error}")
        typer.echo(f"Result: {run.result_json}")
        messages = session.execute(
            select(Message).where(Message.conversation_id == run.conversation_id).order_by(Message.created_at)
        ).scalars()
        for message_row in messages:
            typer.echo(f"{message_row.role}: {message_row.content}")


@runs_app.command("resume")
def runs_resume(
    run_id: str,
    message: str = typer.Option(..., "--message", "-m", help="Message to resume a waiting run."),
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Resume a run waiting for user input."""
    import json

    with session_scope(home or Path.cwd()) as session:
        run = session.get(Run, run_id)
        if run is None:
            raise typer.BadParameter(f"Unknown run: {run_id}")
        if run.status != "waiting_input":
            raise typer.BadParameter(f"Run is not waiting for input: {run.status}")
        run.status = "completed"
        run.result_json = json.dumps({"content": message}, ensure_ascii=False)
        record_event(session, "run.state_changed", {"status": "completed"}, run_id=run.id)
        record_event(session, "run.completed", {"resumed": True}, run_id=run.id)
        typer.echo(f"Run: {run.id}")
        typer.echo("Status: completed")
        typer.echo(message)


@runs_app.command("cancel")
def runs_cancel(
    run_id: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Cancel a run."""
    with session_scope(home or Path.cwd()) as session:
        run = session.get(Run, run_id)
        if run is None:
            raise typer.BadParameter(f"Unknown run: {run_id}")
        run.status = "cancelled"
        record_event(session, "run.cancelled", {}, run_id=run.id)
        typer.echo(f"Run: {run.id}")
        typer.echo("Status: cancelled")


@logs_app.command("tail")
def logs_tail(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
    limit: int = typer.Option(20, "--limit", "-n", help="Number of events to show."),
) -> None:
    """Show recent event log rows."""
    with session_scope(home or Path.cwd()) as session:
        rows = (
            session.execute(select(EventLog).order_by(EventLog.created_at.desc()).limit(limit))
            .scalars()
            .all()
        )
        for row in reversed(rows):
            typer.echo(f"{row.created_at.isoformat()}  {row.event_type}  run={row.run_id or '-'}")


def main() -> None:
    app()
