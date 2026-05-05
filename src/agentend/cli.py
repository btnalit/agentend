from pathlib import Path
from typing import Optional

import typer
from sqlalchemy import select

from agentend.core.conversation import ConversationService
from agentend.core.init import initialize_home
from agentend.db.models import Message, Run
from agentend.db.session import init_database, session_scope

app = typer.Typer(
    help="AgentEnd Lite local single-agent workflow runtime.",
    no_args_is_help=True,
)
db_app = typer.Typer(help="Database commands.", no_args_is_help=True)
runs_app = typer.Typer(help="Run inspection commands.", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(runs_app, name="runs")


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
        messages = session.execute(
            select(Message).where(Message.conversation_id == run.conversation_id).order_by(Message.created_at)
        ).scalars()
        for message_row in messages:
            typer.echo(f"{message_row.role}: {message_row.content}")


def main() -> None:
    app()
