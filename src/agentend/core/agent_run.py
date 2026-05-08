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
from agentend.core.agent_evaluator import evaluate_goal_observation
from agentend.core.agent_selector import SelectedAction, select_next_action_with_trace
from agentend.core.clarifications import create_clarification_request
from agentend.core.effectiveness import record_effectiveness_event
from agentend.core.events import record_event
from agentend.core.goal_analyzer import analyze_goal
from agentend.core.intent_audit import record_intent_decision
from agentend.core.memory_consolidator import consolidate_memory_candidates
from agentend.core.memory_store import search_memory_items
from agentend.core.skills import ensure_builtin_skills
from agentend.core.tool_registry import ToolRegistry
from agentend.core.workflow_registry import WorkflowRegistry
from agentend.core.workflow_runner import WorkflowRunFailed, WorkflowRunner
from agentend.core.workflow_schema import load_workflow_yaml
from agentend.db.models import (
    AgentIteration,
    AgentEvaluationEvent,
    AgentRun,
    Artifact,
    Checkpoint,
    Conversation,
    Run,
    RunStep,
    Skill,
    ToolCall,
    MemoryUseEvent,
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
        agent_run_id = self._start_run(request)
        gated_result = self._initial_intent_gate(request, agent_run_id=agent_run_id)
        if gated_result is not None:
            return gated_result
        if not request.goal:
            raise ValueError("Agent goal must not be empty")
        return self._run_loop(
            request,
            agent_run_id=agent_run_id,
            start_iteration_index=1,
            max_iteration_index=request.max_iterations,
            previous_observations=[],
        )

    def resume(
        self,
        agent_run_id: str,
        *,
        max_iterations: int = 3,
        run_mode: str = "normal",
    ) -> AgentRunResult:
        additional_iterations = max(1, int(max_iterations))
        with session_scope(self.home) as session:
            agent_run = session.get(AgentRun, agent_run_id)
            if agent_run is None:
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
            if agent_run.status == "completed":
                return _result_from_agent_run(agent_run)
            if agent_run.status == "cancelled":
                raise ValueError(f"Cannot resume cancelled agent run: {agent_run_id}")
            previous_observations = _previous_observations_from_iterations(iterations)
            start_iteration_index = (iterations[-1].iteration_index + 1) if iterations else 1
            max_iteration_index = start_iteration_index + additional_iterations - 1
            agent_run.status = "running"
            agent_run.stop_reason = None
            agent_run.completed_at = None
            agent_run.heartbeat_at = utc_now()
            agent_run.max_iterations = max(agent_run.max_iterations, max_iteration_index)
            request = AgentRunRequest(
                goal=agent_run.goal,
                channel=agent_run.channel,
                external_user_id=agent_run.external_user_id,
                max_iterations=additional_iterations,
                run_mode=run_mode,
                conversation_id=agent_run.conversation_id,
            )
        return self._run_loop(
            request,
            agent_run_id=agent_run_id,
            start_iteration_index=start_iteration_index,
            max_iteration_index=max_iteration_index,
            previous_observations=previous_observations,
        )

    def _run_loop(
        self,
        request: AgentRunRequest,
        *,
        agent_run_id: str,
        start_iteration_index: int,
        max_iteration_index: int,
        previous_observations: list[dict[str, Any]],
    ) -> AgentRunResult:
        final: AgentRunResult | None = None

        for iteration_index in range(start_iteration_index, max_iteration_index + 1):
            with session_scope(self.home) as session:
                agent_run = session.get(AgentRun, agent_run_id)
                assert agent_run is not None
                goal_analysis = analyze_goal(self.home, session, request.goal)
                intent_record = record_intent_decision(
                    self.home,
                    session,
                    request.goal,
                    goal_analysis.get("intent_decision", {}),
                    conversation_id=agent_run.conversation_id,
                    agent_run_id=agent_run_id,
                    channel=request.channel,
                    external_user_id=request.external_user_id,
                    route_type="agent_iteration",
                    context_summary={"source": "agent_run.loop", "iteration_index": iteration_index},
                )
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
                    "intent_decision_id": intent_record.id,
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
            evaluation = self._evaluate_observation(request.goal, goal_analysis, observation, iteration_index, max_iteration_index)
            selector_observation = observation | {"action_name": selected.name}
            if not evaluation["complete"]:
                selector_observation["status"] = "incomplete"
                selector_observation["error_code"] = observation.get("error_code") or "goal_incomplete"
                selector_observation["missing_requirements"] = evaluation.get("missing_requirements", [])
            previous_observations.append(selector_observation)

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
                self._record_evaluation_event(session, agent_run_id, iteration_id, evaluation)
                record_effectiveness_event(
                    session,
                    agent_run_id=agent_run_id,
                    iteration_id=iteration_id,
                    capability_type=_capability_type(selected.type),
                    capability_id=selected.name,
                    goal_type=str(evaluation.get("goal_type", "general")),
                    status="success" if evaluation["complete"] else "failure",
                    error_code=observation.get("error_code") or (None if evaluation["complete"] else "goal_incomplete"),
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
                            "satisfied_requirements": evaluation.get("satisfied_requirements", []),
                            "missing_requirements": evaluation.get("missing_requirements", []),
                            "evidence_refs": evaluation.get("evidence_refs", []),
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
                elif iteration_index >= max_iteration_index:
                    agent_run.status = "failed"
                    agent_run.stop_reason = "max_iterations_reached"
                    agent_run.completed_at = utc_now()
                    agent_run.final_result_json = json.dumps(
                        {
                            "content": observation.get("output", ""),
                            "partial_result": observation.get("output", ""),
                            "error": observation.get("error"),
                            "iterations": iteration_index,
                            "progress_artifact_id": progress_artifact_id,
                            "incomplete_conditions": evaluation.get("incomplete_conditions", []),
                            "missing_requirements": evaluation.get("missing_requirements", []),
                            "resume_cursor": _resume_cursor(agent_run_id, iteration_index, evaluation, progress_artifact_id),
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
            self._record_memory_use_events(session, agent_run_id)
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

    def _initial_intent_gate(self, request: AgentRunRequest, *, agent_run_id: str) -> AgentRunResult | None:
        with session_scope(self.home) as session:
            goal_analysis = analyze_goal(self.home, session, request.goal)
            intent = goal_analysis.get("intent_decision")
            if not isinstance(intent, dict):
                return None
            intent_type = str(intent.get("intent_type") or "")
            missing_inputs = [str(item) for item in intent.get("missing_inputs") or []]
            if intent_type == "blocked":
                return self._record_blocked_intent(
                    session,
                    request,
                    agent_run_id=agent_run_id,
                    goal_analysis=goal_analysis,
                    intent=intent,
                )
            if intent_type == "clarification" or missing_inputs:
                return self._record_clarification_intent(
                    session,
                    request,
                    agent_run_id=agent_run_id,
                    goal_analysis=goal_analysis,
                    intent=intent,
                    missing_inputs=missing_inputs,
                )
        return None

    def _record_clarification_intent(
        self,
        session,
        request: AgentRunRequest,
        *,
        agent_run_id: str,
        goal_analysis: dict[str, Any],
        intent: dict[str, Any],
        missing_inputs: list[str],
    ) -> AgentRunResult:
        agent_run = session.get(AgentRun, agent_run_id)
        assert agent_run is not None
        question = str(intent.get("clarification_question") or _clarification_question(missing_inputs))
        run = Run(
            id=str(uuid4()),
            conversation_id=agent_run.conversation_id,
            workflow_id="intent.clarification",
            status="waiting_input",
            input_json=json.dumps(
                {
                    "input": request.goal,
                    "agent_run_id": agent_run_id,
                    "missing_inputs": missing_inputs,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            result_json="{}",
        )
        step = RunStep(
            id=str(uuid4()),
            run_id=run.id,
            node_id="intent.clarification",
            status="waiting_input",
            input_json=json.dumps({"goal": request.goal, "intent_decision": intent}, ensure_ascii=False, sort_keys=True),
            output_json=json.dumps({"prompt": question}, ensure_ascii=False, sort_keys=True),
        )
        session.add(run)
        session.add(step)
        session.flush()
        clarification = create_clarification_request(
            session,
            run_id=run.id,
            step_id=step.id,
            request_type=_clarification_request_type(missing_inputs),
            question=question,
            reason=str(intent.get("routing_reason") or ""),
        )
        payload = {
            "content": question,
            "agent_run_id": agent_run_id,
            "clarification_request_id": clarification.id,
            "goal_analysis": goal_analysis,
            "intent_decision": intent,
            "linked_run_id": run.id,
            "missing_inputs": missing_inputs,
        }
        intent_record = record_intent_decision(
            self.home,
            session,
            request.goal,
            intent,
            conversation_id=agent_run.conversation_id,
            run_id=run.id,
            agent_run_id=agent_run_id,
            channel=request.channel,
            external_user_id=request.external_user_id,
            route_type="clarification",
            context_summary={"source": "agent_run.gate", "missing_inputs": missing_inputs},
        )
        payload["intent_decision_id"] = intent_record.id
        run.result_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        agent_run.status = "waiting_input"
        agent_run.stop_reason = "clarification_required"
        agent_run.final_result_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        agent_run.heartbeat_at = utc_now()
        record_event(
            session,
            "agent.intent_clarification_required",
            {"agent_run_id": agent_run_id, "run_id": run.id, "missing_inputs": missing_inputs},
            run_id=run.id,
        )
        return AgentRunResult(
            agent_run_id=agent_run_id,
            status="waiting_input",
            stop_reason="clarification_required",
            content=question,
            linked_run_id=run.id,
        )

    def _record_blocked_intent(
        self,
        session,
        request: AgentRunRequest,
        *,
        agent_run_id: str,
        goal_analysis: dict[str, Any],
        intent: dict[str, Any],
    ) -> AgentRunResult:
        agent_run = session.get(AgentRun, agent_run_id)
        assert agent_run is not None
        reason = str(intent.get("routing_reason") or "high-risk intent")
        content = f"已阻止该请求：{reason}"
        run = Run(
            id=str(uuid4()),
            conversation_id=agent_run.conversation_id,
            workflow_id="intent.blocked",
            status="blocked",
            input_json=json.dumps({"input": request.goal, "agent_run_id": agent_run_id}, ensure_ascii=False, sort_keys=True),
            result_json="{}",
            error=reason,
        )
        step = RunStep(
            id=str(uuid4()),
            run_id=run.id,
            node_id="intent.blocked",
            status="blocked",
            input_json=json.dumps({"goal": request.goal, "intent_decision": intent}, ensure_ascii=False, sort_keys=True),
            output_json=json.dumps({"content": content}, ensure_ascii=False, sort_keys=True),
            error=reason,
        )
        session.add(run)
        session.add(step)
        payload = {
            "content": content,
            "agent_run_id": agent_run_id,
            "goal_analysis": goal_analysis,
            "intent_decision": intent,
            "linked_run_id": run.id,
            "risk_level": intent.get("risk_level"),
            "risk_notes": intent.get("risk_notes") or [],
        }
        intent_record = record_intent_decision(
            self.home,
            session,
            request.goal,
            intent,
            conversation_id=agent_run.conversation_id,
            run_id=run.id,
            agent_run_id=agent_run_id,
            channel=request.channel,
            external_user_id=request.external_user_id,
            route_type="blocked",
            context_summary={"source": "agent_run.gate", "blocked_reason": reason},
        )
        payload["intent_decision_id"] = intent_record.id
        run.result_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        agent_run.status = "blocked"
        agent_run.stop_reason = "intent_blocked"
        agent_run.completed_at = utc_now()
        agent_run.heartbeat_at = utc_now()
        agent_run.final_result_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        record_event(
            session,
            "agent.intent_blocked",
            {"agent_run_id": agent_run_id, "run_id": run.id, "reason": reason},
            run_id=run.id,
        )
        return AgentRunResult(
            agent_run_id=agent_run_id,
            status="blocked",
            stop_reason="intent_blocked",
            content=content,
            linked_run_id=run.id,
        )

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
        goal: str,
        goal_analysis: dict[str, Any],
        observation: dict[str, Any],
        iteration_index: int,
        max_iterations: int,
    ) -> dict[str, Any]:
        return evaluate_goal_observation(
            goal,
            observation,
            iteration_index=iteration_index,
            max_iterations=max_iterations,
            goal_analysis=goal_analysis,
        )

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
            "progress": _progress_envelope(iteration.iteration_index, observation, evaluation),
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

    def _record_evaluation_event(
        self,
        session,
        agent_run_id: str,
        iteration_id: str,
        evaluation: dict[str, Any],
    ) -> None:
        session.add(
            AgentEvaluationEvent(
                id=str(uuid4()),
                agent_run_id=agent_run_id,
                iteration_id=iteration_id,
                goal_type=str(evaluation.get("goal_type", "general")),
                complete="true" if evaluation.get("complete") else "false",
                confidence=str(evaluation.get("confidence", "0.0")),
                requirements_json=json.dumps(evaluation.get("requirements", []), ensure_ascii=False, sort_keys=True),
                satisfied_requirements_json=json.dumps(evaluation.get("satisfied_requirements", []), ensure_ascii=False, sort_keys=True),
                missing_requirements_json=json.dumps(evaluation.get("missing_requirements", []), ensure_ascii=False, sort_keys=True),
                evidence_refs_json=json.dumps(evaluation.get("evidence_refs", []), ensure_ascii=False, sort_keys=True),
                next_probe=str(evaluation.get("next_probe") or "") or None,
            )
        )

    def _record_memory_use_events(self, session, agent_run_id: str) -> None:
        agent_run = session.get(AgentRun, agent_run_id)
        if agent_run is None:
            return
        iterations = (
            session.execute(
                select(AgentIteration)
                .where(AgentIteration.agent_run_id == agent_run_id)
                .order_by(AgentIteration.iteration_index)
            )
            .scalars()
            .all()
        )
        outcome = "helped" if agent_run.status == "completed" else "not_enough"
        for iteration in iterations:
            plan = _json_dict(iteration.plan_json)
            memory_ids = [str(item) for item in plan.get("memory_ids", []) if item]
            if not memory_ids:
                continue
            action = _json_dict(iteration.selected_action_json)
            selected_action = str(action.get("name") or "")
            for memory_id in memory_ids:
                existing = (
                    session.execute(
                        select(MemoryUseEvent)
                        .where(MemoryUseEvent.agent_run_id == agent_run_id)
                        .where(MemoryUseEvent.iteration_id == iteration.id)
                        .where(MemoryUseEvent.memory_id == memory_id)
                    )
                    .scalars()
                    .first()
                )
                if existing is not None:
                    existing.selected_action = selected_action
                    existing.run_status = agent_run.status
                    existing.outcome = outcome
                    continue
                session.add(
                    MemoryUseEvent(
                        id=str(uuid4()),
                        memory_id=memory_id,
                        agent_run_id=agent_run_id,
                        iteration_id=iteration.id,
                        query=agent_run.goal,
                        selected_action=selected_action,
                        run_status=agent_run.status,
                        outcome=outcome,
                    )
                )


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


def _result_from_agent_run(row: AgentRun) -> AgentRunResult:
    final_result = _json_dict(row.final_result_json)
    return AgentRunResult(
        agent_run_id=row.id,
        status=row.status,
        stop_reason=row.stop_reason or "",
        content=str(final_result.get("content") or ""),
        linked_run_id=str(final_result.get("linked_run_id") or "") or None,
        progress_artifact_id=str(final_result.get("progress_artifact_id") or "") or None,
    )


def _clarification_question(missing_inputs: list[str]) -> str:
    if "path" in missing_inputs:
        return "请提供要写入的文件路径。"
    if "content" in missing_inputs:
        return "请提供要写入的内容。"
    if "risk_confirmation" in missing_inputs:
        return "该请求风险较高。请确认目标、范围和允许的操作。"
    if "goal" in missing_inputs:
        return "What goal should AgentEnd work on?"
    return "请补充完成该目标所需的信息。"


def _clarification_request_type(missing_inputs: list[str]) -> str:
    if "risk_confirmation" in missing_inputs:
        return "risk_confirmation"
    if missing_inputs:
        return "missing_input"
    return "ambiguous_goal"


def _previous_observations_from_iterations(iterations: list[AgentIteration]) -> list[dict[str, Any]]:
    previous: list[dict[str, Any]] = []
    for iteration in iterations:
        observation = _json_dict(iteration.observation_json)
        if not observation:
            continue
        action = _json_dict(iteration.selected_action_json)
        evaluation = _json_dict(iteration.evaluation_json)
        selector_observation = observation | {"action_name": str(action.get("name") or "")}
        if not evaluation.get("complete", observation.get("status") == "completed"):
            selector_observation["status"] = "incomplete"
            selector_observation["error_code"] = observation.get("error_code") or "goal_incomplete"
            selector_observation["missing_requirements"] = evaluation.get("missing_requirements", [])
        previous.append(selector_observation)
    return previous


def _progress_envelope(iteration_index: int, observation: dict[str, Any], evaluation: dict[str, Any]) -> dict[str, Any]:
    evidence = list(evaluation.get("evidence_refs", []))
    blockers = list(evaluation.get("missing_requirements", []))
    return {
        "done": [f"iteration {iteration_index} recorded"],
        "doing": "finish" if evaluation.get("complete") else "replan",
        "next": str(evaluation.get("next_probe") or evaluation.get("next_action") or ""),
        "blockers": [] if evaluation.get("complete") else blockers,
        "evidence": evidence,
        "goal_state": {
            "satisfied_requirements": list(evaluation.get("satisfied_requirements", [])),
            "missing_requirements": list(evaluation.get("missing_requirements", [])),
            "confidence": evaluation.get("confidence", 0.0),
            "observation_status": observation.get("status"),
        },
    }


def _resume_cursor(
    agent_run_id: str,
    iteration_index: int,
    evaluation: dict[str, Any],
    progress_artifact_id: str | None,
) -> dict[str, Any]:
    return {
        "agent_run_id": agent_run_id,
        "next_iteration_index": iteration_index + 1,
        "next_probe": evaluation.get("next_probe"),
        "missing_requirements": evaluation.get("missing_requirements", []),
        "progress_artifact_id": progress_artifact_id,
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
