import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentend.config import load_config
from agentend.core.clarifications import answer_clarification, create_clarification_request, pending_clarification_for_run
from agentend.core.context_runtime import create_checkpoint, estimate_tokens, record_context_ledger
from agentend.core.errors import record_error
from agentend.core.events import record_event
from agentend.core.llm_router import LLMRouter
from agentend.core.profile import load_agent_profile
from agentend.core.replanner import record_replan_suggestion, replan_failure
from agentend.core.tool_contracts import snapshot_tool_contracts, sync_tool_manifests
from agentend.core.tool_registry import ToolRegistry
from agentend.core.workflow_registry import WorkflowRegistry
from agentend.core.workflow_schema import WorkflowDefinition, WorkflowNode
from agentend.db.models import Checkpoint, ContextLedger, Conversation, CostBudget, Run, RunStep, WorkflowDef
from agentend.db.session import init_database, session_scope
from agentend.tools.base import ToolContext


@dataclass(frozen=True)
class WorkflowRunResult:
    run_id: str
    output: str


class WorkflowRunFailed(RuntimeError):
    def __init__(self, run_id: str, message: str):
        super().__init__(message)
        self.run_id = run_id
        self.message = message


class BudgetExceeded(RuntimeError):
    pass


class WorkflowRunner:
    def __init__(self, home: Path):
        self.home = home.expanduser().resolve()
        init_database(self.home)

    def run(
        self,
        workflow: WorkflowDefinition,
        user_input: str,
        channel: str = "cli",
        external_user_id: str = "workflow",
        run_mode: str = "normal",
    ) -> WorkflowRunResult:
        config = load_config(self.home)
        profile = load_agent_profile(config)
        llm = LLMRouter(config)
        tools = ToolRegistry(self.home)
        outputs: dict[str, str] = {}
        result: WorkflowRunResult | None = None
        failure: WorkflowRunFailed | None = None

        with session_scope(self.home) as session:
            conversation = Conversation(
                id=str(uuid4()),
                channel=channel,
                external_user_id=external_user_id,
                title=user_input[:80],
            )
            session.add(conversation)
            run = Run(
                id=str(uuid4()),
                conversation_id=conversation.id,
                workflow_id=workflow.id,
                status="running",
                input_json=json.dumps({"input": user_input}, ensure_ascii=False),
                result_json="{}",
                agent_profile_path=str(profile.path),
                agent_profile_hash=profile.digest,
                llm_provider=config.llm.provider,
                llm_model=config.llm.model,
            )
            session.add(run)
            self._persist_workflow_def(session, workflow)
            tool_contracts = tools.manifests()
            sync_tool_manifests(session, tool_contracts)
            snapshot_tool_contracts(session, run.id, tool_contracts)
            record_event(session, "run.created", {"workflow_id": workflow.id}, run_id=run.id)
            record_event(session, "workflow.loaded", {"workflow_id": workflow.id}, run_id=run.id)

            active_node_id = ""
            try:
                for node in self._ordered_nodes(workflow):
                    active_node_id = node.id
                    output = self._run_node(session, run.id, node, user_input, outputs, llm, tools, run_mode=run_mode, workflow_context=workflow)
                    outputs[node.id] = output
                    if node.type == "human_input":
                        run.status = "waiting_input"
                        run.result_json = json.dumps({"prompt": output}, ensure_ascii=False)
                        record_event(session, "run.state_changed", {"status": "waiting_input"}, run_id=run.id)
                        result = WorkflowRunResult(run_id=run.id, output=output)
                        break

                if result is None:
                    final_output = outputs[workflow.nodes[-1].id]
                    run.status = "completed"
                    run.result_json = json.dumps({"content": final_output}, ensure_ascii=False)
                    record_event(session, "run.completed", {"workflow_id": workflow.id}, run_id=run.id)
                    result = WorkflowRunResult(run_id=run.id, output=final_output)
            except Exception as exc:
                self._mark_run_failed(session, run, workflow, user_input, active_node_id, exc)
                failure = WorkflowRunFailed(run.id, str(exc))

        if failure is not None:
            raise failure
        assert result is not None
        return result

    def resume(
        self,
        run_id: str,
        *,
        answer: str | None = None,
        checkpoint_id: str | None = None,
        run_mode: str = "normal",
        expected_channel: str | None = None,
        expected_external_user_id: str | None = None,
    ) -> WorkflowRunResult:
        if answer is None and checkpoint_id is None:
            raise ValueError("Provide --answer or --checkpoint")
        config = load_config(self.home)
        llm = LLMRouter(config)
        tools = ToolRegistry(self.home)
        result: WorkflowRunResult | None = None
        failure: WorkflowRunFailed | None = None
        resume_error: ValueError | None = None

        with session_scope(self.home) as session:
            run = session.get(Run, run_id)
            if run is None:
                raise ValueError(f"Unknown run: {run_id}")
            conversation = session.get(Conversation, run.conversation_id)
            if expected_channel is not None and (
                conversation is None or conversation.channel != expected_channel
            ):
                raise ValueError("Run does not belong to this channel")
            if expected_external_user_id is not None and (
                conversation is None or conversation.external_user_id != expected_external_user_id
            ):
                raise ValueError("Run does not belong to this external user")
            if run.workflow_id is None:
                raise ValueError("Run has no workflow_id and cannot be resumed")
            workflow = WorkflowRegistry(config).get(run.workflow_id)
            user_input = str(json.loads(run.input_json).get("input", ""))
            outputs = self._completed_outputs_for_workflow(session, run_id, workflow)
            start_after_node: str | None = None

            if answer is not None:
                request = pending_clarification_for_run(session, run_id)
                if request is None:
                    raise ValueError(f"Run has no pending clarification: {run_id}")
                expires_at = request.expires_at
                if expires_at is not None and expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if expires_at is not None and expires_at <= datetime.now(timezone.utc):
                    request.status = "expired"
                    record_event(session, "clarification.expired", {"request_id": request.id}, run_id=run.id)
                    resume_error = ValueError("Clarification request expired")
                else:
                    answer_clarification(session, request, answer)
                    step = session.get(RunStep, request.step_id)
                    if step is None:
                        raise ValueError(f"Clarification step is missing: {request.step_id}")
                    step.status = "completed"
                    step.output_json = json.dumps({"content": answer}, ensure_ascii=False)
                    step.error = None
                    outputs[step.node_id] = answer
                    create_checkpoint(session, run_id=run_id, step=step, output=answer)
                    start_after_node = step.node_id
            elif checkpoint_id is not None:
                checkpoint = session.get(Checkpoint, checkpoint_id)
                if checkpoint is None:
                    raise ValueError(f"Unknown checkpoint: {checkpoint_id}")
                if checkpoint.run_id != run_id:
                    raise ValueError("Checkpoint does not belong to this run")
                start_after_node = checkpoint.node_id
                outputs = self._completed_outputs_for_workflow(session, run_id, workflow, stop_node_id=start_after_node)

            if resume_error is None:
                run.status = "running"
                run.error = None
                record_event(session, "run.resumed", {"checkpoint_id": checkpoint_id, "answered": answer is not None}, run_id=run.id)
                try:
                    result = self._continue_existing_run(
                        session=session,
                        run=run,
                        workflow=workflow,
                        user_input=user_input,
                        outputs=outputs,
                        start_after_node=start_after_node,
                        llm=llm,
                        tools=tools,
                        run_mode=run_mode,
                    )
                except Exception as exc:
                    self._mark_run_failed(session, run, workflow, user_input, start_after_node or "", exc)
                    failure = WorkflowRunFailed(run.id, str(exc))

        if resume_error is not None:
            raise resume_error
        if failure is not None:
            raise failure
        assert result is not None
        return result

    def _run_node(
        self,
        session: Session,
        run_id: str,
        node: WorkflowNode,
        user_input: str,
        outputs: dict[str, str],
        llm: LLMRouter,
        tools: ToolRegistry,
        step_node_id: str | None = None,
        run_mode: str = "normal",
        workflow_context: WorkflowDefinition | None = None,
    ) -> str:
        persisted_node_id = step_node_id or node.id
        step = RunStep(
            id=str(uuid4()),
            run_id=run_id,
            node_id=persisted_node_id,
            status="running",
            input_json=json.dumps({"input": user_input, "depends_on": node.depends_on}, ensure_ascii=False),
            output_json="{}",
        )
        session.add(step)
        record_event(session, "step.started", {"node_id": persisted_node_id, "type": node.type}, run_id=run_id)

        try:
            if node.type == "llm":
                prompt = (node.prompt or "{input}").format(input=user_input, **outputs)
                resolved_workflow_context = workflow_context or self._workflow_for_context(session, run_id)
                ledger = record_context_ledger(
                    session,
                    self.home,
                    run_id=run_id,
                    step_id=step.id,
                    workflow=resolved_workflow_context,
                    user_input=user_input,
                    prompt=prompt,
                    model_stage="workflow_step",
                    model_provider=llm.config.llm.provider,
                    model_model=llm.config.llm.model,
                    step_policy=node.context,
                )
                self._check_budget_after_context(session, run_id, ledger)
                output = llm.complete(prompt)
                self._check_output_budget(session, run_id, output)
            elif node.type == "tool":
                if node.tool is None:
                    raise ValueError(f"Tool node {node.id} is missing tool")
                rendered_input = self._render_data(node.input, user_input, outputs)
                result = tools.call(
                    node.tool,
                    rendered_input,
                    ToolContext(home=self.home, run_id=run_id, step_id=step.id, session=session, run_mode=run_mode),
                )
                output = result.content
            elif node.type == "workflow_call":
                if node.workflow is None:
                    raise ValueError(f"Workflow call node {node.id} is missing workflow")
                child = WorkflowRegistry(load_config(self.home)).get(node.workflow)
                output = self._run_inline_workflow(
                    session=session,
                    run_id=run_id,
                    workflow=child,
                    user_input=user_input,
                    llm=llm,
                    tools=tools,
                    prefix=f"{node.id}.",
                    run_mode=run_mode,
                )
            elif node.type == "condition":
                rendered_input = self._render_data(node.input, user_input, outputs)
                output = "true" if rendered_input.get("left") == rendered_input.get("equals") else "false"
            elif node.type == "parallel":
                output = json.dumps({dep: outputs.get(dep, "") for dep in node.depends_on}, ensure_ascii=False)
            elif node.type == "human_input":
                output = str(node.input.get("prompt", "Input required"))
                create_clarification_request(
                    session,
                    run_id=run_id,
                    step_id=step.id,
                    request_type=str(node.input.get("type", "missing_input")),
                    question=output,
                    reason=str(node.input.get("reason", "")),
                    choices=[str(item) for item in node.input.get("choices", [])],
                    free_text_allowed=bool(node.input.get("free_text_allowed", True)),
                )
                step.status = "waiting_input"
                step.output_json = json.dumps({"prompt": output}, ensure_ascii=False)
                record_event(session, "step.completed", {"node_id": persisted_node_id}, run_id=run_id)
                return output
            elif node.type == "final":
                output = outputs[node.depends_on[-1]] if node.depends_on else user_input
            else:
                raise ValueError(f"Unsupported node type for this runner slice: {node.type}")

            step.status = "completed"
            step.output_json = json.dumps({"content": output}, ensure_ascii=False)
            create_checkpoint(session, run_id=run_id, step=step, output=output)
            record_event(session, "step.completed", {"node_id": persisted_node_id}, run_id=run_id)
            return output
        except Exception as exc:
            step.status = "failed"
            step.error = str(exc)
            record_event(session, "step.failed", {"node_id": persisted_node_id, "error": str(exc)}, run_id=run_id)
            raise

    def _run_inline_workflow(
        self,
        session: Session,
        run_id: str,
        workflow: WorkflowDefinition,
        user_input: str,
        llm: LLMRouter,
        tools: ToolRegistry,
        prefix: str,
        run_mode: str = "normal",
    ) -> str:
        outputs: dict[str, str] = {}
        for node in self._ordered_nodes(workflow):
            outputs[node.id] = self._run_node(
                session,
                run_id,
                node,
                user_input,
                outputs,
                llm,
                tools,
                step_node_id=f"{prefix}{node.id}",
                run_mode=run_mode,
                workflow_context=workflow,
            )
        return outputs[workflow.nodes[-1].id]

    def _render_data(self, value: object, user_input: str, outputs: dict[str, str]) -> object:
        if isinstance(value, str):
            return value.format(input=user_input, **outputs)
        if isinstance(value, list):
            return [self._render_data(item, user_input, outputs) for item in value]
        if isinstance(value, dict):
            return {key: self._render_data(item, user_input, outputs) for key, item in value.items()}
        return value

    def _continue_existing_run(
        self,
        *,
        session: Session,
        run: Run,
        workflow: WorkflowDefinition,
        user_input: str,
        outputs: dict[str, str],
        start_after_node: str | None,
        llm: LLMRouter,
        tools: ToolRegistry,
        run_mode: str,
    ) -> WorkflowRunResult:
        skipping = start_after_node is not None
        active_node_id = ""
        for node in self._ordered_nodes(workflow):
            if skipping:
                if node.id == start_after_node:
                    skipping = False
                continue
            active_node_id = node.id
            output = self._run_node(session, run.id, node, user_input, outputs, llm, tools, run_mode=run_mode, workflow_context=workflow)
            outputs[node.id] = output
            if node.type == "human_input":
                run.status = "waiting_input"
                run.result_json = json.dumps({"prompt": output}, ensure_ascii=False)
                record_event(session, "run.state_changed", {"status": "waiting_input"}, run_id=run.id)
                return WorkflowRunResult(run_id=run.id, output=output)

        final_node_id = workflow.nodes[-1].id
        if final_node_id not in outputs:
            raise ValueError(f"Checkpoint cannot resume workflow after node: {start_after_node}")
        final_output = outputs[final_node_id]
        run.status = "completed"
        run.result_json = json.dumps({"content": final_output}, ensure_ascii=False)
        record_event(session, "run.completed", {"workflow_id": workflow.id, "resumed": True}, run_id=run.id)
        return WorkflowRunResult(run_id=run.id, output=final_output)

    def _completed_outputs_for_workflow(
        self,
        session: Session,
        run_id: str,
        workflow: WorkflowDefinition,
        stop_node_id: str | None = None,
    ) -> dict[str, str]:
        completed = self._completed_outputs(session, run_id)
        outputs: dict[str, str] = {}
        for node in self._ordered_nodes(workflow):
            if node.id in completed:
                outputs[node.id] = completed[node.id]
            if node.id == stop_node_id:
                break
        return outputs

    def _completed_outputs(self, session: Session, run_id: str) -> dict[str, str]:
        outputs: dict[str, str] = {}
        steps = session.execute(select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.created_at)).scalars().all()
        for step in steps:
            if step.status != "completed":
                continue
            try:
                payload = json.loads(step.output_json)
            except json.JSONDecodeError:
                continue
            if "content" in payload:
                outputs[step.node_id] = str(payload["content"])
            elif "prompt" in payload:
                outputs[step.node_id] = str(payload["prompt"])
        return outputs

    def _mark_run_failed(
        self,
        session: Session,
        run: Run,
        workflow: WorkflowDefinition,
        user_input: str,
        active_node_id: str,
        exc: Exception,
    ) -> None:
        failed_step = (
            session.execute(
                select(RunStep)
                .where(RunStep.run_id == run.id)
                .where(RunStep.status == "failed")
                .order_by(RunStep.created_at.desc())
            )
            .scalars()
            .first()
        )
        classified = record_error(
            session,
            exc,
            source="workflow",
            run_id=run.id,
            step_id=failed_step.id if failed_step else None,
        )
        suggestion = replan_failure(
            goal=user_input,
            current_workflow=workflow.id,
            failed_step=active_node_id or (failed_step.node_id if failed_step else ""),
            error=str(exc),
            error_code=classified.code,
        )
        record_replan_suggestion(
            session,
            run_id=run.id,
            step_id=failed_step.id if failed_step else None,
            failed_step=active_node_id or (failed_step.node_id if failed_step else ""),
            error_code=classified.code,
            error_message=str(exc),
            suggestion=suggestion,
        )
        run.status = "failed"
        run.error = str(exc)
        run.result_json = json.dumps({"error": str(exc), "replan_suggestion": suggestion}, ensure_ascii=False)
        record_event(session, "run.failed", {"error": str(exc)}, run_id=run.id)

    def _check_budget_after_context(self, session: Session, run_id: str, ledger: ContextLedger) -> None:
        budget = self._budget_for_run(session, run_id)
        if budget is None:
            return
        if budget.max_llm_calls is not None:
            call_count = len(session.execute(select(ContextLedger).where(ContextLedger.run_id == run_id)).scalars().all())
            if call_count > budget.max_llm_calls:
                raise BudgetExceeded(
                    f"budget_exceeded: max_llm_calls exceeded for {budget.workflow_id} ({call_count} > {budget.max_llm_calls})"
                )
        if budget.max_input_tokens is not None and ledger.estimated_input_tokens > budget.max_input_tokens:
            raise BudgetExceeded(
                "budget_exceeded: "
                f"max_input_tokens exceeded for {budget.workflow_id} ({ledger.estimated_input_tokens} > {budget.max_input_tokens})"
            )

    def _check_output_budget(self, session: Session, run_id: str, output: str) -> None:
        budget = self._budget_for_run(session, run_id)
        if budget is None or budget.max_output_tokens is None:
            return
        output_tokens = estimate_tokens(output)
        if output_tokens > budget.max_output_tokens:
            raise BudgetExceeded(
                f"budget_exceeded: max_output_tokens exceeded for {budget.workflow_id} ({output_tokens} > {budget.max_output_tokens})"
            )

    def _budget_for_run(self, session: Session, run_id: str) -> CostBudget | None:
        run = session.get(Run, run_id)
        if run is None or run.workflow_id is None:
            return None
        return session.get(CostBudget, run.workflow_id)

    def _ordered_nodes(self, workflow: WorkflowDefinition) -> list[WorkflowNode]:
        remaining = list(workflow.nodes)
        ordered: list[WorkflowNode] = []
        completed: set[str] = set()
        while remaining:
            ready = [node for node in remaining if all(dep in completed for dep in node.depends_on)]
            if not ready:
                raise ValueError("Workflow graph contains a cycle")
            for node in ready:
                ordered.append(node)
                completed.add(node.id)
                remaining.remove(node)
        return ordered

    def _persist_workflow_def(self, session: Session, workflow: WorkflowDefinition) -> None:
        existing = session.get(WorkflowDef, workflow.id)
        payload = workflow.model_dump_json(indent=2)
        if existing is None:
            session.add(
                WorkflowDef(
                    id=workflow.id,
                    name=workflow.name,
                    source_path="",
                    source_yaml=payload,
                )
            )
        else:
            existing.name = workflow.name
            existing.source_yaml = payload

    def _workflow_for_context(self, session: Session, run_id: str) -> WorkflowDefinition | None:
        run = session.get(Run, run_id)
        if run is None or run.workflow_id is None:
            return None
        try:
            return WorkflowRegistry(load_config(self.home)).get(run.workflow_id)
        except Exception:
            return None
