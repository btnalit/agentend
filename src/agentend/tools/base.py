from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy.orm import Session


@dataclass(frozen=True)
class ToolContext:
    home: Path
    run_id: str
    step_id: str | None
    session: Session


@dataclass(frozen=True)
class ToolResult:
    content: str
    data: dict[str, Any] = field(default_factory=dict)
    artifact_path: Path | None = None


class Tool(Protocol):
    name: str
    description: str
    input_schema: dict[str, Any]

    def call(self, input_data: dict[str, Any], context: ToolContext) -> ToolResult:
        ...
