from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentend.db.models import Capability, GeneratedTool, Skill, ToolManifest


HIGH_SIDE_EFFECTS = {"local_execute", "network_write", "external_write"}


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
        capability.risk_level = "high" if manifest.side_effect in HIGH_SIDE_EFFECTS else "low"
        capability.example_json = json.dumps(
            {
                "tool": manifest.name,
                "manifest": _tool_capability_manifest(manifest),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
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
        required_tools = _json_list(skill.required_tools_json)
        capability.example_json = json.dumps(
            {
                "skill": skill.id,
                "required_tools": required_tools,
                "manifest": _skill_capability_manifest(skill, required_tools),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
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
            {
                "generated_tool": generated.id,
                "draft_path": generated.draft_path,
                "status": generated.status,
                "manifest": _generated_capability_manifest(generated),
            },
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


def capability_manifest(row: Capability) -> dict[str, object]:
    payload = _json_dict(row.example_json)
    manifest = payload.get("manifest")
    if isinstance(manifest, dict):
        return dict(manifest)
    return _legacy_capability_manifest(row, payload)


def query_capabilities(session: Session, query: str, *, executable_only: bool = False) -> list[Capability]:
    terms = [term for term in query.lower().split() if term]
    rows = session.execute(select(Capability).order_by(Capability.name)).scalars().all()
    if executable_only:
        rows = [row for row in rows if capability_manifest(row).get("executable") is True]
    if not terms:
        return rows
    return [
        row
        for row in rows
        if any(_capability_matches(row, term) for term in terms)
    ]


def query_executable_capabilities(session: Session, query: str) -> list[Capability]:
    return query_capabilities(session, query, executable_only=True)


def _tool_capability_manifest(manifest: ToolManifest) -> dict[str, object]:
    return {
        "id": manifest.name,
        "capability_id": manifest.name,
        "type": "tool",
        "description": manifest.description,
        "input_schema": _json_dict(manifest.input_schema_json),
        "output_schema": _json_dict(manifest.output_schema_json),
        "side_effect_upper_bound": manifest.side_effect,
        "risk_profile": _risk_profile(manifest.side_effect),
        "required_tools": [],
        "eval_status": "passed",
        "policy_tags": _policy_tags("tool", manifest.side_effect),
        "enabled": manifest.enabled == "true",
        "executable": manifest.enabled == "true",
        "version": "0.1.0",
    }


def _skill_capability_manifest(skill: Skill, required_tools: list[str]) -> dict[str, object]:
    return {
        "id": skill.id,
        "capability_id": skill.id,
        "type": "skill",
        "description": skill.description,
        "input_schema": _json_dict(skill.input_schema_json),
        "output_schema": _json_dict(skill.output_schema_json),
        "side_effect_upper_bound": "workflow",
        "risk_profile": {"risk_level": "medium", "requires_confirmation": False},
        "required_tools": required_tools,
        "eval_status": "passed",
        "policy_tags": ["skill", "workflow"],
        "enabled": skill.enabled == "true",
        "executable": skill.enabled == "true",
        "version": skill.version,
    }


def _generated_capability_manifest(generated: GeneratedTool) -> dict[str, object]:
    metadata = _json_dict(generated.metadata_json)
    return {
        "id": generated.id,
        "capability_id": generated.id,
        "type": "generated",
        "description": generated.goal,
        "input_schema": {},
        "output_schema": {},
        "side_effect_upper_bound": "none",
        "risk_profile": {"risk_level": "medium", "requires_confirmation": True},
        "required_tools": [],
        "eval_status": generated.status,
        "policy_tags": ["generated", "draft"],
        "enabled": False,
        "executable": False,
        "version": str(metadata.get("version") or "0.1.0"),
        "draft_path": generated.draft_path,
    }


def _legacy_capability_manifest(row: Capability, payload: dict[str, object]) -> dict[str, object]:
    capability_type = "tool" if row.source == "tool" else "skill" if row.source == "skill" else row.source
    executable = capability_type in {"tool", "skill"}
    return {
        "id": row.name,
        "capability_id": row.name,
        "type": capability_type,
        "description": row.action_summary,
        "input_schema": _json_dict(row.input_summary),
        "output_schema": _json_dict(row.output_summary),
        "side_effect_upper_bound": row.side_effect,
        "risk_profile": _risk_profile(row.side_effect),
        "required_tools": _json_list(json.dumps(payload.get("required_tools", []))),
        "eval_status": "draft" if row.source == "generated" else "passed",
        "policy_tags": _policy_tags(capability_type, row.side_effect),
        "enabled": executable,
        "executable": executable,
        "version": "0.1.0",
    }


def _capability_matches(row: Capability, term: str) -> bool:
    manifest = capability_manifest(row)
    haystack = " ".join(
        [
            row.name,
            row.action_summary,
            row.source,
            str(manifest.get("type", "")),
            " ".join(str(item) for item in manifest.get("policy_tags", [])),
        ]
    ).lower()
    return term in haystack


def _risk_profile(side_effect: str) -> dict[str, object]:
    return {
        "risk_level": "high" if side_effect in HIGH_SIDE_EFFECTS else "low",
        "requires_confirmation": side_effect in HIGH_SIDE_EFFECTS,
    }


def _policy_tags(capability_type: str, side_effect: str) -> list[str]:
    tags = [capability_type, side_effect]
    if side_effect in HIGH_SIDE_EFFECTS:
        tags.append("high-risk")
    return sorted(set(tags))


def _json_dict(raw_json: str) -> dict[str, object]:
    try:
        payload = json.loads(raw_json or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _json_list(raw_json: str) -> list[str]:
    try:
        payload = json.loads(raw_json or "[]")
    except json.JSONDecodeError:
        return []
    return [str(item) for item in payload] if isinstance(payload, list) else []
