from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import or_, select

from agentend.core.agent_run import AgentRunController
from agentend.core.tasks import TaskManager, _cron_due, _same_minute
from agentend.db.models import AgentRun, Schedule, TaskItem, utc_now
from agentend.db.session import init_database, session_scope


@dataclass(frozen=True)
class WorkerResult:
    processed_tasks: int
    created_tasks: int
    schedule_count: int
    agent_run_ids: list[str]
    message: str


class AgentWorker:
    def __init__(self, home: Path, *, worker_id: str | None = None) -> None:
        self.home = home.expanduser().resolve()
        self.worker_id = worker_id or f"{socket.gethostname()}-{uuid4().hex[:8]}"
        init_database(self.home)

    def run_once(self) -> WorkerResult:
        created_from_schedules = self._enqueue_due_schedules()
        created_from_inbox = self._scan_inbox()
        task = self._claim_next_task()
        if task is None:
            created = created_from_schedules + created_from_inbox
            return WorkerResult(0, created, created_from_schedules, [], "no work")

        if task.agent_run_id is not None:
            with session_scope(self.home) as session:
                agent_run = session.get(AgentRun, task.agent_run_id)
            if agent_run and agent_run.status == "waiting_input":
                try:
                    result = AgentRunController(self.home).resume(
                        task.agent_run_id,
                        max_iterations=3,
                        run_mode=task.run_mode,
                    )
                except Exception as exc:
                    self._fail_task(task.id, reason=f"resume_error: {exc}")
                    return WorkerResult(
                        processed_tasks=1,
                        created_tasks=created_from_schedules + created_from_inbox,
                        schedule_count=created_from_schedules,
                        agent_run_ids=[task.agent_run_id],
                        message="resume_failed",
                    )
            else:
                self._fail_task(task.id, reason="agent_run not waiting_input")
                return WorkerResult(
                    processed_tasks=1,
                    created_tasks=created_from_schedules + created_from_inbox,
                    schedule_count=created_from_schedules,
                    agent_run_ids=[task.agent_run_id],
                    message="skipped_resume",
                )
        else:
            result = AgentRunController(self.home).run(
                task.input_text,
                channel="task",
                external_user_id=task.id,
                max_iterations=3,
                run_mode=task.run_mode,
            )

        self._complete_task(task.id, result.agent_run_id)
        return WorkerResult(
            processed_tasks=1,
            created_tasks=created_from_schedules + created_from_inbox,
            schedule_count=created_from_schedules,
            agent_run_ids=[result.agent_run_id],
            message="processed=1",
        )

    def run_forever(self, *, poll_interval: int, max_concurrency: int = 1) -> None:
        if max_concurrency != 1:
            raise ValueError("--max-concurrency currently only supports 1")
        while True:
            self.run_once()
            time.sleep(max(1, int(poll_interval)))

    def _enqueue_due_schedules(self) -> int:
        current = datetime.now(timezone.utc)
        manager = TaskManager(self.home)
        created = 0
        with session_scope(self.home) as session:
            schedules = session.execute(select(Schedule).where(Schedule.status == "active")).scalars().all()
            due_schedule_ids: list[str] = []
            for schedule in schedules:
                try:
                    due = _cron_due(schedule.cron, current)
                except ValueError as exc:
                    schedule.status = "paused"
                    schedule.paused_reason = f"invalid cron: {exc}"
                    schedule.last_error = str(exc)
                    continue
                if not due:
                    continue
                if schedule.last_triggered_at is not None and _same_minute(schedule.last_triggered_at, current):
                    continue
                due_schedule_ids.append(schedule.id)
        for schedule_id in due_schedule_ids:
            with session_scope(self.home) as session:
                schedule = session.get(Schedule, schedule_id)
                if schedule is None:
                    continue
                workflow_id = schedule.workflow_id
                input_text = schedule.input_text
            task = manager.add_task(
                workflow_id=workflow_id,
                input_text=input_text,
                title=f"Scheduled {workflow_id}",
                source="scheduler",
                schedule_id=schedule_id,
                run_mode="scheduler",
            )
            with session_scope(self.home) as session:
                schedule = session.get(Schedule, schedule_id)
                if schedule is not None:
                    schedule.last_task_id = task.id
                    schedule.last_triggered_at = current
            created += 1
        return created

    def _scan_inbox(self) -> int:
        manager = TaskManager(self.home)
        created = manager.scan_inbox_once(workflow_id="simple_chat", limit=5, retry_after_seconds=0)
        return len(created)

    def _claim_next_task(self) -> TaskItem | None:
        now = datetime.now(timezone.utc)
        with session_scope(self.home) as session:
            task = (
                session.execute(
                    select(TaskItem)
                    .where(TaskItem.status == "pending")
                    .where(or_(TaskItem.retry_after_at.is_(None), TaskItem.retry_after_at <= now))
                    .order_by(TaskItem.created_at)
                )
                .scalars()
                .first()
            )
            if task is None:
                return None
            task.status = "running"
            task.claimed_at = utc_now()
            task.heartbeat_at = utc_now()
            task.worker_id = self.worker_id
            task.error = None
            return task

    def _fail_task(self, task_id: str, *, reason: str) -> None:
        with session_scope(self.home) as session:
            task = session.get(TaskItem, task_id)
            if task is not None:
                task.status = "failed"
                task.error = reason
                task.heartbeat_at = utc_now()

    def _complete_task(self, task_id: str, agent_run_id: str) -> None:
        with session_scope(self.home) as session:
            task = session.get(TaskItem, task_id)
            agent_run = session.get(AgentRun, agent_run_id)
            if task is None or agent_run is None:
                return
            final_result = _json_dict(agent_run.final_result_json)
            task.agent_run_id = agent_run_id
            task.status = "completed" if agent_run.status == "completed" else "failed"
            task.error = None if task.status == "completed" else str(final_result.get("error") or agent_run.stop_reason)
            task.heartbeat_at = agent_run.heartbeat_at
            task.progress_artifact_id = str(final_result.get("progress_artifact_id") or "")
            task.resume_cursor_json = json.dumps(
                {
                    "agent_run_id": agent_run_id,
                    "stop_reason": agent_run.stop_reason,
                    "completed_at": agent_run.completed_at.isoformat() if agent_run.completed_at else "",
                    "progress_artifact_id": str(final_result.get("progress_artifact_id") or ""),
                    "missing_requirements": final_result.get("missing_requirements", []),
                    "partial_result": str(final_result.get("partial_result") or final_result.get("content") or ""),
                    "resume_cursor": final_result.get("resume_cursor", {}),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            if task.schedule_id:
                schedule = session.get(Schedule, task.schedule_id)
                if schedule is not None:
                    schedule.last_run_id = final_result.get("linked_run_id")
                    if task.status == "completed":
                        schedule.consecutive_failures = 0
                        schedule.last_error = None
                    else:
                        schedule.consecutive_failures += 1
                        schedule.last_error = task.error
                        if schedule.consecutive_failures >= schedule.max_consecutive_failures:
                            schedule.status = "paused"
                            schedule.paused_reason = f"auto-paused after {schedule.consecutive_failures} failures"


def _json_dict(raw_json: str) -> dict[str, object]:
    try:
        payload = json.loads(raw_json or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}
