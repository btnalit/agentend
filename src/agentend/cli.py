from datetime import datetime
from pathlib import Path
import json
import os
import shutil
import time
from typing import Optional

import typer
from sqlalchemy import select

from agentend.config import load_config, set_llm_config
from agentend.core.agent_run import AgentRunController
from agentend.core.capabilities import query_capabilities, refresh_capabilities
from agentend.core.clipboard import ClipboardUnavailable, read_clipboard, write_clipboard
from agentend.core.conversation import ConversationService
from agentend.core.context_policy import get_context_policy, upsert_context_policy
from agentend.core.context_runtime import build_context_pack
from agentend.core.doctor import doctor_json, run_doctor
from agentend.core.evidence import evidence_manifest_for_run
from agentend.core.episodes import episode_to_dict, promote_episode_to_skill, summarize_run
from agentend.core.errors import classify_exception
from agentend.core.effectiveness import effectiveness_rows, effectiveness_summary_dict
from agentend.core.eval_harness import list_eval_suites, run_eval_suite
from agentend.core.goal_analyzer import analyze_goal, goal_analysis_text
from agentend.core.init import initialize_home
from agentend.core.intent_audit import intent_record_to_dict, record_intent_decision
from agentend.core.intent_router import decide_intent
from agentend.core.llm_router import LLMRouter
from agentend.core.memory_consolidator import (
    consolidate_memory_candidates,
    extract_memory_candidates,
    memory_candidate_to_dict,
)
from agentend.core.memory_quality import compile_project_memory_digest, lint_memory_items
from agentend.core.memory_store import edit_memory_item, forget_memory_item, search_memory_items, write_memory_item
from agentend.core.model_routing import list_routes, set_budget, set_route
from agentend.core.profile import load_agent_profile
from agentend.core.replanner import replan_failure, replan_text
from agentend.core.replay import build_replay_plan, execute_replay_plan
from agentend.core.secrets import configured_secret_names, redact_text, upsert_secret_ref
from agentend.core.skills import add_market, ensure_builtin_skills, load_skill_bundle, refresh_markets, rollback_extension
from agentend.core.storage import (
    build_cleanup_plan,
    execute_cleanup_plan,
    load_cleanup_plan,
    record_cleanup_run,
    storage_usage as storage_usage_report,
)
from agentend.core.tasks import (
    DEFAULT_INBOX_BATCH_LIMIT,
    DEFAULT_SCHEDULE_FAILURE_THRESHOLD,
    TaskManager,
    validate_cron,
)
from agentend.core.tool_contracts import manifest_to_dict, snapshot_to_dict, sync_tool_manifests
from agentend.core.workspace_indexer import index_workspace, workspace_summary
from agentend.core.workflow_registry import WorkflowRegistry
from agentend.core.workflow_runner import WorkflowRunFailed, WorkflowRunner
from agentend.core.workflow_schema import load_workflow_yaml
from agentend.core.events import record_event
from agentend.core.worker import AgentWorker
from agentend.mcp.manager import MCPManager
from agentend.telegram_bot import serve_telegram
from agentend.db.models import (
    Artifact,
    Capability,
    Checkpoint,
    ClarificationRequest,
    ContextDroppedItem,
    ContextPolicy,
    ContextLedger,
    ContextPackItem,
    CostBudget,
    CostUsage,
    Episode,
    EvalRun,
    EventLog,
    ExtensionRecord,
    ExtensionVersion,
    IntentDecisionRecord,
    MemoryItem,
    Message,
    ProjectProfile,
    Run,
    RunExport,
    SourceRecord,
    RunStep,
    Skill,
    SkillMarket,
    StorageCleanupRun,
    ToolCall,
    ToolContractSnapshot,
    ToolManifest,
)
from agentend.db.session import init_database, session_scope
from agentend.db.session import database_path
from agentend.core.tool_registry import ToolRegistry
from agentend.db.models import Conversation
from agentend.tools.base import ToolContext
from uuid import uuid4

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
tools_app = typer.Typer(help="Tool registry commands.", no_args_is_help=True)
skills_app = typer.Typer(help="Skill library commands.", no_args_is_help=True)
skill_markets_app = typer.Typer(help="Skill market commands.", no_args_is_help=True)
skill_effectiveness_app = typer.Typer(help="Skill effectiveness commands.", no_args_is_help=True)
extensions_app = typer.Typer(help="Extension lifecycle commands.", no_args_is_help=True)
secrets_app = typer.Typer(help="Secret reference commands.", no_args_is_help=True)
models_app = typer.Typer(help="Model routing commands.", no_args_is_help=True)
model_routes_app = typer.Typer(help="Model route commands.", no_args_is_help=True)
budget_app = typer.Typer(help="Budget commands.", no_args_is_help=True)
eval_app = typer.Typer(help="Agent eval commands.", no_args_is_help=True)
context_app = typer.Typer(help="Context runtime commands.", no_args_is_help=True)
context_ledger_app = typer.Typer(help="Context ledger commands.", no_args_is_help=True)
context_policy_app = typer.Typer(help="Context policy commands.", no_args_is_help=True)
memory_app = typer.Typer(help="Memory commands.", no_args_is_help=True)
checkpoints_app = typer.Typer(help="Checkpoint commands.", no_args_is_help=True)
clarifications_app = typer.Typer(help="Clarification request commands.", no_args_is_help=True)
workspace_app = typer.Typer(help="Workspace context commands.", no_args_is_help=True)
project_app = typer.Typer(help="Project profile commands.", no_args_is_help=True)
project_profile_app = typer.Typer(help="Project profile commands.", no_args_is_help=True)
artifacts_app = typer.Typer(help="Artifact commands.", no_args_is_help=True)
storage_app = typer.Typer(help="Storage governance commands.", no_args_is_help=True)
capabilities_app = typer.Typer(help="Capability map commands.", no_args_is_help=True)
capability_effectiveness_app = typer.Typer(help="Capability effectiveness commands.", no_args_is_help=True)
sources_app = typer.Typer(help="Source evidence commands.", no_args_is_help=True)
goal_app = typer.Typer(help="Goal analysis commands.", no_args_is_help=True)
intent_app = typer.Typer(help="Intent routing debug commands.", no_args_is_help=True)
plan_app = typer.Typer(help="Planning recovery commands.", no_args_is_help=True)
episodes_app = typer.Typer(help="Episode logger commands.", no_args_is_help=True)
inbox_app = typer.Typer(help="File inbox commands.", no_args_is_help=True)
tasks_app = typer.Typer(help="Task inbox commands.", no_args_is_help=True)
schedule_app = typer.Typer(help="Local schedule commands.", no_args_is_help=True)
clipboard_app = typer.Typer(help="Clipboard commands.", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(runs_app, name="runs")
app.add_typer(llm_app, name="llm")
app.add_typer(agent_app, name="agent")
app.add_typer(workflows_app, name="workflows")
app.add_typer(mcp_app, name="mcp")
app.add_typer(telegram_app, name="telegram")
app.add_typer(logs_app, name="logs")
app.add_typer(tools_app, name="tools")
skills_app.add_typer(skill_markets_app, name="markets")
skills_app.add_typer(skill_effectiveness_app, name="effectiveness")
app.add_typer(skills_app, name="skills")
app.add_typer(extensions_app, name="extensions")
app.add_typer(secrets_app, name="secrets")
models_app.add_typer(model_routes_app, name="routes")
app.add_typer(models_app, name="models")
app.add_typer(budget_app, name="budget")
app.add_typer(eval_app, name="eval")
context_app.add_typer(context_ledger_app, name="ledger")
context_app.add_typer(context_policy_app, name="policy")
app.add_typer(context_app, name="context")
app.add_typer(memory_app, name="memory")
app.add_typer(checkpoints_app, name="checkpoints")
app.add_typer(clarifications_app, name="clarifications")
app.add_typer(workspace_app, name="workspace")
project_app.add_typer(project_profile_app, name="profile")
app.add_typer(project_app, name="project")
app.add_typer(artifacts_app, name="artifacts")
app.add_typer(storage_app, name="storage")
app.add_typer(capabilities_app, name="capabilities")
capabilities_app.add_typer(capability_effectiveness_app, name="effectiveness")
app.add_typer(sources_app, name="sources")
app.add_typer(goal_app, name="goal")
app.add_typer(intent_app, name="intent")
app.add_typer(plan_app, name="plan")
app.add_typer(episodes_app, name="episodes")
app.add_typer(inbox_app, name="inbox")
app.add_typer(tasks_app, name="tasks")
app.add_typer(schedule_app, name="schedule")
app.add_typer(clipboard_app, name="clipboard")


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


@app.command()
def doctor(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Run local environment checks."""
    if json_output:
        typer.echo(doctor_json(home or Path.cwd()))
        return
    for check in run_doctor(home or Path.cwd()):
        suffix = f"  fix: {check.fix_hint}" if check.fix_hint else ""
        typer.echo(f"{check.status.upper()} {check.name}: {check.message}{suffix}")


@goal_app.command("analyze")
def goal_analyze(
    text: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Analyze a user goal and recommend candidate capabilities."""
    resolved_home = (home or Path.cwd()).expanduser().resolve()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        sync_tool_manifests(session, ToolRegistry(resolved_home).manifests())
        payload = analyze_goal(resolved_home, session, text)
        typer.echo(goal_analysis_text(payload))


@intent_app.command("decide")
def intent_decide(
    text: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Decide the structured intent route for one input."""
    resolved_home = (home or Path.cwd()).expanduser().resolve()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        decision = decide_intent(resolved_home, session, text)
        row = record_intent_decision(
            resolved_home,
            session,
            text,
            decision,
            channel="cli",
            external_user_id="local",
            route_type="debug",
            context_summary={"source": "cli.intent_decide", "text_length": len(text)},
        )
        payload = json.loads(row.decision_json)
        payload["intent_decision_id"] = row.id
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    typer.echo(f"IntentDecision: {payload['intent_decision_id']}")
    typer.echo(f"Intent: {payload.get('intent_type')}  confidence={payload.get('confidence')}  risk={payload.get('risk_level')}")
    if payload.get("clarification_question"):
        typer.echo(f"Clarification: {payload['clarification_question']}")
    typer.echo(f"Reason: {payload.get('routing_reason') or '-'}")


@intent_app.command("show")
def intent_show(
    intent_decision_id: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Show one persisted intent decision."""
    resolved_home = (home or Path.cwd()).expanduser().resolve()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        row = session.get(IntentDecisionRecord, intent_decision_id)
        if row is None:
            raise typer.BadParameter(f"unknown intent decision: {intent_decision_id}")
        payload = intent_record_to_dict(row)
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    typer.echo(f"IntentDecision: {payload['id']}")
    typer.echo(f"Intent: {payload['intent_type']}  confidence={payload['confidence']}  risk={payload['risk_level']}")
    typer.echo(f"Source: {payload['source']}  route={payload.get('route_type') or '-'}")
    typer.echo(f"Created: {payload['created_at']}")


@intent_app.command("list")
def intent_list(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
    conversation_id: Optional[str] = typer.Option(None, "--conversation", help="Filter by conversation id."),
    run_id: Optional[str] = typer.Option(None, "--run", help="Filter by run id."),
    agent_run_id: Optional[str] = typer.Option(None, "--agent-run", help="Filter by agent run id."),
    limit: int = typer.Option(20, "--limit", min=1, max=200, help="Maximum rows to show."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """List persisted intent decisions."""
    resolved_home = (home or Path.cwd()).expanduser().resolve()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        query = select(IntentDecisionRecord)
        if conversation_id:
            query = query.where(IntentDecisionRecord.conversation_id == conversation_id)
        if run_id:
            query = query.where(IntentDecisionRecord.run_id == run_id)
        if agent_run_id:
            query = query.where(IntentDecisionRecord.agent_run_id == agent_run_id)
        rows = (
            session.execute(query.order_by(IntentDecisionRecord.created_at.desc()).limit(limit))
            .scalars()
            .all()
        )
        payload = [intent_record_to_dict(row) for row in rows]
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    if not payload:
        typer.echo("No intent decisions.")
        return
    for row in payload:
        typer.echo(
            f"{row['created_at']}  {row['id']}  {row['intent_type']}  "
            f"risk={row['risk_level']}  source={row['source']}  route={row.get('route_type') or '-'}"
        )


@plan_app.command("replan")
def plan_replan(
    failed_step: str = typer.Option(..., "--failed-step", help="Failed workflow step or tool."),
    error: str = typer.Option(..., "--error", help="Error message."),
    error_code: Optional[str] = typer.Option(None, "--error-code", help="Optional structured error code."),
    goal: str = typer.Option("", "--goal", help="Original goal."),
    current_workflow: str = typer.Option("", "--workflow", help="Current workflow id."),
) -> None:
    """Generate a deterministic recovery suggestion."""
    payload = replan_failure(
        goal=goal,
        current_workflow=current_workflow,
        failed_step=failed_step,
        error=error,
        error_code=error_code,
    )
    typer.echo(replan_text(payload))


@tools_app.command("list")
def tools_list(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """List registered tool contracts."""
    resolved_home = home or Path.cwd()
    init_database(resolved_home)
    registry = ToolRegistry(resolved_home)
    with session_scope(resolved_home) as session:
        sync_tool_manifests(session, registry.manifests())
        rows = session.execute(select(ToolManifest).order_by(ToolManifest.name)).scalars().all()
        for row in rows:
            typer.echo(f"{row.name}  source={row.source}  side_effect={row.side_effect}  enabled={row.enabled}")


@tools_app.command("show")
def tools_show(
    tool_name: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Show one tool contract."""
    resolved_home = home or Path.cwd()
    init_database(resolved_home)
    registry = ToolRegistry(resolved_home)
    with session_scope(resolved_home) as session:
        sync_tool_manifests(session, registry.manifests())
        row = session.get(ToolManifest, tool_name)
        if row is None:
            raise typer.BadParameter(f"Unknown tool: {tool_name}")
        typer.echo(json.dumps(manifest_to_dict(row), ensure_ascii=False, indent=2, sort_keys=True))


@tools_app.command("test")
def tools_test(
    tool_name: str,
    input_json: str = typer.Option("{}", "--input", help="Tool input JSON."),
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Execute one tool through the normal ToolRegistry path."""
    resolved_home = home or Path.cwd()
    init_database(resolved_home)
    try:
        payload = json.loads(input_json)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"Invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise typer.BadParameter("--input must be a JSON object")

    registry = ToolRegistry(resolved_home)
    failed = False
    with session_scope(resolved_home) as session:
        sync_tool_manifests(session, registry.manifests())
        conversation = Conversation(id=str(uuid4()), channel="cli", external_user_id="tools-test", title=tool_name)
        run = Run(
            id=str(uuid4()),
            conversation_id=conversation.id,
            workflow_id="tools.test",
            status="running",
            input_json=json.dumps({"tool": tool_name, "input": payload}, ensure_ascii=False),
            result_json="{}",
        )
        session.add(conversation)
        session.add(run)
        try:
            result = registry.call(tool_name, payload, ToolContext(resolved_home.resolve(), run.id, None, session))
            run.status = "completed"
            run.result_json = json.dumps(result.data | {"content": result.content}, ensure_ascii=False, sort_keys=True)
            typer.echo(result.content)
        except Exception as exc:
            classified = classify_exception(exc)
            run.status = "failed"
            run.error = classified.message
            run.result_json = json.dumps({"error_code": classified.code, "error": classified.message}, ensure_ascii=False)
            typer.echo(f"Error [{classified.code}]: {classified.message}")
            failed = True
    if failed:
        raise typer.Exit(1)


@tools_app.command("enable")
def tools_enable(
    tool_name: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Enable a tool contract."""
    _set_tool_enabled(home or Path.cwd(), tool_name, enabled=True)
    typer.echo(f"Enabled tool: {tool_name}")


@tools_app.command("disable")
def tools_disable(
    tool_name: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Disable a tool contract."""
    _set_tool_enabled(home or Path.cwd(), tool_name, enabled=False)
    typer.echo(f"Disabled tool: {tool_name}")


@skills_app.command("list")
def skills_list(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """List installed skills."""
    resolved_home = (home or Path.cwd()).expanduser().resolve()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        ensure_builtin_skills(resolved_home, session)
        rows = session.execute(select(Skill).order_by(Skill.id)).scalars().all()
        for row in rows:
            typer.echo(f"{row.id}  version={row.version}  source={row.source_type}  enabled={row.enabled}")


@skills_app.command("show")
def skills_show(
    skill_id: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Show one skill manifest."""
    resolved_home = (home or Path.cwd()).expanduser().resolve()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        ensure_builtin_skills(resolved_home, session)
        row = session.get(Skill, skill_id)
        if row is None:
            raise typer.BadParameter(f"Unknown skill: {skill_id}")
        typer.echo(json.dumps(_skill_manifest_dict(row), ensure_ascii=False, indent=2, sort_keys=True))


@skills_app.command("install")
def skills_install(
    skill_id: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Install one skill from builtin or configured markets."""
    resolved_home = (home or Path.cwd()).expanduser().resolve()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        ensure_builtin_skills(resolved_home, session)
        row = session.get(Skill, skill_id)
        if row is None:
            refresh_markets(resolved_home, session)
            row = session.get(Skill, skill_id)
        if row is None:
            raise typer.BadParameter(f"Unknown skill: {skill_id}")
        row.enabled = "true"
        extension = session.get(ExtensionRecord, f"skill:{skill_id}")
        if extension is not None:
            extension.status = "enabled"
        record_event(session, "skill.installed", {"skill_id": skill_id})
    typer.echo(f"Installed skill: {skill_id}")


@skills_app.command("validate")
def skills_validate(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
    path: Optional[Path] = typer.Option(None, "--path", help="Validate one skill bundle path."),
) -> None:
    """Validate installed skill manifests and workflows."""
    resolved_home = (home or Path.cwd()).expanduser().resolve()
    init_database(resolved_home)
    if path is not None:
        try:
            bundle = load_skill_bundle(path.expanduser().resolve())
            typer.echo(f"OK {bundle.id}")
        except Exception as exc:
            typer.echo(f"ERROR {path}: {exc}")
            raise typer.Exit(1) from exc
        return
    failed = False
    with session_scope(resolved_home) as session:
        ensure_builtin_skills(resolved_home, session)
        rows = session.execute(select(Skill).order_by(Skill.id)).scalars().all()
        for row in rows:
            try:
                load_skill_bundle(Path(row.source_location or Path(row.workflow_path).parent))
                extension = session.get(ExtensionRecord, f"skill:{row.id}")
                if extension is not None and extension.status == "quarantined":
                    extension.status = "enabled" if row.enabled == "true" else "disabled"
                typer.echo(f"OK {row.id}")
            except Exception as exc:
                failed = True
                extension = session.get(ExtensionRecord, f"skill:{row.id}")
                if extension is not None:
                    extension.status = "quarantined"
                typer.echo(f"ERROR {row.id}: {exc}")
    if failed:
        raise typer.Exit(1)


@skills_app.command("run")
def skills_run(
    skill_id: str,
    input_json: str = typer.Option("{}", "--input", help="Skill input JSON."),
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Run an enabled skill workflow."""
    resolved_home = (home or Path.cwd()).expanduser().resolve()
    init_database(resolved_home)
    try:
        payload = json.loads(input_json)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"Invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise typer.BadParameter("--input must be a JSON object")

    with session_scope(resolved_home) as session:
        ensure_builtin_skills(resolved_home, session)
        row = session.get(Skill, skill_id)
        if row is None:
            raise typer.BadParameter(f"Unknown skill: {skill_id}")
        if row.enabled != "true":
            typer.echo(f"Skill disabled: {skill_id}")
            raise typer.Exit(1)
        workflow_path = Path(row.workflow_path)
        workflow = load_workflow_yaml(workflow_path.read_text(encoding="utf-8"))
        record_event(session, "skill.run_started", {"skill_id": skill_id})

    try:
        result = WorkflowRunner(resolved_home).run(workflow, json.dumps(payload, ensure_ascii=False), channel="skill")
    except WorkflowRunFailed as exc:
        with session_scope(resolved_home) as session:
            record_event(session, "skill.run_failed", {"skill_id": skill_id, "error": exc.message}, run_id=exc.run_id)
        typer.echo(f"Run: {exc.run_id}")
        typer.echo(f"Error: {exc.message}")
        raise typer.Exit(1) from exc
    with session_scope(resolved_home) as session:
        record_event(session, "skill.run_completed", {"skill_id": skill_id}, run_id=result.run_id)
    typer.echo(f"Run: {result.run_id}")
    typer.echo(result.output)


@skills_app.command("enable")
def skills_enable(
    skill_id: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Enable one installed skill."""
    _set_skill_enabled(home or Path.cwd(), skill_id, enabled=True)
    typer.echo(f"Enabled skill: {skill_id}")


@skills_app.command("disable")
def skills_disable(
    skill_id: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Disable one installed skill."""
    _set_skill_enabled(home or Path.cwd(), skill_id, enabled=False)
    typer.echo(f"Disabled skill: {skill_id}")


@skills_app.command("refresh")
def skills_refresh(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Refresh enabled skill markets and register discovered skills."""
    resolved_home = (home or Path.cwd()).expanduser().resolve()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        ensure_builtin_skills(resolved_home, session)
        rows = refresh_markets(resolved_home, session)
        record_event(session, "skill.market_refreshed", {"installed": [row.id for row in rows]})
        for row in rows:
            typer.echo(row.id)


@skill_effectiveness_app.command("show")
def skills_effectiveness_show(
    skill_id: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Show recorded effectiveness for one skill."""
    resolved_home = home or Path.cwd()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        rows = effectiveness_rows(session, capability_type="skill", capability_id=skill_id)
        if not rows:
            typer.echo(f"No effectiveness rows for skill: {skill_id}")
            return
        for row in rows:
            typer.echo(_effectiveness_line(row))


@skill_markets_app.command("list")
def skill_markets_list(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """List configured skill markets."""
    resolved_home = (home or Path.cwd()).expanduser().resolve()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        rows = session.execute(select(SkillMarket).order_by(SkillMarket.name)).scalars().all()
        for row in rows:
            typer.echo(f"{row.name}  backend={row.backend}  enabled={row.enabled}  location={row.location}")


@skill_markets_app.command("add")
def skill_markets_add(
    name: str,
    directory: Optional[Path] = typer.Option(None, "--directory", help="Directory market path."),
    git_location: Optional[str] = typer.Option(None, "--git", help="Git market location. Local git paths are supported."),
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Add or update a skill market."""
    if directory is None and git_location is None:
        raise typer.BadParameter("Provide either --directory or --git")
    if directory is not None and git_location is not None:
        raise typer.BadParameter("Provide only one market source")
    backend = "directory" if directory is not None else "git"
    location = str((directory.expanduser().resolve() if directory is not None else Path(git_location or "").expanduser().resolve()))
    if backend == "directory" and not Path(location).exists():
        raise typer.BadParameter(f"Directory not found: {location}")
    resolved_home = (home or Path.cwd()).expanduser().resolve()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        row = add_market(session, name, backend=backend, location=location)
        record_event(session, "skill.market_added", {"name": row.name, "backend": row.backend, "location": row.location})
    typer.echo(f"Added skill market: {name}")


@skill_markets_app.command("remove")
def skill_markets_remove(
    name: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Disable a skill market."""
    resolved_home = (home or Path.cwd()).expanduser().resolve()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        row = session.get(SkillMarket, name)
        if row is None:
            raise typer.BadParameter(f"Unknown skill market: {name}")
        row.enabled = "false"
        extension = session.get(ExtensionRecord, f"market:{name}")
        if extension is not None:
            extension.status = "removed"
        record_event(session, "extension.status_changed", {"id": f"market:{name}", "status": "removed"})
    typer.echo(f"Removed skill market: {name}")


@extensions_app.command("list")
def extensions_list(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """List registered extensions."""
    resolved_home = (home or Path.cwd()).expanduser().resolve()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        ensure_builtin_skills(resolved_home, session)
        rows = session.execute(select(ExtensionRecord).order_by(ExtensionRecord.id)).scalars().all()
        for row in rows:
            typer.echo(f"{row.id}  kind={row.kind}  status={row.status}  version={row.version}")


@extensions_app.command("show")
def extensions_show(
    extension_id: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Show one extension lifecycle record."""
    resolved_home = (home or Path.cwd()).expanduser().resolve()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        ensure_builtin_skills(resolved_home, session)
        row = session.get(ExtensionRecord, extension_id)
        if row is None:
            raise typer.BadParameter(f"Unknown extension: {extension_id}")
        payload = {
            "id": row.id,
            "kind": row.kind,
            "name": row.name,
            "status": row.status,
            "source": row.source,
            "version": row.version,
            "content_hash": row.content_hash,
            "last_validated_at": row.last_validated_at.isoformat() if row.last_validated_at else None,
        }
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


@extensions_app.command("rollback")
def extensions_rollback(
    extension_id: str,
    version: str = typer.Option(..., "--version", help="Validated extension version to restore."),
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Rollback an extension to a validated version."""
    resolved_home = (home or Path.cwd()).expanduser().resolve()
    init_database(resolved_home)
    try:
        with session_scope(resolved_home) as session:
            ensure_builtin_skills(resolved_home, session)
            rollback_extension(resolved_home, session, extension_id, version)
            record_event(session, "extension.status_changed", {"id": extension_id, "status": "enabled", "version": version})
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Rolled back extension: {extension_id} -> {version}")


@secrets_app.command("list")
def secrets_list(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """List configured secret references without printing values."""
    resolved_home = home or Path.cwd()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        for name in configured_secret_names(resolved_home):
            present = bool(os.environ.get(name))
            upsert_secret_ref(session, name, present=present)
            typer.echo(f"{name}  {'present' if present else 'missing'}  source=env")


@secrets_app.command("check")
def secrets_check(
    name: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Check whether one secret exists without printing its value."""
    resolved_home = home or Path.cwd()
    init_database(resolved_home)
    present = bool(os.environ.get(name))
    with session_scope(resolved_home) as session:
        upsert_secret_ref(session, name, present=present)
    typer.echo(f"{name}: {'present' if present else 'missing'}")


@model_routes_app.command("list")
def model_routes_list(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """List stage-based model routes."""
    resolved_home = home or Path.cwd()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        for route in list_routes(resolved_home, session):
            typer.echo(f"{route.stage}  {route.provider}/{route.model}")


@model_routes_app.command("set")
def model_routes_set(
    stage: str,
    provider: str = typer.Option(..., "--provider", help="Provider name."),
    model: str = typer.Option(..., "--model", help="Model name."),
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Set one model route."""
    with session_scope(home or Path.cwd()) as session:
        route = set_route(session, stage, provider, model)
        typer.echo(f"{route.stage}: {route.provider}/{route.model}")


@budget_app.command("set")
def budget_set(
    workflow: str = typer.Option(..., "--workflow", help="Workflow id."),
    max_llm_calls: Optional[int] = typer.Option(None, "--max-llm-calls", help="Maximum LLM calls."),
    max_input_tokens: Optional[int] = typer.Option(None, "--max-input-tokens", help="Maximum input tokens."),
    max_output_tokens: Optional[int] = typer.Option(None, "--max-output-tokens", help="Maximum output tokens."),
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Set a workflow cost budget."""
    with session_scope(home or Path.cwd()) as session:
        row = set_budget(
            session,
            workflow,
            max_llm_calls=max_llm_calls,
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
        )
        typer.echo(f"{row.workflow_id}: max_llm_calls={row.max_llm_calls or '-'}")


@budget_app.command("show")
def budget_show(
    workflow: Optional[str] = typer.Option(None, "--workflow", help="Workflow id."),
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Show workflow cost budgets."""
    with session_scope(home or Path.cwd()) as session:
        stmt = select(CostBudget)
        if workflow:
            stmt = stmt.where(CostBudget.workflow_id == workflow)
        rows = session.execute(stmt.order_by(CostBudget.workflow_id)).scalars().all()
        if not rows:
            typer.echo("No budgets.")
        for row in rows:
            usage_rows = session.execute(select(CostUsage).where(CostUsage.workflow_id == row.workflow_id)).scalars().all()
            input_tokens = sum(item.input_tokens for item in usage_rows)
            output_tokens = sum(item.output_tokens for item in usage_rows)
            total_tokens = sum(item.total_tokens for item in usage_rows)
            typer.echo(
                f"{row.workflow_id}: max_llm_calls={row.max_llm_calls or '-'} "
                f"usage_calls={len(usage_rows)} input_tokens={input_tokens} "
                f"output_tokens={output_tokens} total_tokens={total_tokens}"
            )


@eval_app.command("list")
def eval_list() -> None:
    """List built-in eval suites."""
    for suite in list_eval_suites():
        typer.echo(suite)


@eval_app.command("run")
def eval_run(
    suite: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
    skill: Optional[str] = typer.Option(None, "--skill", help="Run skills-smoke for one installed skill id."),
    skill_path: Optional[Path] = typer.Option(None, "--skill-path", help="Run skills-smoke for one local skill bundle path."),
    shared_home: bool = typer.Option(False, "--shared-home/--isolated-home", help="Use the provided home directly instead of an isolated suite home."),
) -> None:
    """Run an agent eval suite."""
    base_home = (home or Path.cwd()).expanduser().resolve()
    init_database(base_home)
    effective_home = base_home if shared_home else _isolated_eval_home(base_home, suite)
    if not shared_home:
        initialize_home(effective_home, force=True)
    else:
        init_database(effective_home)
    with session_scope(effective_home) as session:
        result = run_eval_suite(effective_home, session, suite, skill=skill, skill_path=skill_path)
        payload = result.result | {"effective_home": str(effective_home), "shared_home": shared_home}
        row = session.get(EvalRun, result.id)
        if row is not None:
            row.result_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if effective_home != base_home:
        with session_scope(base_home) as session:
            session.add(
                EvalRun(
                    id=result.id,
                    suite=result.suite,
                    status=result.status,
                    result_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
                )
            )
    typer.echo(f"Eval: {result.id}  {result.status}")


@eval_app.command("report")
def eval_report(
    eval_run_id: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Show one eval run report."""
    with session_scope(home or Path.cwd()) as session:
        row = session.get(EvalRun, eval_run_id)
        if row is None:
            raise typer.BadParameter(f"Unknown eval run: {eval_run_id}")
        typer.echo(row.result_json)


def _isolated_eval_home(base_home: Path, suite: str) -> Path:
    safe_suite = "".join(char if char.isalnum() or char in "._-" else "_" for char in suite).strip("._") or "suite"
    return base_home / "eval-homes" / f"{safe_suite}-{uuid4().hex[:12]}"


@context_policy_app.command("set")
def context_policy_set(
    scope: str = typer.Option(..., "--scope", help="Policy scope: global, project, or skill."),
    target: str = typer.Option("default", "--target", help="Policy target."),
    policy_json: Optional[str] = typer.Option(None, "--json", help="JSON policy patch."),
    max_items: Optional[int] = typer.Option(None, "--max-items", help="Maximum selected context items."),
    max_context_tokens: Optional[int] = typer.Option(None, "--max-context-tokens", help="Maximum estimated context tokens."),
    retrieve_top_k: Optional[int] = typer.Option(None, "--retrieve-top-k", help="Memory retrieval candidate count."),
    min_memory_confidence: Optional[float] = typer.Option(None, "--min-memory-confidence", help="Minimum memory confidence."),
    memory_scopes: Optional[str] = typer.Option(None, "--memory-scopes", help="Comma-separated memory scopes."),
    trusted_memory_sources: Optional[str] = typer.Option(None, "--trusted-memory-sources", help="Comma-separated trusted memory sources."),
    include_memory: Optional[bool] = typer.Option(None, "--include-memory/--no-include-memory", help="Include memory in context."),
    redact_secrets: Optional[bool] = typer.Option(None, "--redact-secrets/--no-redact-secrets", help="Redact secrets in context."),
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Create or update one context policy row."""
    policy = _context_policy_payload(
        policy_json=policy_json,
        max_items=max_items,
        max_context_tokens=max_context_tokens,
        retrieve_top_k=retrieve_top_k,
        min_memory_confidence=min_memory_confidence,
        memory_scopes=memory_scopes,
        trusted_memory_sources=trusted_memory_sources,
        include_memory=include_memory,
        redact_secrets=redact_secrets,
    )
    resolved_home = home or Path.cwd()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        try:
            row = upsert_context_policy(session, scope, target, policy)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        typer.echo(f"{row.scope}:{row.target}")
        typer.echo(row.policy_json)


@context_policy_app.command("show")
def context_policy_show(
    scope: Optional[str] = typer.Option(None, "--scope", help="Policy scope."),
    target: str = typer.Option("default", "--target", help="Policy target."),
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Show context policies."""
    resolved_home = home or Path.cwd()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        if scope:
            try:
                row = get_context_policy(session, scope, target)
            except ValueError as exc:
                raise typer.BadParameter(str(exc)) from exc
            if row is None:
                raise typer.BadParameter(f"Unknown context policy: {scope}:{target}")
            typer.echo(_context_policy_row_json(row))
            return
        rows = session.execute(select(ContextPolicy).order_by(ContextPolicy.scope, ContextPolicy.target)).scalars().all()
        if not rows:
            typer.echo("No context policies.")
        for row in rows:
            typer.echo(_context_policy_row_json(row))


@context_app.command("preview")
def context_preview(
    workflow_id: str = typer.Option(..., "--workflow", help="Workflow id."),
    input_text: str = typer.Option("", "--input", help="Workflow input text."),
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Preview the context pack without calling the LLM."""
    resolved_home = home or Path.cwd()
    init_database(resolved_home)
    workflow = WorkflowRegistry(load_config(resolved_home)).get(workflow_id)
    with session_scope(resolved_home) as session:
        pack = build_context_pack(resolved_home, workflow=workflow, user_input=input_text, session=session)
        for item in pack.selected:
            typer.echo(f"{item.item_type}  {item.source}  tokens={item.token_estimate}")
            typer.echo(item.summary)
        if pack.dropped:
            typer.echo("Dropped context items:")
        for dropped in pack.dropped:
            item = dropped.item
            typer.echo(f"{item.item_type}  {item.source}  reason={dropped.reason}  tokens={item.token_estimate}")
            typer.echo(item.summary)


@context_app.command("compact")
def context_compact(
    run_id: str = typer.Option(..., "--run", help="Run id."),
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Show existing context summaries for a run."""
    from agentend.db.models import ContextSummary

    with session_scope(home or Path.cwd()) as session:
        rows = session.execute(select(ContextSummary).where(ContextSummary.run_id == run_id)).scalars().all()
        for row in rows:
            typer.echo(f"{row.source_type} {row.source_id}: {row.summary}")


@context_ledger_app.command("show")
def context_ledger_show(
    ledger_id: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Show one context ledger and its pack items."""
    with session_scope(home or Path.cwd()) as session:
        ledger = session.get(ContextLedger, ledger_id)
        if ledger is None:
            raise typer.BadParameter(f"Unknown context ledger: {ledger_id}")
        typer.echo(f"Ledger: {ledger.id}")
        typer.echo(f"Run: {ledger.run_id}")
        typer.echo(f"Model: {ledger.model_provider}/{ledger.model_model}")
        typer.echo(f"Stage: {ledger.model_stage}")
        rows = (
            session.execute(select(ContextPackItem).where(ContextPackItem.ledger_id == ledger.id).order_by(ContextPackItem.item_type))
            .scalars()
            .all()
        )
        for row in rows:
            typer.echo(f"{row.item_type}  {row.source}  tokens={row.token_estimate}")
            typer.echo(row.summary)
        dropped_rows = (
            session.execute(select(ContextDroppedItem).where(ContextDroppedItem.ledger_id == ledger.id).order_by(ContextDroppedItem.reason))
            .scalars()
            .all()
        )
        if dropped_rows:
            typer.echo("Dropped context items:")
        for row in dropped_rows:
            typer.echo(f"{row.item_type}  {row.source}  reason={row.reason}  tokens={row.token_estimate}")
            typer.echo(row.summary)


def _context_policy_payload(
    *,
    policy_json: str | None,
    max_items: int | None,
    max_context_tokens: int | None,
    retrieve_top_k: int | None,
    min_memory_confidence: float | None,
    memory_scopes: str | None,
    trusted_memory_sources: str | None,
    include_memory: bool | None,
    redact_secrets: bool | None,
) -> dict:
    payload: dict[str, object] = {}
    if policy_json:
        try:
            parsed = json.loads(policy_json)
        except json.JSONDecodeError as exc:
            raise typer.BadParameter(f"Invalid --json: {exc}") from exc
        if not isinstance(parsed, dict):
            raise typer.BadParameter("--json must be an object")
        payload.update(parsed)
    if max_items is not None:
        payload["max_items"] = max_items
    if max_context_tokens is not None:
        payload["max_context_tokens"] = max_context_tokens
    if retrieve_top_k is not None:
        payload["retrieve_top_k"] = retrieve_top_k
    if min_memory_confidence is not None:
        payload["min_memory_confidence"] = min_memory_confidence
    if memory_scopes is not None:
        payload["memory_scopes"] = _csv_list(memory_scopes)
    if trusted_memory_sources is not None:
        payload["trusted_memory_sources"] = _csv_list(trusted_memory_sources)
    if include_memory is not None:
        payload["include_memory"] = include_memory
    if redact_secrets is not None:
        payload["redact_secrets"] = redact_secrets
    return payload


def _csv_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _context_policy_row_json(row: ContextPolicy) -> str:
    payload = {
        "id": row.id,
        "scope": row.scope,
        "target": row.target,
        "policy": json.loads(row.policy_json),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


@memory_app.command("write")
def memory_write(
    content: str = typer.Option(..., "--content", help="Memory content."),
    scope: str = typer.Option("default", "--scope", help="Memory scope."),
    source: str = typer.Option("manual", "--source", help="Memory source."),
    confidence: str = typer.Option("1.0", "--confidence", help="Memory confidence."),
    ttl: Optional[str] = typer.Option(None, "--ttl", help="Optional ISO timestamp expiration."),
    tags: str = typer.Option("", "--tags", help="Comma-separated tags."),
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Write a local memory item."""
    resolved_home = home or Path.cwd()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        try:
            memory = write_memory_item(
                session,
                resolved_home,
                content=content,
                scope=scope,
                source=source,
                confidence=confidence,
                ttl=ttl,
                tags=[tag.strip() for tag in tags.split(",") if tag.strip()],
            )
        except PermissionError as exc:
            typer.echo(f"Error: {exc}")
            raise typer.Exit(1) from exc
        typer.echo(f"Memory: {memory.id}")


@memory_app.command("search")
def memory_search(
    query: str,
    scope: Optional[str] = typer.Option(None, "--scope", help="Optional memory scope."),
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Search local memory items."""
    resolved_home = home or Path.cwd()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        rows = search_memory_items(session, query, scope=scope)
        for row in rows:
            typer.echo(f"{row.id}  {row.scope}  {row.content}")


@memory_app.command("list")
def memory_list(
    scope: Optional[str] = typer.Option(None, "--scope", help="Optional memory scope."),
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """List active memory items."""
    resolved_home = home or Path.cwd()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        stmt = select(MemoryItem).where(MemoryItem.status == "active")
        if scope:
            stmt = stmt.where(MemoryItem.scope == scope)
        for row in session.execute(stmt.order_by(MemoryItem.created_at)).scalars().all():
            typer.echo(f"{row.id}  {row.scope}  {row.content}")


@memory_app.command("candidates")
def memory_candidates(
    agent_run_id: Optional[str] = typer.Option(None, "--agent-run", help="AgentRun id."),
    run_id: Optional[str] = typer.Option(None, "--run", help="Workflow run id."),
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """List memory candidates extracted from an agent or workflow run."""
    if not agent_run_id and not run_id:
        raise typer.BadParameter("Provide --agent-run or --run")
    resolved_home = home or Path.cwd()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        try:
            rows = extract_memory_candidates(session, agent_run_id=agent_run_id, run_id=run_id)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        typer.echo(json.dumps([memory_candidate_to_dict(row) for row in rows], ensure_ascii=False, indent=2, sort_keys=True))


@memory_app.command("consolidate")
def memory_consolidate(
    agent_run_id: Optional[str] = typer.Option(None, "--agent-run", help="AgentRun id."),
    run_id: Optional[str] = typer.Option(None, "--run", help="Workflow run id."),
    auto_relations: bool = typer.Option(True, "--auto-relations/--no-auto-relations", help="Classify related active memories before creating new long-term memory."),
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Merge pending memory candidates into the Memory Store."""
    resolved_home = home or Path.cwd()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        try:
            result = consolidate_memory_candidates(
                session,
                agent_run_id=agent_run_id,
                run_id=run_id,
                auto_relations=auto_relations,
                home=resolved_home,
            )
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        typer.echo(
            "Memory consolidation: "
            f"candidates={result.candidate_count} "
            f"created={result.created_count} "
            f"merged={result.merged_count} "
            f"skipped={result.skipped_count}"
            f" superseded={result.superseded_count}"
            f" conflicts={result.conflict_count}"
            f" reinforced={result.reinforced_count}"
            f" needs_review={result.needs_review_count}"
            f" conflict_candidates={result.conflict_candidate_count}"
        )
        for memory_id in result.memory_ids:
            typer.echo(f"Memory: {memory_id}")


@memory_app.command("digest")
def memory_digest(
    max_items: int = typer.Option(12, "--max-items", min=1, help="Maximum source memories to include."),
    max_chars: int = typer.Option(1200, "--max-chars", min=120, help="Maximum digest content length."),
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Compile or update the project memory digest."""
    resolved_home = home or Path.cwd()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        digest = compile_project_memory_digest(session, max_items=max_items, max_chars=max_chars)
        payload = {
            "id": digest.id,
            "scope": digest.scope,
            "source": digest.source,
            "confidence": digest.confidence,
            "tags": json.loads(digest.tags_json),
            "content": digest.content,
        }
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


@memory_app.command("lint")
def memory_lint(
    max_content_chars: int = typer.Option(1200, "--max-content-chars", min=120, help="Maximum active memory content length."),
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Report memory quality issues."""
    resolved_home = home or Path.cwd()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        issues = lint_memory_items(session, max_content_chars=max_content_chars)
        typer.echo(json.dumps(issues, ensure_ascii=False, indent=2, sort_keys=True))


@memory_app.command("edit")
def memory_edit(
    memory_id: str,
    content: str = typer.Option(..., "--content", help="New content."),
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Edit a memory item."""
    resolved_home = home or Path.cwd()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        memory = session.get(MemoryItem, memory_id)
        if memory is None:
            raise typer.BadParameter(f"Unknown memory: {memory_id}")
        edit_memory_item(session, resolved_home, memory, content)
        typer.echo(f"Memory: {memory.id}")


@memory_app.command("forget")
def memory_forget(
    memory_id: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Soft-delete a memory item."""
    resolved_home = home or Path.cwd()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        memory = session.get(MemoryItem, memory_id)
        if memory is None:
            raise typer.BadParameter(f"Unknown memory: {memory_id}")
        forget_memory_item(session, memory)
        typer.echo(f"Memory forgotten: {memory.id}")


@checkpoints_app.command("list")
def checkpoints_list(
    run_id: str = typer.Option(..., "--run", help="Run id."),
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """List checkpoints for a run."""
    resolved_home = home or Path.cwd()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        rows = session.execute(select(Checkpoint).where(Checkpoint.run_id == run_id).order_by(Checkpoint.created_at)).scalars().all()
        for row in rows:
            typer.echo(f"{row.id}  node={row.node_id}  step={row.step_id}")


@clarifications_app.command("list")
def clarifications_list(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
    status: Optional[str] = typer.Option("pending", "--status", help="Filter by status. Use 'all' for every status."),
) -> None:
    """List clarification requests."""
    resolved_home = home or Path.cwd()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        query = (
            select(ClarificationRequest, Conversation)
            .join(Run, ClarificationRequest.run_id == Run.id)
            .join(Conversation, Run.conversation_id == Conversation.id)
            .order_by(ClarificationRequest.created_at.desc())
        )
        if status and status != "all":
            query = query.where(ClarificationRequest.status == status)
        rows = session.execute(query).all()
        for row, conversation in rows:
            typer.echo(
                f"{row.id}  {row.status}  channel={conversation.channel}  "
                f"user={conversation.external_user_id}  type={row.request_type}  run={row.run_id}  {row.question}"
            )


@clarifications_app.command("show")
def clarifications_show(
    request_id: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Show one clarification request."""
    resolved_home = home or Path.cwd()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        row = session.get(ClarificationRequest, request_id)
        if row is None:
            raise typer.BadParameter(f"Unknown clarification request: {request_id}")
        run = session.get(Run, row.run_id)
        conversation = session.get(Conversation, run.conversation_id) if run is not None else None
        payload = {
            "id": row.id,
            "run_id": row.run_id,
            "step_id": row.step_id,
            "channel": conversation.channel if conversation is not None else None,
            "external_user_id": conversation.external_user_id if conversation is not None else None,
            "type": row.request_type,
            "question": row.question,
            "reason": row.reason,
            "choices": json.loads(row.choices_json),
            "free_text_allowed": row.free_text_allowed == "true",
            "status": row.status,
            "answer": row.answer,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        }
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


@workspace_app.command("index")
def workspace_index(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Index lightweight project context files."""
    resolved_home = home or Path.cwd()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        rows = index_workspace(resolved_home, session)
        typer.echo(f"Indexed workspace files: {len(rows)}")


@workspace_app.command("summary")
def workspace_summary_cmd(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Show indexed workspace context."""
    resolved_home = home or Path.cwd()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        rows = workspace_summary(session)
        if not rows:
            rows = index_workspace(resolved_home, session)
        for row in rows:
            typer.echo(f"## {row.source_path}")
            typer.echo(row.summary)


@project_profile_app.command("show")
def project_profile_show(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Show the local project profile."""
    resolved_home = home or Path.cwd()
    path = resolved_home / "agentend.project.md"
    if not path.exists():
        path.write_text("# AgentEnd Project Profile\n", encoding="utf-8")
    with session_scope(resolved_home) as session:
        row = session.get(ProjectProfile, "default")
        if row is None:
            row = ProjectProfile(id="default", path=str(path), content=path.read_text(encoding="utf-8"))
            session.add(row)
        typer.echo(row.content)


@project_profile_app.command("edit")
def project_profile_edit(
    content: str = typer.Option(..., "--content", help="New profile content."),
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Update the local project profile content."""
    resolved_home = home or Path.cwd()
    path = resolved_home / "agentend.project.md"
    path.write_text(content, encoding="utf-8")
    with session_scope(resolved_home) as session:
        row = session.get(ProjectProfile, "default")
        if row is None:
            row = ProjectProfile(id="default", path=str(path), content=content)
            session.add(row)
        else:
            row.path = str(path)
            row.content = content
        typer.echo(str(path))


@artifacts_app.command("list")
def artifacts_list(
    run_id: str = typer.Option(..., "--run", help="Run id."),
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """List artifacts for a run."""
    with session_scope(home or Path.cwd()) as session:
        rows = session.execute(select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.created_at)).scalars().all()
        for row in rows:
            typer.echo(f"{row.id}  {row.kind}  {row.path}  bytes={row.size_bytes}")


@artifacts_app.command("show")
def artifacts_show(
    artifact_id: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Show artifact metadata and text content when readable."""
    with session_scope(home or Path.cwd()) as session:
        artifact = session.get(Artifact, artifact_id)
        if artifact is None:
            raise typer.BadParameter(f"Unknown artifact: {artifact_id}")
        typer.echo(f"Artifact: {artifact.id}")
        typer.echo(f"Path: {artifact.path}")
        path = Path(artifact.path)
        if path.exists() and path.is_file():
            try:
                typer.echo(path.read_text(encoding="utf-8"))
            except UnicodeDecodeError:
                typer.echo("<binary artifact>")


@runs_app.command("export")
def runs_export(
    run_id: str,
    output: Path = typer.Option(..., "--output", "-o", help="Export directory."),
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Export one run with steps, tool calls, and artifact metadata."""
    resolved_home = home or Path.cwd()
    init_database(resolved_home)
    export_root = output / run_id
    export_root.mkdir(parents=True, exist_ok=True)
    with session_scope(resolved_home) as session:
        run = session.get(Run, run_id)
        if run is None:
            raise typer.BadParameter(f"Unknown run: {run_id}")
        steps = session.execute(select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.created_at)).scalars().all()
        tool_calls = session.execute(select(ToolCall).where(ToolCall.run_id == run_id).order_by(ToolCall.created_at)).scalars().all()
        artifacts = session.execute(select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.created_at)).scalars().all()
        contract_snapshots = (
            session.execute(select(ToolContractSnapshot).where(ToolContractSnapshot.run_id == run_id).order_by(ToolContractSnapshot.tool_name))
            .scalars()
            .all()
        )
        contract_payload = [snapshot_to_dict(snapshot) for snapshot in contract_snapshots]
        evidence_manifest = evidence_manifest_for_run(session, resolved_home, run_id)
        intent_decisions = (
            session.execute(
                select(IntentDecisionRecord)
                .where(IntentDecisionRecord.run_id == run_id)
                .order_by(IntentDecisionRecord.created_at)
            )
            .scalars()
            .all()
        )
        payload = {
            "run": {"id": run.id, "status": run.status, "workflow_id": run.workflow_id, "error": run.error},
            "steps": [{"id": step.id, "node_id": step.node_id, "status": step.status, "error": step.error} for step in steps],
            "tool_calls": [
                {"id": call.id, "tool_name": call.tool_name, "status": call.status, "error": call.error}
                for call in tool_calls
            ],
            "tool_contract_snapshots": contract_payload,
            "artifacts": [{"id": artifact.id, "path": artifact.path, "kind": artifact.kind} for artifact in artifacts],
            "intent_decisions": [intent_record_to_dict(row) for row in intent_decisions],
            "evidence_manifest": evidence_manifest,
        }
        (export_root / "run.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        (export_root / "tool_contracts.json").write_text(json.dumps(contract_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        (export_root / "evidence_manifest.json").write_text(json.dumps(evidence_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        row = RunExport(id=str(uuid4()), run_id=run_id, output_path=str(export_root), metadata_json=json.dumps(payload))
        session.add(row)
        typer.echo(str(export_root))


@storage_app.command("usage")
def storage_usage(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Show local AgentEnd storage usage."""
    resolved_home = home or Path.cwd()
    init_database(resolved_home)
    for row in storage_usage_report(resolved_home):
        typer.echo(f"{row['name']}: {row['size_bytes']} bytes")


@storage_app.command("cleanup")
def storage_cleanup(
    older_than: str = typer.Option(..., "--older-than", help="Age threshold, for example 30d."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview cleanup without deleting."),
    confirm: bool = typer.Option(False, "--confirm", help="Delete planned cleanup targets."),
    plan_id: Optional[str] = typer.Option(None, "--plan-id", help="Execute a previously recorded dry-run plan."),
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Run storage cleanup with dry-run planning or explicit confirmation."""
    if dry_run and confirm:
        raise typer.BadParameter("Use either --dry-run or --confirm, not both")
    if plan_id and dry_run:
        raise typer.BadParameter("--plan-id can only be used with --confirm")
    if not dry_run and not confirm:
        raise typer.BadParameter("storage cleanup requires --dry-run or --confirm")
    resolved_home = home or Path.cwd()
    init_database(resolved_home)
    try:
        with session_scope(resolved_home) as session:
            plan = load_cleanup_plan(session, plan_id) if plan_id else build_cleanup_plan(resolved_home, session, older_than=older_than)
            if dry_run:
                row = record_cleanup_run(session, mode="dry-run", plan=plan)
                _echo_cleanup_plan(row.plan_id or str(plan["plan_id"]), list(plan.get("items", [])), int(plan.get("total_bytes", 0)))
                return

            results = execute_cleanup_plan(resolved_home, session, list(plan.get("items", [])))
            errors = [item for item in results if item.get("status") == "error"]
            row = record_cleanup_run(
                session,
                mode="completed",
                plan=plan,
                results=results,
                source_plan_id=plan_id,
                status="failed" if errors else "completed",
            )
            typer.echo(
                f"cleanup completed: plan={row.plan_id} source_plan={row.source_plan_id or '-'} "
                f"deleted={row.deleted_count} bytes={row.total_bytes}"
            )
            for item in results:
                label = item.get("path") or f"{item.get('table')}:{item.get('row_id')}"
                typer.echo(f"{item.get('status')}  {item.get('size_bytes')} bytes  {item.get('reason')}  {label}")
            if errors:
                raise typer.Exit(1)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


@storage_app.command("backup")
def storage_backup(
    output: Path = typer.Option(..., "--output", "-o", help="Backup output directory."),
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Backup SQLite and key local metadata."""
    resolved_home = home or Path.cwd()
    init_database(resolved_home)
    output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(database_path(resolved_home), output / "agentend.sqlite")
    typer.echo(str(output))


@storage_app.command("restore")
def storage_restore(
    backup_path: Path,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Restore SQLite from a backup directory into a new home."""
    resolved_home = home or Path.cwd()
    source = backup_path / "agentend.sqlite" if backup_path.is_dir() else backup_path
    if not source.exists():
        raise typer.BadParameter(f"Backup not found: {source}")
    target = database_path(resolved_home)
    if target.exists():
        raise typer.BadParameter(f"Refusing to overwrite existing AgentEnd database: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    typer.echo(f"Restored: {target}")


def _echo_cleanup_plan(plan_id: str, items: list[dict[str, object]], total_bytes: int) -> None:
    typer.echo(f"cleanup dry-run: plan={plan_id} items={len(items)} bytes={total_bytes}")
    for item in items:
        label = item.get("path") or f"{item.get('table')}:{item.get('row_id')}"
        typer.echo(f"planned  {item.get('size_bytes')} bytes  {item.get('reason')}  {label}")


@capabilities_app.command("refresh")
def capabilities_refresh(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Refresh the capability map from enabled tool contracts."""
    resolved_home = home or Path.cwd()
    init_database(resolved_home)
    registry = ToolRegistry(resolved_home)
    with session_scope(resolved_home) as session:
        ensure_builtin_skills(resolved_home, session)
        sync_tool_manifests(session, registry.manifests())
        rows = refresh_capabilities(session)
        typer.echo(f"Capabilities refreshed: {len(rows)}")


@capabilities_app.command("list")
def capabilities_list(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """List capability records."""
    with session_scope(home or Path.cwd()) as session:
        rows = session.execute(select(Capability).order_by(Capability.name)).scalars().all()
        for row in rows:
            typer.echo(f"{row.name}  source={row.source}  side_effect={row.side_effect}")


@capabilities_app.command("query")
def capabilities_query(
    query: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Query capability records."""
    resolved_home = home or Path.cwd()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        if not session.execute(select(Capability)).first():
            ensure_builtin_skills(resolved_home, session)
            sync_tool_manifests(session, ToolRegistry(resolved_home).manifests())
            refresh_capabilities(session)
        for row in query_capabilities(session, query):
            typer.echo(f"{row.name}  {row.action_summary}")


@capability_effectiveness_app.command("show")
def capabilities_effectiveness_show(
    capability_id: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Show recorded effectiveness for a capability."""
    resolved_home = home or Path.cwd()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        rows = effectiveness_rows(session, capability_id=capability_id)
        if not rows:
            typer.echo(f"No effectiveness rows for capability: {capability_id}")
            return
        for row in rows:
            typer.echo(_effectiveness_line(row))


@sources_app.command("list")
def sources_list(
    run_id: str = typer.Option(..., "--run", help="Run id."),
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """List sources used by a run."""
    with session_scope(home or Path.cwd()) as session:
        rows = session.execute(select(SourceRecord).where(SourceRecord.used_by_run_id == run_id)).scalars().all()
        for row in rows:
            typer.echo(f"{row.id}  {row.title or '-'}  {row.url or row.path or '-'}")


@sources_app.command("show")
def sources_show(
    source_id: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Show source metadata."""
    with session_scope(home or Path.cwd()) as session:
        row = session.get(SourceRecord, source_id)
        if row is None:
            raise typer.BadParameter(f"Unknown source: {source_id}")
        typer.echo(f"Source: {row.id}")
        typer.echo(f"Type: {row.source_type}")
        typer.echo(f"Title: {row.title or '-'}")
        typer.echo(f"URL: {row.url or '-'}")
        typer.echo(row.quote)


@episodes_app.command("summarize")
def episodes_summarize(
    run_id: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Summarize one run into an episode."""
    resolved_home = (home or Path.cwd()).expanduser().resolve()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        episode = summarize_run(resolved_home, session, run_id)
        typer.echo(f"Episode: {episode.id}")
        typer.echo(episode.summary)


@episodes_app.command("list")
def episodes_list(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """List episodes."""
    resolved_home = (home or Path.cwd()).expanduser().resolve()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        rows = session.execute(select(Episode).order_by(Episode.created_at.desc())).scalars().all()
        for row in rows:
            typer.echo(f"{row.id}  run={row.run_id}  status={row.status}  workflow={row.workflow_id or '-'}")


@episodes_app.command("show")
def episodes_show(
    episode_id: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Show one episode with tools and artifacts."""
    resolved_home = (home or Path.cwd()).expanduser().resolve()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        row = session.get(Episode, episode_id)
        if row is None:
            raise typer.BadParameter(f"Unknown episode: {episode_id}")
        typer.echo(json.dumps(episode_to_dict(session, row), ensure_ascii=False, indent=2, sort_keys=True))


@episodes_app.command("promote")
def episodes_promote(
    episode_id: str,
    skill_id: str = typer.Option(..., "--skill-id", help="Skill id for the generated draft."),
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Promote a successful episode into a local skill draft."""
    resolved_home = (home or Path.cwd()).expanduser().resolve()
    init_database(resolved_home)
    try:
        with session_scope(resolved_home) as session:
            draft = promote_episode_to_skill(resolved_home, session, episode_id, skill_id)
            typer.echo(f"Skill draft: {draft.path}")
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc


def _set_tool_enabled(home: Path, tool_name: str, *, enabled: bool) -> None:
    init_database(home)
    registry = ToolRegistry(home)
    with session_scope(home) as session:
        sync_tool_manifests(session, registry.manifests())
        row = session.get(ToolManifest, tool_name)
        if row is None:
            raise typer.BadParameter(f"Unknown tool: {tool_name}")
        row.enabled = "true" if enabled else "false"


def _set_skill_enabled(home: Path, skill_id: str, *, enabled: bool) -> None:
    resolved_home = home.expanduser().resolve()
    init_database(resolved_home)
    with session_scope(resolved_home) as session:
        ensure_builtin_skills(resolved_home, session)
        row = session.get(Skill, skill_id)
        if row is None:
            raise typer.BadParameter(f"Unknown skill: {skill_id}")
        row.enabled = "true" if enabled else "false"
        extension = session.get(ExtensionRecord, f"skill:{skill_id}")
        if extension is not None:
            extension.status = "enabled" if enabled else "disabled"
        record_event(session, "skill.enabled" if enabled else "skill.disabled", {"skill_id": skill_id})
        record_event(
            session,
            "extension.status_changed",
            {"id": f"skill:{skill_id}", "status": "enabled" if enabled else "disabled"},
        )


def _skill_manifest_dict(row: Skill) -> dict:
    try:
        manifest = json.loads(row.manifest_json)
    except json.JSONDecodeError:
        manifest = {}
    manifest.update(
        {
            "id": row.id,
            "version": row.version,
            "description": row.description,
            "workflow_path": row.workflow_path,
            "required_tools": json.loads(row.required_tools_json),
            "required_mcp": json.loads(row.required_mcp_json),
            "enabled": row.enabled == "true",
            "source": {"type": row.source_type, "location": row.source_location},
        }
    )
    return manifest


def _effectiveness_line(row) -> str:
    payload = effectiveness_summary_dict(row)
    return (
        f"{payload['capability_id']}  "
        f"type={payload['capability_type']}  "
        f"goal_type={payload['goal_type']}  "
        f"attempts={payload['attempts']}  "
        f"successes={payload['successes']}  "
        f"failures={payload['failures']}  "
        f"blocked={payload['blocked']}  "
        f"avg_duration_ms={payload['avg_duration_ms']}"
    )


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
    agent: bool = typer.Option(False, "--agent", help="Use the explicit AgentRun loop instead of the default workflow chat."),
) -> None:
    """Start a local CLI conversation."""
    resolved_home = home or Path.cwd()
    service = ConversationService(resolved_home)
    if message is not None:
        if agent:
            result = AgentRunController(resolved_home).run(message, channel="cli", external_user_id="local")
            typer.echo(f"AgentRun: {result.agent_run_id}")
            typer.echo(f"Status: {result.status}")
            if result.linked_run_id:
                typer.echo(f"Run: {result.linked_run_id}")
            typer.echo(result.content)
            return
        response = service.handle_message("cli", "local", message)
        if response.run_id:
            typer.echo(f"Run: {response.run_id}")
        if response.agent_run_id:
            typer.echo(f"AgentRun: {response.agent_run_id}")
            typer.echo(f"Status: {response.status}")
        typer.echo(response.content)
        return

    typer.echo("AgentEnd chat. Type /exit to quit.")
    while True:
        text = typer.prompt("You")
        if text.strip() in {"/exit", "exit", "quit"}:
            break
        if agent:
            result = AgentRunController(resolved_home).run(text, channel="cli", external_user_id="local")
            typer.echo(f"AgentRun: {result.agent_run_id}")
            typer.echo(result.content)
            continue
        response = service.handle_message("cli", "local", text)
        if response.run_id:
            typer.echo(f"Run: {response.run_id}")
        if response.agent_run_id:
            typer.echo(f"AgentRun: {response.agent_run_id}")
            typer.echo(f"Status: {response.status}")
        typer.echo(response.content)


@app.command("serve")
def serve(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
    once: bool = typer.Option(False, "--once", help="Process one worker tick and exit."),
    poll_interval: int = typer.Option(10, "--poll-interval", help="Polling interval in seconds."),
    max_concurrency: int = typer.Option(1, "--max-concurrency", help="Worker concurrency. First slice supports 1."),
) -> None:
    """Run the local long-task worker loop."""
    if max_concurrency != 1:
        raise typer.BadParameter("--max-concurrency currently only supports 1")
    resolved_home = home or Path.cwd()
    worker = AgentWorker(resolved_home)
    if once:
        result = worker.run_once()
        if result.processed_tasks:
            typer.echo(
                f"processed={result.processed_tasks} "
                f"created={result.created_tasks} "
                f"schedules={result.schedule_count}"
            )
            for agent_run_id in result.agent_run_ids:
                typer.echo(f"AgentRun: {agent_run_id}")
        else:
            typer.echo(f"no work created={result.created_tasks} schedules={result.schedule_count}")
        return
    worker.run_forever(poll_interval=poll_interval, max_concurrency=max_concurrency)


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
    base_url: Optional[str] = typer.Option(None, "--base-url", help="OpenAI-compatible base URL."),
    api_key_env: Optional[str] = typer.Option(None, "--api-key-env", help="Environment variable that stores the provider API key."),
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Set the active LLM provider and model."""
    config = set_llm_config(home or Path.cwd(), provider=provider, model=model, base_url=base_url, api_key_env=api_key_env)
    typer.echo(f"LLM set: {config.llm.provider}/{config.llm.model}")
    if config.llm.provider_config.api_key_env:
        typer.echo(f"API key env: {config.llm.provider_config.api_key_env}")
    if config.llm.provider_config.base_url:
        typer.echo(f"Base URL: {config.llm.provider_config.base_url}")


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
    agent_run_id: Optional[str] = typer.Argument(None, help="Optional AgentRun id. Omit to print the agent profile."),
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Print the current local agent profile or one AgentRun."""
    resolved_home = home or Path.cwd()
    if agent_run_id is None:
        profile = load_agent_profile(load_config(resolved_home))
        typer.echo(profile.content)
        return
    try:
        payload = AgentRunController(resolved_home).show(agent_run_id)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))


@agent_app.command("run")
def agent_run(
    goal: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
    max_iterations: int = typer.Option(3, "--max-iterations", help="Maximum agent loop iterations."),
) -> None:
    """Run a goal through the explicit AgentRunController loop."""
    resolved_home = home or Path.cwd()
    try:
        result = AgentRunController(resolved_home).run(goal, max_iterations=max_iterations, channel="cli", external_user_id="local")
    except Exception as exc:
        typer.echo(f"Error: {exc}")
        raise typer.Exit(1) from exc
    typer.echo(f"AgentRun: {result.agent_run_id}")
    typer.echo(f"Status: {result.status}")
    typer.echo(f"Stop reason: {result.stop_reason}")
    if result.linked_run_id:
        typer.echo(f"Run: {result.linked_run_id}")
    if result.progress_artifact_id:
        typer.echo(f"Progress: {result.progress_artifact_id}")
    typer.echo(result.content)


@agent_app.command("iterations")
def agent_iterations(
    agent_run_id: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """List iterations for one AgentRun."""
    try:
        rows = AgentRunController(home or Path.cwd()).iterations(agent_run_id)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    for row in rows:
        typer.echo(json.dumps(row, ensure_ascii=False, sort_keys=True))


@agent_app.command("resume")
def agent_resume(
    agent_run_id: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
    max_iterations: int = typer.Option(3, "--max-iterations", help="Maximum resumed loop iterations."),
) -> None:
    """Resume an existing AgentRun by appending new iterations."""
    resolved_home = home or Path.cwd()
    try:
        result = AgentRunController(resolved_home).resume(agent_run_id, max_iterations=max_iterations)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"AgentRun: {result.agent_run_id}")
    typer.echo(f"Status: {result.status}")
    typer.echo(result.content)


@agent_app.command("cancel")
def agent_cancel(
    agent_run_id: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Mark an AgentRun cancelled."""
    try:
        payload = AgentRunController(home or Path.cwd()).cancel(agent_run_id)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))


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


@clipboard_app.command("read")
def clipboard_read(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Read text from the system clipboard or AGENTEND_CLIPBOARD_FILE."""
    _ = home
    try:
        typer.echo(read_clipboard())
    except ClipboardUnavailable as exc:
        typer.echo(f"Error: {exc}")
        raise typer.Exit(1) from exc


@clipboard_app.command("write")
def clipboard_write(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
    text: Optional[str] = typer.Option(None, "--text", help="Text to write."),
    stdin_input: bool = typer.Option(False, "--stdin", help="Read clipboard text from stdin."),
) -> None:
    """Write text to the system clipboard or AGENTEND_CLIPBOARD_FILE."""
    _ = home
    if stdin_input:
        payload = typer.get_text_stream("stdin").read()
    elif text is not None:
        payload = text
    else:
        raise typer.BadParameter("Provide --text or --stdin")
    try:
        write_clipboard(payload)
        typer.echo("Clipboard written")
    except ClipboardUnavailable as exc:
        typer.echo(f"Error: {exc}")
        raise typer.Exit(1) from exc


@inbox_app.command("watch")
def inbox_watch(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
    workflow_id: str = typer.Option(..., "--workflow", help="Workflow id for new file tasks."),
    inbox_dir: Optional[Path] = typer.Option(None, "--inbox-dir", help="Inbox directory. Defaults to data/inbox."),
    once: bool = typer.Option(False, "--once", help="Scan once and exit."),
    interval_seconds: float = typer.Option(2.0, "--interval", help="Polling interval for watch mode."),
    limit: int = typer.Option(DEFAULT_INBOX_BATCH_LIMIT, "--limit", help="Maximum files to enqueue per scan."),
    backoff_seconds: float = typer.Option(2.0, "--backoff", help="Initial retry delay after inbox scan errors."),
) -> None:
    """Watch the local file inbox and create tasks for newly detected files."""
    if limit < 1:
        raise typer.BadParameter("--limit must be at least 1")
    if backoff_seconds < 0:
        raise typer.BadParameter("--backoff must be zero or greater")
    manager = TaskManager(home or Path.cwd())

    def scan() -> None:
        created = manager.scan_inbox_once(workflow_id=workflow_id, inbox_dir=inbox_dir, limit=limit)
        if not created:
            typer.echo("No new inbox files")
            return
        for task in created:
            typer.echo(f"Task: {task.id}  status={task.status}  file={task.source_path}")

    if once:
        scan()
        return
    current_backoff = backoff_seconds
    while True:
        try:
            scan()
            current_backoff = backoff_seconds
            time.sleep(interval_seconds)
        except Exception as exc:
            typer.echo(f"Inbox scan error: {exc}")
            time.sleep(current_backoff)
            current_backoff = min(max(current_backoff * 2, 1.0), 60.0)


@tasks_app.command("add")
def tasks_add(
    goal: str = typer.Argument(..., help="Task goal or input text."),
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
    workflow_id: str = typer.Option("simple_chat", "--workflow", help="Workflow id."),
    input_text: Optional[str] = typer.Option(None, "--input", help="Workflow input text. Defaults to goal."),
) -> None:
    """Add a local pending task."""
    task = TaskManager(home or Path.cwd()).add_task(
        workflow_id=workflow_id,
        input_text=input_text if input_text is not None else goal,
        title=goal,
        source="manual",
    )
    typer.echo(f"Task: {task.id}  status={task.status}  workflow={task.workflow_id}")


@tasks_app.command("list")
def tasks_list(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
    status: Optional[str] = typer.Option(None, "--status", help="Filter by task status."),
) -> None:
    """List local tasks."""
    tasks = TaskManager(home or Path.cwd()).list_tasks(status=status)
    for task in tasks:
        run = f"  run={task.run_id}" if task.run_id else ""
        source = f"  source={task.source}" if task.source else ""
        typer.echo(f"{task.id}  {task.status}  workflow={task.workflow_id}{run}{source}  {task.title}")


@tasks_app.command("run")
def tasks_run(
    task_id: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Run one pending task."""
    outcome = TaskManager(home or Path.cwd()).run_task(task_id)
    typer.echo(f"Task: {outcome.task_id}  status={outcome.status}")
    if outcome.run_id:
        typer.echo(f"Run: {outcome.run_id}")
    if outcome.error:
        typer.echo(f"Error: {outcome.error}")
        raise typer.Exit(1)
    typer.echo(outcome.output)


@tasks_app.command("resume")
def tasks_resume(
    task_id: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
    message: Optional[str] = typer.Option(None, "--message", help="Replace task input before resuming."),
    run_now: bool = typer.Option(False, "--run", help="Run immediately after marking pending."),
) -> None:
    """Mark a failed or blocked task as pending, optionally replacing input."""
    manager = TaskManager(home or Path.cwd())
    task = manager.resume_task(task_id, message=message)
    typer.echo(f"Task: {task.id}  status={task.status}")
    if run_now:
        outcome = manager.run_task(task.id)
        typer.echo(f"Run: {outcome.run_id}")
        typer.echo(outcome.output if not outcome.error else outcome.error)
        if outcome.error:
            raise typer.Exit(1)


@schedule_app.command("add")
def schedule_add(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
    workflow_id: str = typer.Option(..., "--workflow", help="Workflow id."),
    cron: str = typer.Option(..., "--cron", help="Cron expression."),
    input_text: str = typer.Option("", "--input", help="Workflow input text."),
    paused: bool = typer.Option(False, "--paused", help="Create schedule in paused state."),
    max_failures: int = typer.Option(
        DEFAULT_SCHEDULE_FAILURE_THRESHOLD,
        "--max-failures",
        help="Consecutive failures before auto-pausing the schedule.",
    ),
) -> None:
    """Add a local schedule definition."""
    try:
        schedule = TaskManager(home or Path.cwd()).add_schedule(
            workflow_id=workflow_id,
            cron=cron,
            input_text=input_text,
            status="paused" if paused else "active",
            max_consecutive_failures=max_failures,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Schedule: {schedule.id}  status={schedule.status}  workflow={schedule.workflow_id}  cron={schedule.cron}")


@schedule_app.command("validate")
def schedule_validate(
    cron: str = typer.Option(..., "--cron", help="Cron expression to validate."),
) -> None:
    """Validate a local schedule cron expression."""
    try:
        validate_cron(cron)
    except ValueError as exc:
        typer.echo(f"Invalid cron: {exc}")
        raise typer.Exit(1) from exc
    typer.echo(f"Valid cron: {cron}")


@schedule_app.command("list")
def schedule_list(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """List local schedules."""
    schedules = TaskManager(home or Path.cwd()).list_schedules()
    for schedule in schedules:
        last = f"  last_run={schedule.last_run_id}" if schedule.last_run_id else ""
        failures = f"  failures={schedule.consecutive_failures}/{schedule.max_consecutive_failures}"
        reason = f"  reason={schedule.paused_reason}" if schedule.paused_reason else ""
        typer.echo(
            f"{schedule.id}  {schedule.status}  workflow={schedule.workflow_id}  cron={schedule.cron}{last}{failures}{reason}"
        )


@schedule_app.command("remove")
def schedule_remove(
    schedule_id: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Remove a local schedule."""
    TaskManager(home or Path.cwd()).remove_schedule(schedule_id)
    typer.echo(f"Removed schedule: {schedule_id}")


@schedule_app.command("run-now")
def schedule_run_now(
    schedule_id: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Trigger one schedule immediately."""
    outcome = TaskManager(home or Path.cwd()).run_schedule_now(schedule_id)
    typer.echo(f"Schedule: {outcome.schedule_id}  status={outcome.status}")
    typer.echo(f"Task: {outcome.task_id}")
    if outcome.run_id:
        typer.echo(f"Run: {outcome.run_id}")
    if outcome.error:
        typer.echo(f"Error: {outcome.error}")
        raise typer.Exit(1)
    typer.echo(outcome.output)


@schedule_app.command("tick")
def schedule_tick(
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
    now: Optional[str] = typer.Option(None, "--now", help="ISO timestamp used for deterministic scheduler tests."),
) -> None:
    """Run all schedules due at the current or supplied time."""
    current = datetime.fromisoformat(now) if now else None
    outcomes = TaskManager(home or Path.cwd()).run_due_schedules(current)
    if not outcomes:
        typer.echo("No due schedules")
        return
    failed = False
    for outcome in outcomes:
        typer.echo(f"Schedule: {outcome.schedule_id}  status={outcome.status}")
        typer.echo(f"Task: {outcome.task_id}")
        if outcome.run_id:
            typer.echo(f"Run: {outcome.run_id}")
        if outcome.error:
            typer.echo(f"Error: {outcome.error}")
            failed = True
        else:
            typer.echo(outcome.output)
    if failed:
        raise typer.Exit(1)


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
    stdin_input: bool = typer.Option(False, "--stdin", help="Read workflow input from stdin."),
    output_format: str = typer.Option("text", "--output", help="Output format: text or json."),
) -> None:
    """Run a workflow by id."""
    _run_workflow_by_id(workflow_id, home=home, input_text=input_text, stdin_input=stdin_input, output_format=output_format)


@app.command("run")
def run_workflow_alias(
    workflow_id: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
    input_text: str = typer.Option("", "--input", help="Workflow input text."),
    stdin_input: bool = typer.Option(False, "--stdin", help="Read workflow input from stdin."),
    output_format: str = typer.Option("text", "--output", help="Output format: text or json."),
) -> None:
    """Run a workflow by id."""
    _run_workflow_by_id(workflow_id, home=home, input_text=input_text, stdin_input=stdin_input, output_format=output_format)


def _run_workflow_by_id(
    workflow_id: str,
    *,
    home: Optional[Path],
    input_text: str,
    stdin_input: bool,
    output_format: str,
) -> None:
    if output_format not in {"text", "json"}:
        raise typer.BadParameter("--output must be text or json")
    resolved_home = home or Path.cwd()
    actual_input = typer.get_text_stream("stdin").read() if stdin_input else input_text
    registry = WorkflowRegistry(load_config(resolved_home))
    workflow = registry.get(workflow_id)
    try:
        result = WorkflowRunner(resolved_home).run(workflow, actual_input)
        if output_format == "json":
            typer.echo(
                json.dumps(
                    {"status": "completed", "run_id": result.run_id, "output": result.output},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return
        typer.echo(f"Run: {result.run_id}")
        typer.echo(result.output)
    except WorkflowRunFailed as exc:
        if output_format == "json":
            typer.echo(json.dumps({"status": "failed", "run_id": exc.run_id, "error": exc.message}, ensure_ascii=False, sort_keys=True))
            raise typer.Exit(1) from exc
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
    answer: Optional[str] = typer.Option(None, "--answer", "-a", help="Answer for a pending clarification request."),
    checkpoint_id: Optional[str] = typer.Option(None, "--checkpoint", help="Checkpoint id to resume from."),
    message: Optional[str] = typer.Option(None, "--message", "-m", help="Backward-compatible alias for --answer."),
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
) -> None:
    """Resume a run from a pending clarification or a checkpoint."""
    resolved_home = home or Path.cwd()
    actual_answer = answer if answer is not None else message
    try:
        result = WorkflowRunner(resolved_home).resume(
            run_id,
            answer=actual_answer,
            checkpoint_id=checkpoint_id,
        )
    except (ValueError, WorkflowRunFailed) as exc:
        typer.echo(f"Error: {exc}")
        raise typer.Exit(1) from exc
    typer.echo(f"Run: {result.run_id}")
    typer.echo("Status: completed")
    typer.echo(result.output)


@runs_app.command("replay")
def runs_replay(
    run_id: str,
    home: Optional[Path] = typer.Option(None, "--home", "-H", help="AgentEnd home directory."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Build a replay plan without creating a replay run."),
) -> None:
    """Replay one historical run by reusing safe historical outputs."""
    resolved_home = home or Path.cwd()
    init_database(resolved_home)
    try:
        with session_scope(resolved_home) as session:
            if dry_run:
                plan = build_replay_plan(resolved_home, session, run_id)
                plan["dry_run"] = True
                typer.echo(json.dumps(plan, ensure_ascii=False, sort_keys=True))
                return
            execution = execute_replay_plan(resolved_home, session, run_id)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Run: {execution.run_id}")
    if execution.status != "completed":
        typer.echo(f"Error: {execution.output}")
        raise typer.Exit(1)
    typer.echo(execution.output)


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
