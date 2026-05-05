import json
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session

from agentend.config import load_config
from agentend.core.events import record_event
from agentend.core.llm_router import LLMRouter
from agentend.core.profile import load_agent_profile
from agentend.core.tool_registry import ToolRegistry
from agentend.core.workflow_registry import WorkflowRegistry
from agentend.core.workflow_schema import WorkflowDefinition, WorkflowNode
from agentend.db.models import Conversation, Run, RunStep, WorkflowDef
from agentend.db.session import init_database, session_scope
from agentend.tools.base import ToolContext


@dataclass(frozen=True)
class WorkflowRunResult:
    run_id: str
    output: str


class WorkflowRunner:
    def __init__(self, home: Path):
        self.home = home.expanduser().resolve()
        init_database(self.home)

    def run(self, workflow: WorkflowDefinition, user_input: str, channel: str = "cli") -> WorkflowRunResult:
        config = load_config(self.home)
        profile = load_agent_profile(config)
        llm = LLMRouter(config)
        tools = ToolRegistry()
        outputs: dict[str, str] = {}

        with session_scope(self.home) as session:
            conversation = Conversation(
                id=str(uuid4()),
                channel=channel,
                external_user_id="workflow",
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
            record_event(session, "run.created", {"workflow_id": workflow.id}, run_id=run.id)
            record_event(session, "workflow.loaded", {"workflow_id": workflow.id}, run_id=run.id)

            for node in self._ordered_nodes(workflow):
                output = self._run_node(session, run.id, node, user_input, outputs, llm, tools)
                outputs[node.id] = output

            final_output = outputs[workflow.nodes[-1].id]
            run.status = "completed"
            run.result_json = json.dumps({"content": final_output}, ensure_ascii=False)
            record_event(session, "run.completed", {"workflow_id": workflow.id}, run_id=run.id)
            return WorkflowRunResult(run_id=run.id, output=final_output)

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

        if node.type == "llm":
            prompt = (node.prompt or "{input}").format(input=user_input, **outputs)
            output = llm.complete(prompt)
        elif node.type == "tool":
            if node.tool is None:
                raise ValueError(f"Tool node {node.id} is missing tool")
            rendered_input = self._render_data(node.input, user_input, outputs)
            result = tools.call(
                node.tool,
                rendered_input,
                ToolContext(home=self.home, run_id=run_id, step_id=step.id, session=session),
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
            )
        elif node.type == "condition":
            rendered_input = self._render_data(node.input, user_input, outputs)
            output = "true" if rendered_input.get("left") == rendered_input.get("equals") else "false"
        elif node.type == "parallel":
            output = json.dumps({dep: outputs.get(dep, "") for dep in node.depends_on}, ensure_ascii=False)
        elif node.type == "human_input":
            output = str(node.input.get("prompt", "Input required"))
        elif node.type == "final":
            output = outputs[node.depends_on[-1]] if node.depends_on else user_input
        else:
            raise ValueError(f"Unsupported node type for this runner slice: {node.type}")

        step.status = "completed"
        step.output_json = json.dumps({"content": output}, ensure_ascii=False)
        record_event(session, "step.completed", {"node_id": persisted_node_id}, run_id=run_id)
        return output

    def _run_inline_workflow(
        self,
        session: Session,
        run_id: str,
        workflow: WorkflowDefinition,
        user_input: str,
        llm: LLMRouter,
        tools: ToolRegistry,
        prefix: str,
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
