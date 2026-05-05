from pathlib import Path
from typing import Optional

import typer

from agentend.core.init import initialize_home

app = typer.Typer(
    help="AgentEnd Lite local single-agent workflow runtime.",
    no_args_is_help=True,
)


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


def main() -> None:
    app()
