from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentend.db.models import Capability, GeneratedTool, Skill, ToolManifest


def refresh_capabilities(session: Session) -> list[Capability]:
    active_names: set[str] = set()
    capabilities: list[Capability] = []
    tool_rows = session.execute(select(ToolManifest).where(ToolManifest.enabled == "true")).scalars().all()
    for manifest in tool_rows:
        capability = session.get(Capability, manifest.name)
        if capability is None:
            capability = Capability(name=manifest.name)
            session.add(capability)
        active_names.add(manifest.name)
        capability.source = "tool"
        capability.action_summary = manifest.description
        capability.input_summary = manifest.input_schema_json
        capability.output_summary = manifest.output_schema_json
        capability.required_secrets_json = manifest.requires_secrets_json
        capability.side_effect = manifest.side_effect
        capability.risk_level = "high" if manifest.side_effect in {"local_execute", "network_write", "external_write"} else "low"
        capability.example_json = json.dumps({"tool": manifest.name}, ensure_ascii=False, sort_keys=True)
        capabilities.append(capability)
    skill_rows = session.execute(select(Skill).where(Skill.enabled == "true")).scalars().all()
    for skill in skill_rows:
        capability = session.get(Capability, skill.id)
        if capability is None:
            capability = Capability(name=skill.id)
            session.add(capability)
        active_names.add(skill.id)
        capability.source = "skill"
        capability.action_summary = skill.description
        capability.input_summary = skill.input_schema_json
        capability.output_summary = skill.output_schema_json
        capability.required_secrets_json = "[]"
        capability.side_effect = "workflow"
        capability.risk_level = "medium"
        capability.example_json = json.dumps({"skill": skill.id, "required_tools": json.loads(skill.required_tools_json)}, ensure_ascii=False, sort_keys=True)
        capabilities.append(capability)
    generated_rows = session.execute(select(GeneratedTool).where(GeneratedTool.status == "draft")).scalars().all()
    for generated in generated_rows:
        capability = session.get(Capability, generated.id)
        if capability is None:
            capability = Capability(name=generated.id)
            session.add(capability)
        active_names.add(generated.id)
        capability.source = "generated"
        capability.action_summary = generated.goal
        capability.input_summary = "{}"
        capability.output_summary = "{}"
        capability.required_secrets_json = "[]"
        capability.side_effect = "none"
        capability.risk_level = "medium"
        capability.example_json = json.dumps(
            {"generated_tool": generated.id, "draft_path": generated.draft_path, "status": generated.status},
            ensure_ascii=False,
            sort_keys=True,
        )
        capabilities.append(capability)
    stale_rows = (
        session.execute(select(Capability).where(Capability.source.in_(["tool", "skill", "generated"]))).scalars().all()
    )
    for stale in stale_rows:
        if stale.name not in active_names:
            session.delete(stale)
    return capabilities


def query_capabilities(session: Session, query: str) -> list[Capability]:
    terms = [term for term in query.lower().split() if term]
    rows = session.execute(select(Capability).order_by(Capability.name)).scalars().all()
    if not terms:
        return rows
    return [
        row
        for row in rows
        if any(term in row.name.lower() or term in row.action_summary.lower() for term in terms)
    ]
