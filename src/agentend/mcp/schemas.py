from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DiscoveredMCPTool:
    name: str
    description: str | None
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class MCPCallResult:
    content: str
    data: dict[str, Any]
