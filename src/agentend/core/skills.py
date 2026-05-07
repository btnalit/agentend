from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import yaml
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from agentend.core.workflow_schema import WorkflowDefinition, load_workflow_yaml
from agentend.db.models import ExtensionRecord, ExtensionVersion, Skill, SkillMarket, utc_now


BUILTIN_SKILL_SPECS = {
    "file.workspace_ops": {
        "description": "Inspect and operate on the local workspace.",
        "triggers": ["file", "workspace", "整理文件"],
        "required_tools": ["fs.list"],
        "prompt": "Workspace task: {input}",
    },
    "shell.automation": {
        "description": "Run local shell automation tasks.",
        "triggers": ["shell", "automation"],
        "required_tools": ["shell.run"],
        "prompt": "Shell automation task: {input}",
    },
    "code.local_task": {
        "description": "Work on local code tasks.",
        "triggers": ["code", "test", "代码"],
        "required_tools": ["fs.read_text", "shell.run", "git.status"],
        "prompt": "Code task: {input}",
    },
    "data.quick_analysis": {
        "description": "Analyze local CSV, JSON, or SQLite data.",
        "triggers": ["data", "analysis"],
        "required_tools": ["python.exec"],
        "prompt": "Data analysis task: {input}",
    },
    "research.report": {
        "description": "Search, fetch, and produce a sourced research report.",
        "triggers": ["research", "report", "调研"],
        "required_tools": ["web.search", "web.fetch", "fs.write_text"],
        "prompt": "Research report task: {input}",
    },
    "mcp.tool_setup": {
        "description": "Help configure MCP servers and example workflows.",
        "triggers": ["mcp", "tool"],
        "required_tools": ["tools.discover"],
        "prompt": "MCP setup task: {input}",
    },
}


@dataclass(frozen=True)
class SkillBundle:
    id: str
    version: str
    description: str
    triggers: list[str]
    workflow_path: Path
    required_tools: list[str]
    required_mcp: list[str]
    input_schema: dict
    output_schema: dict
    enabled: bool
    source_type: str
    source_location: str | None
    manifest: dict


def ensure_builtin_skills(home: Path, session: Session) -> list[Skill]:
    root = home / "skills" / "builtin"
    root.mkdir(parents=True, exist_ok=True)
    rows: list[Skill] = []
    for skill_id, spec in BUILTIN_SKILL_SPECS.items():
        skill_dir = root / skill_id
        skill_dir.mkdir(parents=True, exist_ok=True)
        workflow = skill_dir / "workflow.yaml"
        manifest = skill_dir / "skill.yaml"
        workflow_yaml = _builtin_workflow_yaml(skill_id, spec)
        if not workflow.exists() or _workflow_requires_update(workflow, spec["required_tools"]):
            workflow.write_text(workflow_yaml, encoding="utf-8")
        if not manifest.exists():
            payload = {
                "id": skill_id,
                "version": "0.1.0",
                "description": spec["description"],
                "triggers": spec["triggers"],
                "workflow": "workflow.yaml",
                "required_tools": spec["required_tools"],
                "required_mcp": [],
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
                "enabled": True,
                "source": {"type": "builtin"},
            }
            manifest.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
        bundle = load_skill_bundle(skill_dir)
        rows.append(upsert_skill(session, bundle))
    return rows


def load_skill_bundle(skill_dir: Path) -> SkillBundle:
    manifest_path = skill_dir / "skill.yaml"
    if not manifest_path.exists():
        raise ValueError(f"Missing skill.yaml: {skill_dir}")
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    required = ["id", "version", "description", "workflow", "required_tools", "input_schema", "output_schema"]
    missing = [key for key in required if key not in manifest]
    if missing:
        raise ValueError(f"{manifest_path}: missing fields {missing}")
    workflow_path = skill_dir / str(manifest["workflow"])
    if not workflow_path.exists():
        raise ValueError(f"Missing workflow: {workflow_path}")
    workflow = load_workflow_yaml(workflow_path.read_text(encoding="utf-8"))
    _validate_required_tools(workflow, [str(item) for item in manifest.get("required_tools", [])], workflow_path)
    source = manifest.get("source") or {}
    return SkillBundle(
        id=str(manifest["id"]),
        version=str(manifest["version"]),
        description=str(manifest["description"]),
        triggers=[str(item) for item in manifest.get("triggers", [])],
        workflow_path=workflow_path,
        required_tools=[str(item) for item in manifest.get("required_tools", [])],
        required_mcp=[str(item) for item in manifest.get("required_mcp", [])],
        input_schema=dict(manifest.get("input_schema", {})),
        output_schema=dict(manifest.get("output_schema", {})),
        enabled=bool(manifest.get("enabled", True)),
        source_type=str(source.get("type", "local")),
        source_location=str(skill_dir),
        manifest=manifest,
    )


def upsert_skill(
    session: Session,
    bundle: SkillBundle,
    *,
    extension_content_hash: str | None = None,
    extension_version_source: str | None = None,
) -> Skill:
    values = _skill_bundle_values(bundle)
    update_values = {key: value for key, value in values.items() if key not in {"id", "enabled"}}
    session.execute(
        sqlite_insert(Skill)
        .values(**values)
        .on_conflict_do_update(index_elements=["id"], set_=update_values)
    )
    row = session.get(Skill, bundle.id)
    if row is None:
        session.flush()
        row = session.get(Skill, bundle.id)
    assert row is not None
    upsert_extension(
        session,
        kind="skill",
        name=bundle.id,
        status="enabled" if row.enabled == "true" else "disabled",
        source=bundle.source_location or bundle.source_type,
        version=bundle.version,
        content_hash=extension_content_hash,
        version_source=extension_version_source,
    )
    return row


def _skill_bundle_values(bundle: SkillBundle) -> dict[str, str]:
    return {
        "id": bundle.id,
        "version": bundle.version,
        "description": bundle.description,
        "triggers_json": json.dumps(bundle.triggers, ensure_ascii=False),
        "workflow_path": str(bundle.workflow_path),
        "required_tools_json": json.dumps(bundle.required_tools, ensure_ascii=False),
        "required_mcp_json": json.dumps(bundle.required_mcp, ensure_ascii=False),
        "input_schema_json": json.dumps(bundle.input_schema, ensure_ascii=False),
        "output_schema_json": json.dumps(bundle.output_schema, ensure_ascii=False),
        "enabled": "true" if bundle.enabled else "false",
        "source_type": bundle.source_type,
        "source_location": bundle.source_location,
        "manifest_json": json.dumps(bundle.manifest, ensure_ascii=False, sort_keys=True),
    }


def _apply_skill_bundle(row: Skill, bundle: SkillBundle, *, created: bool) -> None:
    row.version = bundle.version
    row.description = bundle.description
    row.triggers_json = json.dumps(bundle.triggers, ensure_ascii=False)
    row.workflow_path = str(bundle.workflow_path)
    row.required_tools_json = json.dumps(bundle.required_tools, ensure_ascii=False)
    row.required_mcp_json = json.dumps(bundle.required_mcp, ensure_ascii=False)
    row.input_schema_json = json.dumps(bundle.input_schema, ensure_ascii=False)
    row.output_schema_json = json.dumps(bundle.output_schema, ensure_ascii=False)
    if created:
        row.enabled = "true" if bundle.enabled else "false"
    row.source_type = bundle.source_type
    row.source_location = bundle.source_location
    row.manifest_json = json.dumps(bundle.manifest, ensure_ascii=False, sort_keys=True)


def upsert_extension(
    session: Session,
    *,
    kind: str,
    name: str,
    status: str,
    source: str,
    version: str,
    content_hash: str | None = None,
    version_source: str | None = None,
) -> ExtensionRecord:
    extension_id = f"{kind}:{name}"
    digest = content_hash or sha256(f"{kind}:{name}:{source}:{version}".encode("utf-8")).hexdigest()
    values = {
        "id": extension_id,
        "kind": kind,
        "name": name,
        "status": status,
        "source": source,
        "version": version,
        "content_hash": digest,
        "last_validated_at": utc_now(),
    }
    session.execute(
        sqlite_insert(ExtensionRecord)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["id"],
            set_={key: value for key, value in values.items() if key != "id"},
        )
    )
    row = session.get(ExtensionRecord, extension_id)
    if row is None:
        session.flush()
        row = session.get(ExtensionRecord, extension_id)
    assert row is not None
    if not (
        session.execute(
            select(ExtensionVersion)
            .where(ExtensionVersion.extension_id == extension_id)
            .where(ExtensionVersion.version == version)
            .where(ExtensionVersion.content_hash == digest)
            .where(ExtensionVersion.status == ("validated" if status != "quarantined" else "quarantined"))
        ).first()
    ):
        session.add(
            ExtensionVersion(
                id=str(uuid4()),
                extension_id=extension_id,
                version=version,
                content_hash=digest,
                source=version_source or source,
                status="validated" if status != "quarantined" else "quarantined",
            )
        )
    return row


def list_skill_bundles_from_directory(path: Path) -> list[SkillBundle]:
    bundles: list[SkillBundle] = []
    for child in sorted(path.iterdir()):
        if child.is_dir() and (child / "skill.yaml").exists():
            bundles.append(load_skill_bundle(child))
    return bundles


def add_market(session: Session, name: str, *, backend: str, location: str) -> SkillMarket:
    row = session.get(SkillMarket, name)
    if row is None:
        row = SkillMarket(name=name, backend=backend, location=location)
        session.add(row)
    else:
        row.backend = backend
        row.location = location
        row.enabled = "true"
    upsert_extension(session, kind="market", name=name, status="enabled", source=location, version="0.1.0")
    return row


def refresh_markets(home: Path, session: Session) -> list[Skill]:
    installed: list[Skill] = []
    markets = session.execute(select(SkillMarket).where(SkillMarket.enabled == "true")).scalars().all()
    cache_root = home / "skills" / "market-cache"
    cache_root.mkdir(parents=True, exist_ok=True)
    for market in markets:
        market_cache = cache_root / _safe_name(market.name)
        source = _prepare_market_source(market, market_cache)
        for child in sorted(source.iterdir()):
            if not child.is_dir() or not (child / "skill.yaml").exists():
                continue
            try:
                bundle = load_skill_bundle(child)
                digest = _directory_content_hash(child)
                snapshot = _snapshot_bundle(market_cache, child, bundle, digest)
                installed.append(
                    upsert_skill(
                        session,
                        bundle,
                        extension_content_hash=digest,
                        extension_version_source=str(snapshot),
                    )
                )
            except Exception as exc:
                _quarantine_bundle(session, market_cache, child, exc)
    return installed


def rollback_extension(home: Path, session: Session, extension_id: str, version: str) -> ExtensionRecord:
    row = session.get(ExtensionRecord, extension_id)
    if row is None:
        raise ValueError(f"Unknown extension: {extension_id}")
    version_row = (
        session.execute(
            select(ExtensionVersion)
            .where(ExtensionVersion.extension_id == extension_id)
            .where(ExtensionVersion.version == version)
            .where(ExtensionVersion.status == "validated")
            .order_by(ExtensionVersion.created_at.desc())
        )
        .scalars()
        .first()
    )
    if version_row is None:
        raise ValueError(f"Unknown validated version for {extension_id}: {version}")

    row.version = version_row.version
    row.content_hash = version_row.content_hash
    row.status = "enabled"
    row.source = version_row.source
    if row.kind == "skill":
        skill = session.get(Skill, row.name)
        if skill is not None:
            snapshot = Path(version_row.source)
            target = Path(skill.source_location or snapshot)
            if not target.is_absolute():
                target = (home / target).resolve()
            if snapshot.exists() and snapshot.is_dir():
                if snapshot.resolve() != target.resolve():
                    _replace_directory(snapshot, target)
                bundle = load_skill_bundle(target)
                _apply_skill_bundle(skill, bundle, created=False)
                skill.enabled = "true"
                row.source = str(target)
    return row


def _prepare_market_source(market: SkillMarket, market_cache: Path) -> Path:
    source_target = market_cache / "source"
    _ensure_cache_target(source_target)
    if market.backend == "git":
        source = Path(market.location)
        if source.exists():
            shutil.copytree(source, source_target, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        else:
            subprocess.run(
                ["git", "clone", "--depth", "1", market.location, str(source_target)],
                check=True,
                capture_output=True,
                text=True,
            )
        return source_target
    source = Path(market.location)
    shutil.copytree(source, source_target, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    return source_target


def _ensure_cache_target(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.rmtree(path)


def _snapshot_bundle(market_cache: Path, source: Path, bundle: SkillBundle, digest: str) -> Path:
    snapshot = market_cache / "snapshots" / _safe_name(bundle.id) / f"{bundle.version}-{digest[:12]}"
    if snapshot.exists():
        shutil.rmtree(snapshot)
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, snapshot, ignore=shutil.ignore_patterns("__pycache__"))
    return snapshot


def _quarantine_bundle(session: Session, market_cache: Path, source: Path, exc: Exception) -> None:
    skill_id, version = _manifest_identity(source)
    report_dir = market_cache / "quarantine" / _safe_name(skill_id)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "error.json"
    payload = {"skill_id": skill_id, "version": version, "source": str(source), "error": str(exc)}
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    skill = session.get(Skill, skill_id)
    if skill is not None:
        skill.enabled = "false"
    upsert_extension(
        session,
        kind="skill",
        name=skill_id,
        status="quarantined",
        source=str(report_path),
        version=version,
        content_hash=_directory_content_hash(source),
        version_source=str(report_path),
    )


def _manifest_identity(source: Path) -> tuple[str, str]:
    try:
        manifest = yaml.safe_load((source / "skill.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        return source.name, "unknown"
    return str(manifest.get("id") or source.name), str(manifest.get("version") or "unknown")


def _replace_directory(source: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))


def _directory_content_hash(path: Path) -> str:
    digest = sha256()
    for file in sorted(item for item in path.rglob("*") if item.is_file() and ".git" not in item.parts):
        digest.update(str(file.relative_to(path)).replace("\\", "/").encode("utf-8"))
        digest.update(b"\0")
        digest.update(file.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _safe_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in value).strip("._") or "item"


def _builtin_workflow_yaml(skill_id: str, spec: dict) -> str:
    workflow_id = f"skill.{skill_id}"
    if skill_id == "file.workspace_ops":
        nodes = [
            {"id": "list_workspace", "type": "tool", "tool": "fs.list", "input": {"path": "."}},
            {"id": "glob_workspace", "type": "tool", "tool": "fs.glob", "input": {"pattern": "*"}, "depends_on": ["list_workspace"]},
            {"id": "summary", "type": "parallel", "depends_on": ["list_workspace", "glob_workspace"]},
            {
                "id": "answer",
                "type": "llm",
                "prompt": "Workspace task: {input}\n\nTool summary:\n{summary}",
                "depends_on": ["summary"],
            },
            {"id": "final", "type": "final", "depends_on": ["answer"]},
        ]
    elif skill_id == "shell.automation":
        nodes = [
            {"id": "run_shell", "type": "tool", "tool": "shell.run", "input": {"command": "echo {input}"}},
            {"id": "final", "type": "final", "depends_on": ["run_shell"]},
        ]
    elif skill_id == "code.local_task":
        nodes = [
            {"id": "git_status", "type": "tool", "tool": "git.status", "input": {"cwd": "."}},
            {"id": "read_profile", "type": "tool", "tool": "fs.read_text", "input": {"path": "agent.md"}},
            {"id": "python_version", "type": "tool", "tool": "shell.run", "input": {"command": "python --version"}},
            {"id": "summary", "type": "parallel", "depends_on": ["git_status", "read_profile", "python_version"]},
            {"id": "final", "type": "final", "depends_on": ["summary"]},
        ]
    elif skill_id == "data.quick_analysis":
        nodes = [
            {
                "id": "analyze",
                "type": "tool",
                "tool": "python.exec",
                "input": {
                    "code": (
                        "import json\n"
                        "payload = {{\"input\": '''{input}'''}}\n"
                        "print(json.dumps({{\"received\": payload[\"input\"]}}))\n"
                    )
                },
            },
            {"id": "final", "type": "final", "depends_on": ["analyze"]},
        ]
    elif skill_id == "research.report":
        nodes = [
            {"id": "search", "type": "tool", "tool": "web.search", "input": {"query": "{input}", "provider": "fake", "limit": 1}},
            {"id": "fetch", "type": "tool", "tool": "web.fetch", "input": {"url": "https://example.com/search/1"}, "depends_on": ["search"]},
            {
                "id": "write_report",
                "type": "tool",
                "tool": "fs.write_text",
                "input": {
                    "path": "reports/research-report.md",
                    "content": "# Research Report\n\nInput:\n{input}\n\nSearch evidence:\n{search}\n\nFetched source:\n{fetch}\n",
                },
                "depends_on": ["fetch"],
            },
            {"id": "final", "type": "final", "depends_on": ["write_report"]},
        ]
    elif skill_id == "mcp.tool_setup":
        nodes = [
            {"id": "discover", "type": "tool", "tool": "tools.discover", "input": {"query": "mcp"}},
            {"id": "final", "type": "final", "depends_on": ["discover"]},
        ]
    else:
        nodes = [
            {"id": "answer", "type": "llm", "prompt": str(spec["prompt"])},
            {"id": "final", "type": "final", "depends_on": ["answer"]},
        ]
    return yaml.safe_dump(
        {"id": workflow_id, "name": skill_id, "nodes": nodes},
        allow_unicode=True,
        sort_keys=False,
    )


def _workflow_requires_update(workflow_path: Path, required_tools: list[str]) -> bool:
    try:
        workflow = load_workflow_yaml(workflow_path.read_text(encoding="utf-8"))
    except Exception:
        return True
    used_tools = _workflow_tool_names(workflow)
    return any(tool not in used_tools for tool in required_tools)


def _validate_required_tools(workflow: WorkflowDefinition, required_tools: list[str], workflow_path: Path) -> None:
    used_tools = _workflow_tool_names(workflow)
    missing = [tool for tool in required_tools if tool not in used_tools]
    if missing:
        raise ValueError(f"{workflow_path}: required_tools not used by workflow: {missing}")


def _workflow_tool_names(workflow: WorkflowDefinition) -> set[str]:
    return {str(node.tool) for node in workflow.nodes if node.type == "tool" and node.tool}
