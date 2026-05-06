from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentend.db.models import ToolContractSnapshot, ToolManifest
from agentend.tools.base import Tool


BUILTIN_SIDE_EFFECTS = {
    "file.read_text": "local_read",
    "file.write_text": "local_write",
    "http.request": "network_read",
    "python.exec": "local_execute",
    "memory.search": "local_read",
    "memory.write": "local_write",
    "shell.run": "local_execute",
    "fs.list": "local_read",
    "fs.glob": "local_read",
    "fs.stat": "local_read",
    "fs.read_text": "local_read",
    "fs.write_text": "local_write",
    "fs.copy": "local_write",
    "fs.move": "local_write",
    "fs.delete": "local_write",
    "fs.mkdir": "local_write",
    "git.status": "local_read",
    "git.diff": "local_read",
    "git.show": "local_read",
    "git.log": "local_read",
    "git.branch": "local_read",
    "git.commit": "local_write",
    "web.fetch": "network_read",
    "web.search": "network_read",
    "tools.discover": "local_read",
    "tools.describe": "local_read",
    "tools.generate": "local_write",
    "goal.analyze": "local_read",
    "plan.replan": "local_read",
    "browser.open": "network_read",
    "browser.click": "network_write",
    "browser.type": "network_write",
    "browser.screenshot": "network_read",
    "browser.extract": "network_read",
    "db.query": "local_read",
    "db.execute": "local_write",
    "db.write_rows": "local_write",
    "im.telegram.send_message": "external_write",
    "im.telegram.send_file": "external_write",
    "vision.describe": "network_read",
    "vision.ocr": "network_read",
    "vision.extract_chart": "network_read",
}

HTTP_READ_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass(frozen=True)
class ToolContract:
    name: str
    source: str
    category: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object"})
    timeout_seconds: int = 120
    side_effect: str = "none"
    retryable: bool = False
    requires_secrets: list[str] = field(default_factory=list)
    artifact_policy: str = "none"
    audit_events: list[str] = field(default_factory=lambda: ["tool.called", "tool.completed"])
    enabled: bool = True

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": self.source,
            "category": self.category,
            "description": self.description,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "timeout_seconds": self.timeout_seconds,
            "side_effect": self.side_effect,
            "retryable": self.retryable,
            "requires_secrets": self.requires_secrets,
            "artifact_policy": self.artifact_policy,
            "audit_events": self.audit_events,
            "enabled": self.enabled,
        }


def contract_for_tool(tool: Tool, *, source: str = "builtin", enabled: bool = True) -> ToolContract:
    side_effect = BUILTIN_SIDE_EFFECTS.get(tool.name, "network_read" if source == "mcp" else "none")
    category = side_effect if side_effect != "none" else source
    return ToolContract(
        name=tool.name,
        source=source,
        category=category,
        description=tool.description,
        input_schema=tool.input_schema,
        side_effect=side_effect,
        retryable=side_effect in {"network_read"},
        artifact_policy="capture_artifact" if tool.name in {"file.write_text", "browser.screenshot", "browser.click", "browser.type"} else "none",
    )


def effective_side_effect(tool_name: str, input_data: dict[str, Any], default_side_effect: str) -> str:
    if tool_name == "http.request":
        method = str(input_data.get("method", "GET")).upper()
        return "network_read" if method in HTTP_READ_METHODS else "network_write"
    return default_side_effect


def sync_tool_manifests(session: Session, contracts: list[ToolContract]) -> None:
    for contract in contracts:
        row = session.get(ToolManifest, contract.name)
        if row is None:
            row = ToolManifest(name=contract.name)
            session.add(row)
        row.source = contract.source
        row.category = contract.category
        row.description = contract.description
        row.input_schema_json = json.dumps(contract.input_schema, ensure_ascii=False, sort_keys=True)
        row.output_schema_json = json.dumps(contract.output_schema, ensure_ascii=False, sort_keys=True)
        row.timeout_seconds = contract.timeout_seconds
        row.side_effect = contract.side_effect
        row.retryable = "true" if contract.retryable else "false"
        row.requires_secrets_json = json.dumps(contract.requires_secrets, ensure_ascii=False, sort_keys=True)
        row.artifact_policy = contract.artifact_policy
        row.audit_events_json = json.dumps(contract.audit_events, ensure_ascii=False, sort_keys=True)
        if row.enabled not in {"true", "false"}:
            row.enabled = "true"


def snapshot_tool_contracts(session: Session, run_id: str, contracts: list[ToolContract] | None = None) -> list[ToolContractSnapshot]:
    existing = session.execute(select(ToolContractSnapshot).where(ToolContractSnapshot.run_id == run_id)).scalars().all()
    if existing:
        return existing
    if contracts is not None:
        sync_tool_manifests(session, contracts)
    rows = session.execute(select(ToolManifest).order_by(ToolManifest.name)).scalars().all()
    snapshots: list[ToolContractSnapshot] = []
    for row in rows:
        snapshot = ToolContractSnapshot(
            id=str(uuid4()),
            run_id=run_id,
            tool_name=row.name,
            contract_json=json.dumps(manifest_to_dict(row), ensure_ascii=False, sort_keys=True),
        )
        session.add(snapshot)
        snapshots.append(snapshot)
    return snapshots


def snapshot_to_dict(row: ToolContractSnapshot) -> dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "tool_name": row.tool_name,
        "contract": json.loads(row.contract_json),
        "created_at": row.created_at.isoformat(),
    }


def manifest_to_dict(row: ToolManifest) -> dict[str, Any]:
    return {
        "name": row.name,
        "source": row.source,
        "category": row.category,
        "description": row.description,
        "input_schema": json.loads(row.input_schema_json),
        "output_schema": json.loads(row.output_schema_json),
        "timeout_seconds": row.timeout_seconds,
        "side_effect": row.side_effect,
        "retryable": row.retryable == "true",
        "requires_secrets": json.loads(row.requires_secrets_json),
        "artifact_policy": row.artifact_policy,
        "audit_events": json.loads(row.audit_events_json),
        "enabled": row.enabled == "true",
    }
