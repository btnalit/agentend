from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentend.config import load_config
from agentend.core.evidence import evidence_manifest_for_run
from agentend.core.errors import classify_exception
from agentend.core.memory_store import write_memory_item
from agentend.core.skills import ensure_builtin_skills, load_skill_bundle
from agentend.core.tool_contracts import snapshot_to_dict
from agentend.core.tool_registry import ToolRegistry
from agentend.core.workflow_runner import WorkflowRunFailed
from agentend.core.workflow_runner import WorkflowRunner
from agentend.core.workflow_schema import load_workflow_yaml
from agentend.db.models import (
    ActionPolicyDecision,
    Artifact,
    Conversation,
    ContextLedger,
    ContextDroppedItem,
    ContextPackItem,
    ContextPolicy,
    ContextSummary,
    EvalRun,
    MemoryRetrieval,
    Run,
    RunExport,
    RunStep,
    Skill,
    SourceRecord,
    ToolCall,
    ToolContractSnapshot,
)
from agentend.db.session import session_scope
from agentend.tools.base import ToolContext


CONTEXT_SMOKE_INPUT = "agentend context smoke anchor"
CONTEXT_SMOKE_MEMORY = "agentend context smoke anchor project memory"
CONTEXT_SMOKE_WORKFLOW = "context_smoke_eval"
EVAL_SUITES = ("smoke", "context-smoke", "context-long", "tools-smoke", "skills-smoke")
PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c4944415408d7636060600000000400010d0a2db40000000049454e44ae426082"
)


@dataclass(frozen=True)
class EvalResult:
    id: str
    suite: str
    status: str
    result: dict[str, object]


def list_eval_suites() -> list[str]:
    return list(EVAL_SUITES)


def run_eval_suite(
    home: Path,
    session: Session,
    suite: str,
    *,
    skill: str | None = None,
    skill_path: Path | None = None,
) -> EvalResult:
    if suite == "smoke":
        payload = _run_smoke_suite(home)
    elif suite == "context-smoke":
        payload = _run_context_smoke_suite(home)
    elif suite == "context-long":
        payload = _run_context_long_suite(home)
    elif suite == "tools-smoke":
        payload = _run_tools_smoke_suite(home)
    elif suite == "skills-smoke":
        payload = _run_skills_smoke_suite(home, skill=skill, skill_path=skill_path)
    else:
        raise ValueError(f"Unknown eval suite: {suite}")

    row = EvalRun(
        id=str(uuid4()),
        suite=suite,
        status=str(payload["status"]),
        result_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )
    session.add(row)
    return EvalResult(id=row.id, suite=suite, status=row.status, result=payload)


def _run_smoke_suite(home: Path) -> dict[str, object]:
    config = load_config(home)
    tool_names = ToolRegistry(home).names()
    context_payload = _run_context_smoke_suite(home)
    checks = {
        "config_loaded": bool(config.llm.provider),
        "tools_registered": "file.read_text" in tool_names and "memory.write" in tool_names,
        "context_smoke_passed": context_payload["status"] == "passed",
    }
    status = "passed" if all(checks.values()) else "failed"
    return {
        "suite": "smoke",
        "status": status,
        "checks": checks,
        "nested_suites": {"context-smoke": context_payload},
    }


def _run_context_smoke_suite(home: Path) -> dict[str, object]:
    workflow = _prepare_context_smoke_fixture(home)
    run_id = ""
    workflow_error: str | None = None
    try:
        result = WorkflowRunner(home).run(workflow, CONTEXT_SMOKE_INPUT, channel="eval")
        run_id = result.run_id
    except Exception as exc:
        workflow_error = str(exc)

    cases = _inspect_context_smoke_cases(home, run_id, workflow_error)
    status = "passed" if all(case["status"] == "passed" for case in cases) else "failed"
    return {
        "suite": "context-smoke",
        "status": status,
        "checks": {"all_cases_passed": status == "passed", "case_count": len(cases)},
        "cases": cases,
    }


def _run_context_long_suite(home: Path) -> dict[str, object]:
    resolved_home = home.expanduser().resolve()
    _prepare_context_long_static_fixture(resolved_home)
    parent_run_id = ""
    skill_run_id = ""
    workflow_error = ""
    with _search_provider_fixture() as search_url:
        previous_key = os.environ.get("AGENTEND_EVAL_SEARCH_KEY")
        os.environ["AGENTEND_EVAL_SEARCH_KEY"] = "eval-key"
        try:
            _write_context_long_workflows(resolved_home, search_url)
            parent = load_workflow_yaml((resolved_home / "workflows" / "definitions" / "context_long_parent.yaml").read_text(encoding="utf-8"))
            parent_result = WorkflowRunner(resolved_home).run(
                parent,
                "context-long-anchor " * 40,
                channel="eval",
            )
            parent_run_id = parent_result.run_id
            skill_run_id = _run_context_long_skill_fixture(resolved_home)
        except Exception as exc:
            workflow_error = str(exc)
        finally:
            if previous_key is None:
                os.environ.pop("AGENTEND_EVAL_SEARCH_KEY", None)
            else:
                os.environ["AGENTEND_EVAL_SEARCH_KEY"] = previous_key

    cases = _inspect_context_long_cases(resolved_home, parent_run_id, skill_run_id, workflow_error)
    return _suite_payload("context-long", cases)


def _prepare_context_long_static_fixture(home: Path) -> None:
    with session_scope(home) as session:
        global_policy = session.get(ContextPolicy, "eval.context_long.global")
        if global_policy is None:
            global_policy = ContextPolicy(id="eval.context_long.global", scope="global", target="default")
            session.add(global_policy)
        global_policy.policy_json = json.dumps(
            {"redact_secrets": True, "max_items": 8, "trusted_memory_sources": ["manual"], "min_memory_confidence": 0.5},
            ensure_ascii=False,
            sort_keys=True,
        )
        project_policy = session.get(ContextPolicy, "eval.context_long.project")
        if project_policy is None:
            project_policy = ContextPolicy(id="eval.context_long.project", scope="project", target="default")
            session.add(project_policy)
        project_policy.policy_json = json.dumps(
            {"max_items": 5, "memory_scopes": ["project", "task"], "retrieve_top_k": 12},
            ensure_ascii=False,
            sort_keys=True,
        )
        skill_policy = session.get(ContextPolicy, "eval.context_long.skill")
        if skill_policy is None:
            skill_policy = ContextPolicy(id="eval.context_long.skill", scope="skill", target="file.workspace_ops")
            session.add(skill_policy)
        skill_policy.policy_json = json.dumps({"redact_secrets": False, "max_items": 3}, ensure_ascii=False, sort_keys=True)
        write_memory_item(
            session,
            home,
            content="context-long-anchor trusted project memory",
            scope="project",
            source="manual",
            confidence="1.0",
            tags=["eval", "context-long"],
        )
        write_memory_item(
            session,
            home,
            content="context-long-anchor low confidence memory",
            scope="project",
            source="manual",
            confidence="0.1",
            tags=["eval", "context-long"],
        )
        write_memory_item(
            session,
            home,
            content="context-long-anchor expired memory",
            scope="project",
            source="manual",
            confidence="1.0",
            ttl="2000-01-01T00:00:00+00:00",
            tags=["eval", "context-long"],
        )
        write_memory_item(
            session,
            home,
            content="context-long-anchor untrusted web memory",
            scope="task",
            source="web",
            confidence="1.0",
            tags=["eval", "context-long"],
        )


def _write_context_long_workflows(home: Path, search_url: str) -> None:
    workflow_dir = home / "workflows" / "definitions"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / "context_long_child.yaml").write_text(
        """id: context_long_child
name: Context Long Child
nodes:
  - id: child_answer
    type: llm
    prompt: "Child keeps context: {input}"
  - id: final
    type: final
    depends_on: [child_answer]
""",
        encoding="utf-8",
    )
    (workflow_dir / "context_long_parent.yaml").write_text(
        f"""id: context_long_parent
name: Context Long Parent
context:
  include_memory: true
  retrieve_top_k: 12
nodes:
  - id: search
    type: tool
    tool: web.search
    input:
      query: "context-long-anchor"
      provider: brave
      base_url: "{search_url}"
      api_key_env: AGENTEND_EVAL_SEARCH_KEY
      limit: 1
  - id: child
    type: workflow_call
    workflow: context_long_child
    depends_on: [search]
  - id: answer
    type: llm
    depends_on: [child]
    prompt: "Parent keeps context: {{input}}"
  - id: final
    type: final
    depends_on: [answer]
""",
        encoding="utf-8",
    )


def _run_context_long_skill_fixture(home: Path) -> str:
    with session_scope(home) as session:
        row = next(skill for skill in ensure_builtin_skills(home, session) if skill.id == "file.workspace_ops")
        workflow_path = Path(row.workflow_path)
    workflow = load_workflow_yaml(workflow_path.read_text(encoding="utf-8"))
    result = WorkflowRunner(home).run(workflow, json.dumps({"task": "context-long skill policy"}, sort_keys=True), channel="eval-skill")
    return result.run_id


def _inspect_context_long_cases(home: Path, parent_run_id: str, skill_run_id: str, workflow_error: str) -> list[dict[str, object]]:
    if workflow_error:
        return [_failed_case(case_id, parent_run_id, workflow_error) for case_id in _context_long_case_ids()]

    with session_scope(home) as session:
        parent_ledgers = (
            session.execute(select(ContextLedger).where(ContextLedger.run_id == parent_run_id).order_by(ContextLedger.created_at))
            .scalars()
            .all()
        )
        parent_items = _context_items_for_ledgers(session, parent_ledgers)
        parent_dropped = _context_dropped_for_ledgers(session, parent_ledgers)
        parent_sources = session.execute(select(SourceRecord).where(SourceRecord.used_by_run_id == parent_run_id)).scalars().all()
        skill_ledger = _latest_context_ledger(session, skill_run_id)
        skill_items = []
        if skill_ledger is not None:
            skill_items = (
                session.execute(select(ContextPackItem).where(ContextPackItem.ledger_id == skill_ledger.id).order_by(ContextPackItem.created_at))
                .scalars()
                .all()
            )
        skill_policy_item = next((item for item in skill_items if item.item_type == "context_policy"), None)
        skill_policy = _json_or_empty(skill_policy_item.summary if skill_policy_item else "")
        dropped_reasons = {row.reason for row in parent_dropped}
        common = {
            "run_id": parent_run_id,
            "context_ledger_id": parent_ledgers[-1].id if parent_ledgers else "",
            "tool_call_id": "",
            "policy_decision_id": "",
            "artifact_id": "",
        }

        return [
            _case(
                "long-input-retained",
                common,
                [
                    _assertion("parent run records context", bool(parent_ledgers)),
                    _assertion(
                        "long input task remains selected",
                        any(item.item_type == "task" and "context-long-anchor" in item.summary for item in parent_items),
                    ),
                ],
            ),
            _case(
                "multi-workflow-ledgers",
                common,
                [
                    _assertion("parent and child llm steps both record ledgers", len(parent_ledgers) >= 2),
                ],
            ),
            _case(
                "real-search-provider",
                common,
                [
                    _assertion(
                        "brave-compatible search fixture records source evidence",
                        any(source.source_type == "web_search" and source.title == "Context Long Result" for source in parent_sources),
                    ),
                ],
            ),
            _case(
                "skill-policy-merge",
                common | {"skill_run_id": skill_run_id, "skill_context_ledger_id": skill_ledger.id if skill_ledger else ""},
                [
                    _assertion("skill context ledger exists", skill_ledger is not None),
                    _assertion("skill max_items tightens context", skill_policy.get("max_items") == 3 and len(skill_items) <= 3),
                    _assertion("global redaction remains enabled", skill_policy.get("redact_secrets") is True),
                ],
            ),
            _case(
                "memory-guard-dropped-reasons",
                common,
                [
                    _assertion("low confidence memory is dropped", "memory_low_confidence" in dropped_reasons),
                    _assertion("expired memory is dropped", "memory_expired" in dropped_reasons),
                    _assertion("untrusted memory is dropped", "memory_untrusted_source" in dropped_reasons),
                    _assertion("budget records max item drops", "max_items_exceeded" in dropped_reasons),
                ],
            ),
        ]


def _context_items_for_ledgers(session: Session, ledgers: list[ContextLedger]) -> list[ContextPackItem]:
    if not ledgers:
        return []
    ledger_ids = [ledger.id for ledger in ledgers]
    return session.execute(select(ContextPackItem).where(ContextPackItem.ledger_id.in_(ledger_ids))).scalars().all()


def _context_dropped_for_ledgers(session: Session, ledgers: list[ContextLedger]) -> list[ContextDroppedItem]:
    if not ledgers:
        return []
    ledger_ids = [ledger.id for ledger in ledgers]
    return session.execute(select(ContextDroppedItem).where(ContextDroppedItem.ledger_id.in_(ledger_ids))).scalars().all()


def _context_long_case_ids() -> list[str]:
    return [
        "long-input-retained",
        "multi-workflow-ledgers",
        "real-search-provider",
        "skill-policy-merge",
        "memory-guard-dropped-reasons",
    ]


@dataclass(frozen=True)
class ToolEvalCase:
    id: str
    tool_name: str
    input_data: dict[str, Any]
    assertion_name: str
    assertion: Callable[[dict[str, Any]], bool]


def _run_tools_smoke_suite(home: Path) -> dict[str, object]:
    resolved_home = home.expanduser().resolve()
    _prepare_tools_smoke_files(resolved_home)
    with _browser_fixture() as browser_url:
        cases = [
            ToolEvalCase(
                id="shell.run",
                tool_name="shell.run",
                input_data={"command": f'"{sys.executable}" -c "print(\'eval-shell\')"', "timeout_seconds": 10},
                assertion_name="shell command exits successfully",
                assertion=lambda data: data.get("exit_code") == 0 and "eval-shell" in str(data.get("stdout", "")),
            ),
            ToolEvalCase(
                id="python.exec",
                tool_name="python.exec",
                input_data={"code": "from pathlib import Path\nPath('artifact.txt').write_text('eval')\nprint('eval-python')"},
                assertion_name="python subprocess produces output and artifact",
                assertion=lambda data: data.get("exit_code") == 0
                and "eval-python" in str(data.get("stdout", ""))
                and bool(data.get("artifacts")),
            ),
            ToolEvalCase(
                id="browser.extract",
                tool_name="browser.extract",
                input_data={"url": browser_url},
                assertion_name="browser extracts local fixture text",
                assertion=lambda data: data.get("title") == "AgentEnd Eval" and "browser smoke" in str(data.get("text", "")),
            ),
            ToolEvalCase(
                id="db.write_rows",
                tool_name="db.write_rows",
                input_data={
                    "database": "data/eval/tools-smoke.sqlite",
                    "table": "items",
                    "rows": [{"name": "alpha"}],
                },
                assertion_name="database row is inserted",
                assertion=lambda data: data.get("inserted") == 1 and data.get("table") == "items",
            ),
            ToolEvalCase(
                id="im.telegram.send_message",
                tool_name="im.telegram.send_message",
                input_data={"chat_id": "eval-chat", "text": "eval dry run", "dry_run": True},
                assertion_name="telegram dry-run avoids external write",
                assertion=lambda data: data.get("dry_run") is True and data.get("text") == "eval dry run",
            ),
            ToolEvalCase(
                id="vision.describe",
                tool_name="vision.describe",
                input_data={"path": "data/eval/vision.png"},
                assertion_name="vision provider returns image metadata",
                assertion=lambda data: data.get("mime") == "image/png" and int(data.get("size_bytes", 0)) > 0,
            ),
            ToolEvalCase(
                id="tools.generate",
                tool_name="tools.generate",
                input_data={"goal": "create eval helper tool", "name": "eval_helper"},
                assertion_name="tool generator writes a draft package",
                assertion=lambda data: data.get("status") == "draft" and Path(str(data.get("draft_path", ""))).exists(),
            ),
        ]
        results = [_run_tool_eval_case(resolved_home, case) for case in cases]
    return _suite_payload("tools-smoke", results)


def _run_skills_smoke_suite(home: Path, *, skill: str | None = None, skill_path: Path | None = None) -> dict[str, object]:
    resolved_home = home.expanduser().resolve()
    if skill and skill_path:
        raise ValueError("Use either --skill or --skill-path, not both")
    if skill_path is not None:
        bundle = load_skill_bundle(skill_path.expanduser().resolve())
        cases = [
            _run_skill_eval_case(
                resolved_home,
                skill_id=bundle.id,
                workflow_path=bundle.workflow_path,
                input_payload=_skill_eval_input(bundle.source_location and Path(bundle.source_location), bundle.id),
            )
        ]
        return _suite_payload("skills-smoke", cases)

    with session_scope(resolved_home) as session:
        rows = ensure_builtin_skills(resolved_home, session)
        selected = [row for row in rows if row.enabled == "true" and (skill is None or row.id == skill)]
        skill_cases = [(row.id, Path(row.workflow_path), _default_skill_input(row.id)) for row in selected]
    if skill is not None and not skill_cases:
        raise ValueError(f"Unknown or disabled skill: {skill}")
    cases = [
        _run_skill_eval_case(resolved_home, skill_id=skill_id, workflow_path=workflow_path, input_payload=input_payload)
        for skill_id, workflow_path, input_payload in skill_cases
    ]
    return _suite_payload("skills-smoke", cases)


def _prepare_tools_smoke_files(home: Path) -> None:
    eval_dir = home / "data" / "eval"
    eval_dir.mkdir(parents=True, exist_ok=True)
    (eval_dir / "vision.png").write_bytes(PNG_1X1)
    with sqlite3.connect(eval_dir / "tools-smoke.sqlite") as connection:
        connection.execute("create table if not exists items (name text)")
        connection.commit()


def _run_tool_eval_case(home: Path, case: ToolEvalCase) -> dict[str, object]:
    registry = ToolRegistry(home)
    with session_scope(home) as session:
        conversation = Conversation(id=str(uuid4()), channel="eval", external_user_id="tools-smoke", title=case.id)
        run = Run(
            id=str(uuid4()),
            conversation_id=conversation.id,
            workflow_id=f"eval.tools.{case.id}",
            status="running",
            input_json=json.dumps({"tool": case.tool_name, "input": case.input_data}, ensure_ascii=False, sort_keys=True),
            result_json="{}",
        )
        session.add(conversation)
        session.add(run)
        result_data: dict[str, Any] = {}
        error = ""
        try:
            result = registry.call(case.tool_name, case.input_data, ToolContext(home, run.id, None, session))
            result_data = result.data
            run.status = "completed"
            run.result_json = json.dumps(result.data | {"content": result.content}, ensure_ascii=False, sort_keys=True)
        except Exception as exc:
            classified = classify_exception(exc)
            error = f"{classified.code}: {classified.message}"
            run.status = "failed"
            run.error = classified.message
            run.result_json = json.dumps({"error_code": classified.code, "error": classified.message}, ensure_ascii=False, sort_keys=True)

        session.flush()
        tool_call = _latest_tool_call(session, run.id, case.tool_name)
        decision = _latest_policy_decision(session, run.id, case.tool_name)
        artifact = _latest_artifact(session, run.id)
        export_path = ""
        assertions = [_assertion("tool run completes", run.status == "completed", error)]
        if run.status == "completed":
            try:
                user_visible_passed = case.assertion(result_data)
            except Exception as exc:
                user_visible_passed = False
                error = str(exc)
            assertions.extend(
                [
                    _assertion(case.assertion_name, user_visible_passed, error),
                    _assertion("tool call is audited", tool_call is not None),
                    _assertion("action policy decision is audited", decision is not None),
                ]
            )
        else:
            export_path = _export_run(home, session, run.id, "tools-smoke", case.id)
            assertions.append(_assertion("failed run is exported", bool(export_path) and (Path(export_path) / "run.json").exists(), export_path))

        return _case(
            case.id,
            {
                "run_id": run.id,
                "tool_call_id": tool_call.id if tool_call else "",
                "policy_decision_id": decision.id if decision else "",
                "artifact_id": artifact.id if artifact else "",
                "export_path": export_path,
                "result_preview": _preview_json(result_data),
                "error": error,
            },
            assertions,
        )


def _run_skill_eval_case(home: Path, *, skill_id: str, workflow_path: Path, input_payload: dict[str, Any]) -> dict[str, object]:
    workflow = load_workflow_yaml(workflow_path.read_text(encoding="utf-8"))
    run_id = ""
    output = ""
    error = ""
    try:
        result = WorkflowRunner(home).run(workflow, json.dumps(input_payload, ensure_ascii=False, sort_keys=True), channel="eval-skill")
        run_id = result.run_id
        output = result.output
    except WorkflowRunFailed as exc:
        run_id = exc.run_id
        error = exc.message
    except Exception as exc:
        error = str(exc)

    export_path = ""
    with session_scope(home) as session:
        run = session.get(Run, run_id) if run_id else None
        ledger = _latest_context_ledger(session, run_id) if run_id else None
        if error and run_id:
            export_path = _export_run(home, session, run_id, "skills-smoke", skill_id)
        assertions = [
            _assertion("skill workflow completes", run is not None and run.status == "completed", error),
            _assertion("skill output is non-empty", bool(output.strip()), error),
            _assertion("workflow run is audited", run is not None),
        ]
        if ledger is not None:
            assertions.append(_assertion("context ledger is audited", True))
        return _case(
            skill_id,
            {
                "run_id": run_id,
                "context_ledger_id": ledger.id if ledger else "",
                "tool_call_id": "",
                "policy_decision_id": "",
                "artifact_id": "",
                "export_path": export_path,
                "result_preview": output[:300],
                "error": error,
            },
            assertions,
        )


def _skill_eval_input(skill_dir: Path | None, skill_id: str) -> dict[str, Any]:
    if skill_dir is None:
        return _default_skill_input(skill_id)
    eval_path = skill_dir / "evals" / "smoke.json"
    if not eval_path.exists():
        return _default_skill_input(skill_id)
    try:
        payload = json.loads(eval_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return _default_skill_input(skill_id)
    input_value = payload.get("input")
    if isinstance(input_value, dict):
        return input_value
    if isinstance(input_value, str):
        return {"task": input_value}
    return _default_skill_input(skill_id)


def _default_skill_input(skill_id: str) -> dict[str, Any]:
    return {"task": f"Run smoke eval for {skill_id}"}


def _suite_payload(suite: str, cases: list[dict[str, object]]) -> dict[str, object]:
    failed_count = sum(1 for case in cases if case["status"] != "passed")
    status = "passed" if failed_count == 0 else "failed"
    return {
        "suite": suite,
        "status": status,
        "human_summary": _human_summary(suite, cases),
        "checks": {
            "all_cases_passed": status == "passed",
            "case_count": len(cases),
            "failed_case_count": failed_count,
        },
        "cases": cases,
    }


def _human_summary(suite: str, cases: list[dict[str, object]]) -> str:
    passed = sum(1 for case in cases if case["status"] == "passed")
    failed = len(cases) - passed
    return f"{suite}: {passed} passed, {failed} failed"


def _latest_tool_call(session: Session, run_id: str, tool_name: str) -> ToolCall | None:
    return (
        session.execute(
            select(ToolCall)
            .where(ToolCall.run_id == run_id)
            .where(ToolCall.tool_name == tool_name)
            .order_by(ToolCall.created_at.desc())
        )
        .scalars()
        .first()
    )


def _latest_policy_decision(session: Session, run_id: str, tool_name: str) -> ActionPolicyDecision | None:
    return (
        session.execute(
            select(ActionPolicyDecision)
            .where(ActionPolicyDecision.run_id == run_id)
            .where(ActionPolicyDecision.tool_name == tool_name)
            .order_by(ActionPolicyDecision.created_at.desc())
        )
        .scalars()
        .first()
    )


def _latest_artifact(session: Session, run_id: str) -> Artifact | None:
    return session.execute(select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.created_at.desc())).scalars().first()


def _latest_context_ledger(session: Session, run_id: str) -> ContextLedger | None:
    return session.execute(select(ContextLedger).where(ContextLedger.run_id == run_id).order_by(ContextLedger.created_at.desc())).scalars().first()


def _export_run(home: Path, session: Session, run_id: str, suite: str, case_id: str) -> str:
    session.flush()
    run = session.get(Run, run_id)
    if run is None:
        return ""
    export_root = home / "data" / "eval_exports" / suite / _safe_path_part(case_id) / run_id
    export_root.mkdir(parents=True, exist_ok=True)
    steps = session.execute(select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.created_at)).scalars().all()
    tool_calls = session.execute(select(ToolCall).where(ToolCall.run_id == run_id).order_by(ToolCall.created_at)).scalars().all()
    artifacts = session.execute(select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.created_at)).scalars().all()
    decisions = (
        session.execute(select(ActionPolicyDecision).where(ActionPolicyDecision.run_id == run_id).order_by(ActionPolicyDecision.created_at))
        .scalars()
        .all()
    )
    contract_snapshots = (
        session.execute(select(ToolContractSnapshot).where(ToolContractSnapshot.run_id == run_id).order_by(ToolContractSnapshot.tool_name))
        .scalars()
        .all()
    )
    contract_payload = [snapshot_to_dict(snapshot) for snapshot in contract_snapshots]
    evidence_manifest = evidence_manifest_for_run(session, home, run_id)
    payload = {
        "run": {
            "id": run.id,
            "status": run.status,
            "workflow_id": run.workflow_id,
            "error": run.error,
            "input": _json_or_empty(run.input_json),
            "result": _json_or_empty(run.result_json),
        },
        "steps": [{"id": step.id, "node_id": step.node_id, "status": step.status, "error": step.error} for step in steps],
        "tool_calls": [{"id": call.id, "tool_name": call.tool_name, "status": call.status, "error": call.error} for call in tool_calls],
        "action_policy_decisions": [
            {
                "id": decision.id,
                "tool_name": decision.tool_name,
                "decision": decision.decision,
                "side_effect": decision.side_effect,
                "reason": decision.reason,
            }
            for decision in decisions
        ],
        "tool_contract_snapshots": contract_payload,
        "artifacts": [{"id": artifact.id, "path": artifact.path, "kind": artifact.kind} for artifact in artifacts],
        "evidence_manifest": evidence_manifest,
    }
    (export_root / "run.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (export_root / "tool_contracts.json").write_text(json.dumps(contract_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (export_root / "evidence_manifest.json").write_text(json.dumps(evidence_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    session.add(RunExport(id=str(uuid4()), run_id=run_id, output_path=str(export_root), metadata_json=json.dumps(payload, ensure_ascii=False)))
    return str(export_root)


def _safe_path_part(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return safe or "case"


def _preview_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)[:500]


@contextmanager
def _browser_fixture():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = b"<html><head><title>AgentEnd Eval</title></head><body>browser smoke <a href='/next'>next</a></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            _ = format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@contextmanager
def _search_provider_fixture():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = json.dumps(
                {
                    "web": {
                        "results": [
                            {
                                "title": "Context Long Result",
                                "url": "https://example.com/context-long",
                                "description": "context-long-anchor search evidence",
                            }
                        ]
                    }
                },
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            _ = format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _prepare_context_smoke_fixture(home: Path):
    config = load_config(home)
    workflow_dir = config.resolve_home_path(config.data.workflow_dir)
    workflow_dir.mkdir(parents=True, exist_ok=True)
    big_content = "context-smoke-large-output " + ("x" * 2400)
    workflow_yaml = f"""id: {CONTEXT_SMOKE_WORKFLOW}
name: Context Smoke Eval
context:
  include_memory: true
  redact_secrets: false
  max_items: 8
  memory_scopes: [project, task]
  retrieve_top_k: 5
nodes:
  - id: write_big
    type: tool
    tool: fs.write_text
    input:
      path: data/eval/context-smoke-large.txt
      content: "{big_content}"
  - id: answer
    type: llm
    depends_on: [write_big]
    prompt: "Keep current task in view: {{input}}"
    context:
      redact_secrets: false
      max_items: 6
  - id: final
    type: final
    depends_on: [answer]
"""
    workflow_path = workflow_dir / f"{CONTEXT_SMOKE_WORKFLOW}.yaml"
    workflow_path.write_text(workflow_yaml, encoding="utf-8")
    with session_scope(home) as setup_session:
        policy = setup_session.get(ContextPolicy, "eval.context_smoke.global")
        if policy is None:
            policy = ContextPolicy(id="eval.context_smoke.global", scope="global", target="default")
            setup_session.add(policy)
        policy.policy_json = json.dumps({"redact_secrets": True, "max_items": 9}, ensure_ascii=False, sort_keys=True)
        write_memory_item(
            setup_session,
            home,
            content=CONTEXT_SMOKE_MEMORY,
            scope="project",
            source="manual",
            tags=["eval", "context-smoke"],
        )
    return load_workflow_yaml(workflow_yaml)


def _inspect_context_smoke_cases(home: Path, run_id: str, workflow_error: str | None) -> list[dict[str, object]]:
    if workflow_error is not None:
        return [_failed_case(case_id, run_id, workflow_error) for case_id in _context_case_ids()]

    with session_scope(home) as session:
        ledger = (
            session.execute(select(ContextLedger).where(ContextLedger.run_id == run_id).order_by(ContextLedger.created_at.desc()))
            .scalars()
            .first()
        )
        items = []
        if ledger is not None:
            items = (
                session.execute(select(ContextPackItem).where(ContextPackItem.ledger_id == ledger.id).order_by(ContextPackItem.created_at))
                .scalars()
                .all()
            )
        summaries = session.execute(select(ContextSummary).where(ContextSummary.run_id == run_id)).scalars().all()
        tool_calls = session.execute(select(ToolCall).where(ToolCall.run_id == run_id)).scalars().all()
        decisions = session.execute(select(ActionPolicyDecision).where(ActionPolicyDecision.run_id == run_id)).scalars().all()
        artifacts = session.execute(select(Artifact).where(Artifact.run_id == run_id)).scalars().all()
        retrieval = session.execute(select(MemoryRetrieval).order_by(MemoryRetrieval.created_at.desc())).scalars().first()

        policy_item = next((item for item in items if item.item_type == "context_policy"), None)
        merged_policy = _json_or_empty(policy_item.summary if policy_item else "")
        common = {
            "run_id": run_id,
            "context_ledger_id": ledger.id if ledger else "",
            "tool_call_id": tool_calls[0].id if tool_calls else "",
            "policy_decision_id": decisions[0].id if decisions else "",
            "artifact_id": artifacts[0].id if artifacts else "",
        }

        lost_context = _case(
            "lost-context",
            common,
            [
                _assertion("context ledger exists", ledger is not None),
                _assertion(
                    "current task is present",
                    any(item.item_type == "task" and CONTEXT_SMOKE_INPUT in item.summary for item in items),
                ),
            ],
        )
        tool_output_bloat = _case(
            "tool-output-bloat",
            common,
            [
                _assertion("tool result is compacted", any(summary.source_id == "fs.write_text" for summary in summaries)),
                _assertion("context pack excludes large tool output", all(len(item.summary) < 1200 for item in items)),
                _assertion("artifact records full tool output path", bool(artifacts)),
            ],
        )
        memory_retrieval = _case(
            "memory-retrieval",
            common | {"memory_retrieval_id": retrieval.id if retrieval else ""},
            [
                _assertion(
                    "project memory enters context",
                    any(item.item_type == "memory" and CONTEXT_SMOKE_MEMORY in item.summary for item in items),
                ),
                _assertion("memory retrieval is audited", retrieval is not None),
            ],
        )
        policy_merge = _case(
            "policy-merge",
            common,
            [
                _assertion("global redaction remains enabled", merged_policy.get("redact_secrets") is True),
                _assertion("step max_items shrinks context pack", merged_policy.get("max_items") == 6 and len(items) <= 6),
            ],
        )
        return [lost_context, tool_output_bloat, memory_retrieval, policy_merge]


def _context_case_ids() -> list[str]:
    return ["lost-context", "tool-output-bloat", "memory-retrieval", "policy-merge"]


def _assertion(name: str, passed: bool, details: str = "") -> dict[str, str]:
    payload = {"name": name, "status": "passed" if passed else "failed"}
    if details:
        payload["details"] = details
    return payload


def _case(case_id: str, metadata: dict[str, object], assertions: list[dict[str, str]]) -> dict[str, object]:
    status = "passed" if all(assertion["status"] == "passed" for assertion in assertions) else "failed"
    return {"id": case_id, "status": status, **metadata, "assertions": assertions}


def _failed_case(case_id: str, run_id: str, error: str) -> dict[str, object]:
    return _case(
        case_id,
        {"run_id": run_id, "context_ledger_id": "", "tool_call_id": "", "policy_decision_id": "", "artifact_id": ""},
        [_assertion("workflow run completes", False, error)],
    )


def _json_or_empty(value: str) -> dict[str, object]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}
