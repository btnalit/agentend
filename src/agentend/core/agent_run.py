from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from agentend.config import load_config
from agentend.core.agent_selector import SelectedAction, select_next_action_with_trace
from agentend.core.effectiveness import record_effectiveness_event
from agentend.core.goal_analyzer import analyze_goal
from agentend.core.memory_consolidator import consolidate_memory_candidates
from agentend.core.memory_store import search_memory_items
from agentend.core.skills import ensure_builtin_skills
from agentend.core.tool_registry import ToolRegistry
from agentend.core.workflow_registry import WorkflowRegistry
from agentend.core.workflow_runner import WorkflowRunFailed, WorkflowRunner
from agentend.core.workflow_schema import load_workflow_yaml
from agentend.db.models import (
    AgentIteration,
    AgentRun,
    Artifact,
    Checkpoint,
    Conversation,
    Run,
    RunStep,
    Skill,
    ToolCall,
    utc_now,
)
from agentend.db.session import init_database, session_scope
from agentend.tools.base import ToolContext


@dataclass(frozen=True)
class AgentRunRequest:
    goal: str
    channel: str = "cli"
    external_user_id: str = "local"
    max_iterations: int = 3
    run_mode: str = "normal"
    conversation_id: str | None = None


@dataclass(frozen=True)
class AgentRunResult:
    agent_run_id: str
    status: str
    stop_reason: str
    content: str
    linked_run_id: str | None = None
    progress_artifact_id: str | None = None


class AgentRunController:
    def __init__(self, home: Path) -> None:
        self.home = home.expanduser().resolve()
        init_database(self.home)

    def run(
        self,
        goal: str,
        *,
        channel: str = "cli",
        external_user_id: str = "local",
        max_iterations: int = 3,
        run_mode: str = "normal",
        conversation_id: str | None = None,
    ) -> AgentRunResult:
        request = AgentRunRequest(
            goal=goal.strip(),
            channel=channel,
            external_user_id=external_user_id,
            max_iterations=max(1, int(max_iterations)),
            run_mode=run_mode,
            conversation_id=conversation_id,
        )
        if not request.goal:
            raise ValueError("Agent goal must not be empty")
        agent_run_id = self._start_run(request)
        previous_observations: list[dict[str, Any]] = []
        final: AgentRunResult | None = None

        for iteration_index in range(1, request.max_iterations + 1):
            with session_scope(self.home) as session:
                agent_run = session.get(AgentRun, agent_run_id)
                assert agent_run is not None
                goal_analysis = analyze_goal(self.home, session, request.goal)
                memories = search_memory_items(session, request.goal, limit=5)
                selection = select_next_action_with_trace(
                    self.home,
                    session,
                    request.goal,
                    goal_analysis,
                    previous_observations,
                )
                selected = selection.selected
                plan = {
                    "goal_analysis": goal_analysis,
                    "memory_ids": [memory.id for memory in memories],
                    "memory_summaries": [memory.content[:240] for memory in memories],
                    "previous_observations": previous_observations,
                    "selector_trace": selection.trace,
                }
                iteration = AgentIteration(
                    id=str(uuid4()),
                    agent_run_id=agent_run_id,
                    iteration_index=iteration_index,
                    status="running",
                    plan_json=json.dumps(plan, ensure_ascii=False, sort_keys=True),
                    selected_action_json=selected.to_json(),
                    started_at=utc_now(),
                )
                session.add(iteration)
                agent_run.heartbeat_at = utc_now()
                iteration_id = iteration.id

            started = perf_counter()
            observation = self._execute_action(
                selected,
                request,
                agent_run_id=agent_run_id,
                iteration_id=iteration_id,
            )
            duration_ms = int((perf_counter() - started) * 1000)
            evaluation = self._evaluate_observation(observation, iteration_index, request.max_iterations)
            previous_observations.append(observation | {"action_name": selected.name})

            with session_scope(self.home) as session:
                iteration = session.get(AgentIteration, iteration_id)
                agent_run = session.get(AgentRun, agent_run_id)
                assert iteration is not None and agent_run is not None
                iteration.linked_run_id = observation.get("run_id")
                iteration.linked_tool_call_id = observation.get("tool_call_id")
                iteration.checkpoint_id = _latest_checkpoint_id(session, observation.get("run_id"))
                iteration.observation_json = json.dumps(observation, ensure_ascii=False, sort_keys=True)
                iteration.evaluation_json = json.dumps(evaluation, ensure_ascii=False, sort_keys=True)
                iteration.status = "completed" if observation.get("status") == "completed" else "failed"
                iteration.error = observation.get("error")
                iteration.completed_at = utc_now()
                progress_artifact_id = self._write_progress_artifact(session, agent_run, iteration, observation, evaluation)
                iteration.progress_artifact_id = progress_artifact_id
                agent_run.heartbeat_at = utc_now()
                record_effectiveness_event(
                    session,
                    agent_run_id=agent_run_id,
                    iteration_id=iteration_id,
                    capability_type=_capability_type(selected.type),
                    capability_id=selected.name,
                    goal_type=str(evaluation.get("goal_type", "general")),
                    status="success" if observation.get("status") == "completed" else "failure",
                    error_code=observation.get("error_code"),
                    duration_ms=duration_ms,
                    output_artifact_count=1 if progress_artifact_id else 0,
                    iteration_count=iteration_index,
                )
                if evaluation["complete"]:
                    agent_run.status = "completed"
                    agent_run.stop_reason = "success"
                    agent_run.completed_at = utc_now()
                    agent_run.final_result_json = json.dumps(
                        {
                            "content": observation.get("output", ""),
                            "iterations": iteration_index,
                            "linked_run_id": observation.get("run_id"),
                            "progress_artifact_id": progress_artifact_id,
                            "selected_action": selected.to_dict(),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    final = AgentRunResult(
                        agent_run_id=agent_run_id,
                        status="completed",
                        stop_reason="success",
                        content=str(observation.get("output", "")),
                        linked_run_id=observation.get("run_id"),
                        progress_artifact_id=progress_artifact_id,
                    )
                elif iteration_index >= request.max_iterations:
                    agent_run.status = "failed"
                    agent_run.stop_reason = "max_iterations_reached"
                    agent_run.completed_at = utc_now()
                    agent_run.final_result_json = json.dumps(
                        {
                            "content": observation.get("output", ""),
                            "error": observation.get("error"),
                            "iterations": iteration_index,
                            "progress_artifact_id": progress_artifact_id,
                            "incomplete_conditions": evaluation.get("incomplete_conditions", []),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    final = AgentRunResult(
                        agent_run_id=agent_run_id,
                        status="failed",
                        stop_reason="max_iterations_reached",
                        content=str(observation.get("output") or observation.get("error") or ""),
                        linked_run_id=observation.get("run_id"),
                        progress_artifact_id=progress_artifact_id,
                    )
            if final is not None:
                break

        with session_scope(self.home) as session:
            consolidate_memory_candidates(session, agent_run_id=agent_run_id)

        assert final is not None
        return final

    def show(self, agent_run_id: str) -> dict[str, Any]:
        with session_scope(self.home) as session:
            row = session.get(AgentRun, agent_run_id)
            if row is None:
                raise ValueError(f"Unknown agent run: {agent_run_id}")
            iterations = (
                session.execute(
                    select(AgentIteration)
                    .where(AgentIteration.agent_run_id == agent_run_id)
                    .order_by(AgentIteration.iteration_index)
                )
                .scalars()
                .all()
            )
            return agent_run_to_dict(row, iterations)

    def iterations(self, agent_run_id: str) -> list[dict[str, Any]]:
        with session_scope(self.home) as session:
            if session.get(AgentRun, agent_run_id) is None:
                raise ValueError(f"Unknown agent run: {agent_run_id}")
            rows = (
                session.execute(
                    select(AgentIteration)
                    .where(AgentIteration.agent_run_id == agent_run_id)
                    .order_by(AgentIteration.iteration_index)
                )
                .scalars()
                .all()
            )
            return [agent_iteration_to_dict(row) for row in rows]

    def cancel(self, agent_run_id: str) -> dict[str, Any]:
        with session_scope(self.home) as session:
            row = session.get(AgentRun, agent_run_id)
            if row is None:
                raise ValueError(f"Unknown agent run: {agent_run_id}")
            row.status = "cancelled"
            row.stop_reason = "cancelled"
            row.completed_at = utc_now()
            return agent_run_to_dict(row, [])

    def _start_run(self, request: AgentRunRequest) -> str:
        with session_scope(self.home) as session:
            conversation_id = request.conversation_id
            if conversation_id is not None and session.get(Conversation, conversation_id) is None:
                raise ValueError(f"Unknown conversation: {conversation_id}")
            if conversation_id is None:
                conversation = Conversation(
                    id=str(uuid4()),
                    channel=request.channel,
                    external_user_id=request.external_user_id,
                    title=request.goal[:80],
                )
                session.add(conversation)
                conversation_id = conversation.id
            row = AgentRun(
                id=str(uuid4()),
                conversation_id=conversation_id,
                channel=request.channel,
                external_user_id=request.external_user_id,
                goal=request.goal,
                goal_package_json=json.dumps(
                    {
                        "goal": request.goal,
                        "success_criteria": ["observable action completes", "non-empty observation"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                status="running",
                max_iterations=request.max_iterations,
                started_at=utc_now(),
                heartbeat_at=utc_now(),
            )
            session.add(row)
            return row.id

    def _execute_action(
        self,
        selected: SelectedAction,
        request: AgentRunRequest,
        *,
        agent_run_id: str,
        iteration_id: str,
    ) -> dict[str, Any]:
        try:
            if selected.type == "skill_run":
                return self._execute_skill(selected, request)
            if selected.type == "workflow_run":
                return self._execute_workflow(selected.name, request.goal, request)
            if selected.type == "tool_call":
                return self._execute_tool(selected, request, agent_run_id=agent_run_id, iteration_id=iteration_id)
            return self._execute_workflow("simple_chat", request.goal, request)
        except WorkflowRunFailed as exc:
            return {
                "status": "failed",
                "run_id": exc.run_id,
                "output": "",
                "error": exc.message,
                "error_code": "workflow_failed",
            }
        except Exception as exc:
            return {
                "status": "failed",
                "run_id": None,
                "output": "",
                "error": str(exc),
                "error_code": "action_failed",
            }

    def _execute_skill(self, selected: SelectedAction, request: AgentRunRequest) -> dict[str, Any]:
        with session_scope(self.home) as session:
            ensure_builtin_skills(self.home, session)
            skill = session.get(Skill, selected.name)
            if skill is None:
                raise ValueError(f"Unknown skill: {selected.name}")
            if skill.enabled != "true":
                raise ValueError(f"Skill disabled: {selected.name}")
            workflow_path = Path(skill.workflow_path)
        workflow = load_workflow_yaml(workflow_path.read_text(encoding="utf-8"))
        payload = json.dumps(selected.input_data, ensure_ascii=False, sort_keys=True)
        result = WorkflowRunner(self.home).run(
            workflow,
            payload,
            channel=request.channel,
            external_user_id=request.external_user_id,
            run_mode=request.run_mode,
            conversation_id=request.conversation_id,
        )
        return {"status": "completed", "run_id": result.run_id, "output": result.output, "error": None}

    def _execute_workflow(self, workflow_id: str, user_input: str, request: AgentRunRequest) -> dict[str, Any]:
        workflow = WorkflowRegistry(load_config(self.home)).get(workflow_id)
        result = WorkflowRunner(self.home).run(
            workflow,
            user_input,
            channel=request.channel,
            external_user_id=request.external_user_id,
            run_mode=request.run_mode,
            conversation_id=request.conversation_id,
        )
        return {"status": "completed", "run_id": result.run_id, "output": result.output, "error": None}

    def _execute_tool(
        self,
        selected: SelectedAction,
        request: AgentRunRequest,
        *,
        agent_run_id: str,
        iteration_id: str,
    ) -> dict[str, Any]:
        registry = ToolRegistry(self.home)
        with session_scope(self.home) as session:
            conversation = Conversation(
                id=str(uuid4()),
                channel=request.channel,
                external_user_id=request.external_user_id,
                title=request.goal[:80],
            )
            session.add(conversation)
            run = Run(
                id=str(uuid4()),
                conversation_id=conversation.id,
                workflow_id="agent.tool_call",
                status="running",
                input_json=json.dumps({"goal": request.goal, "action": selected.to_dict()}, ensure_ascii=False, sort_keys=True),
                result_json="{}",
            )
            step = RunStep(
                id=str(uuid4()),
                run_id=run.id,
                node_id=f"agent.{iteration_id}",
                status="running",
                input_json=json.dumps(selected.input_data, ensure_ascii=False, sort_keys=True),
                output_json="{}",
            )
            session.add(run)
            session.add(step)
            try:
                result = registry.call(
                    selected.name,
                    selected.input_data,
                    ToolContext(home=self.home, run_id=run.id, step_id=step.id, session=session, run_mode=request.run_mode),
                )
                step.status = "completed"
                step.output_json = json.dumps({"content": result.content}, ensure_ascii=False, sort_keys=True)
                run.status = "completed"
                run.result_json = json.dumps(result.data | {"content": result.content}, ensure_ascii=False, sort_keys=True)
                tool_call = (
                    session.execute(
                        select(ToolCall)
                        .where(ToolCall.run_id == run.id)
                        .where(ToolCall.tool_name == selected.name)
                        .order_by(ToolCall.created_at.desc())
                    )
                    .scalars()
                    .first()
                )
                return {
                    "status": "completed",
                    "run_id": run.id,
                    "tool_call_id": tool_call.id if tool_call else None,
                    "output": result.content,
                    "error": None,
                }
            except Exception as exc:
                step.status = "failed"
                step.error = str(exc)
                run.status = "failed"
                run.error = str(exc)
                run.result_json = json.dumps({"error": str(exc)}, ensure_ascii=False, sort_keys=True)
                return {
                    "status": "failed",
                    "run_id": run.id,
                    "tool_call_id": None,
                    "output": "",
                    "error": str(exc),
                    "error_code": "tool_failed",
                }

    def _evaluate_observation(
        self,
        observation: dict[str, Any],
        iteration_index: int,
        max_iterations: int,
    ) -> dict[str, Any]:
        complete = observation.get("status") == "completed" and bool(str(observation.get("output", "")).strip())
        return {
            "complete": complete,
            "goal_type": "general",
            "next_action": "finish" if complete else "replan",
            "incomplete_conditions": [] if complete else ["action did not produce a completed non-empty observation"],
            "remaining_iterations": max(0, max_iterations - iteration_index),
        }

    def _write_progress_artifact(
        self,
        session,
        agent_run: AgentRun,
        iteration: AgentIteration,
        observation: dict[str, Any],
        evaluation: dict[str, Any],
    ) -> str | None:
        linked_run_id = observation.get("run_id")
        if not linked_run_id:
            return None
        root = self.home / "data" / "artifacts" / "agent_runs" / agent_run.id
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"iteration-{iteration.iteration_index}-progress.json"
        payload = {
            "agent_run_id": agent_run.id,
            "iteration_id": iteration.id,
            "iteration_index": iteration.iteration_index,
            "goal": agent_run.goal,
            "selected_action": _json_dict(iteration.selected_action_json),
            "observation": observation,
            "evaluation": evaluation,
            "heartbeat_at": agent_run.heartbeat_at.isoformat() if agent_run.heartbeat_at else "",
        }
        content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        path.write_text(content, encoding="utf-8")
        artifact = Artifact(
            id=str(uuid4()),
            run_id=linked_run_id,
            path=str(path),
            kind="agent_progress",
            mime="application/json",
            size_bytes=len(content.encode("utf-8")),
            sha256=sha256(content.encode("utf-8")).hexdigest(),
            metadata_json=json.dumps(
                {"agent_run_id": agent_run.id, "iteration_id": iteration.id},
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        session.add(artifact)
        return artifact.id


def agent_run_to_dict(row: AgentRun, iterations: list[AgentIteration]) -> dict[str, Any]:
    return {
        "id": row.id,
        "conversation_id": row.conversation_id,
        "channel": row.channel,
        "external_user_id": row.external_user_id,
        "goal": row.goal,
        "goal_package": _json_dict(row.goal_package_json),
        "status": row.status,
        "stop_reason": row.stop_reason,
        "final_result": _json_dict(row.final_result_json),
        "max_iterations": row.max_iterations,
        "heartbeat_at": row.heartbeat_at.isoformat() if row.heartbeat_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "iterations": [agent_iteration_to_dict(iteration) for iteration in iterations],
    }


def agent_iteration_to_dict(row: AgentIteration) -> dict[str, Any]:
    return {
        "id": row.id,
        "agent_run_id": row.agent_run_id,
        "iteration_index": row.iteration_index,
        "status": row.status,
        "plan": _json_dict(row.plan_json),
        "selected_action": _json_dict(row.selected_action_json),
        "observation": _json_dict(row.observation_json),
        "evaluation": _json_dict(row.evaluation_json),
        "linked_run_id": row.linked_run_id,
        "linked_tool_call_id": row.linked_tool_call_id,
        "checkpoint_id": row.checkpoint_id,
        "progress_artifact_id": row.progress_artifact_id,
        "error": row.error,
    }


def _capability_type(action_type: str) -> str:
    if action_type == "skill_run":
        return "skill"
    if action_type == "workflow_run":
        return "workflow"
    if action_type == "tool_call":
        return "tool"
    return "agent"


def _latest_checkpoint_id(session, run_id: str | None) -> str | None:
    if not run_id:
        return None
    row = (
        session.execute(select(Checkpoint).where(Checkpoint.run_id == run_id).order_by(Checkpoint.created_at.desc()))
        .scalars()
        .first()
    )
    return row.id if row is not None else None


def _json_dict(raw_json: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw_json or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}
