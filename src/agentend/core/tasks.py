from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from sqlalchemy import or_, select

from agentend.config import load_config
from agentend.core.events import record_event
from agentend.core.workflow_registry import WorkflowRegistry
from agentend.core.workflow_runner import WorkflowRunFailed, WorkflowRunner
from agentend.db.models import Schedule, TaskItem
from agentend.db.session import init_database, session_scope


TASK_STATUSES = {"pending", "running", "blocked", "completed", "failed"}
SCHEDULE_STATUSES = {"active", "paused"}
DEFAULT_SCHEDULE_FAILURE_THRESHOLD = 3
DEFAULT_INBOX_BATCH_LIMIT = 20


@dataclass(frozen=True)
class TaskRunOutcome:
    task_id: str
    status: str
    run_id: str | None
    output: str
    error: str | None = None


@dataclass(frozen=True)
class ScheduleRunOutcome:
    schedule_id: str
    task_id: str
    run_id: str | None
    status: str
    output: str
    error: str | None = None


class TaskManager:
    def __init__(self, home: Path) -> None:
        self.home = home.expanduser().resolve()
        init_database(self.home)

    def add_task(
        self,
        *,
        workflow_id: str,
        input_text: str,
        title: str | None = None,
        source: str = "manual",
        source_path: str | None = None,
        source_hash: str | None = None,
        batch_id: str | None = None,
        schedule_id: str | None = None,
        run_mode: str = "normal",
        retry_after_at: datetime | None = None,
    ) -> TaskItem:
        with session_scope(self.home) as session:
            task = TaskItem(
                id=str(uuid4()),
                title=title or _default_title(input_text, workflow_id),
                workflow_id=workflow_id,
                input_text=input_text,
                status="pending",
                source=source,
                source_path=source_path,
                source_hash=source_hash,
                batch_id=batch_id,
                schedule_id=schedule_id,
                run_mode=run_mode,
                retry_after_at=retry_after_at,
            )
            session.add(task)
            record_event(
                session,
                "task.created",
                {
                    "task_id": task.id,
                    "workflow_id": workflow_id,
                    "source": source,
                    "source_path": source_path,
                    "source_hash": source_hash,
                    "batch_id": batch_id,
                    "run_mode": run_mode,
                },
            )
            return task

    def list_tasks(self, status: str | None = None) -> list[TaskItem]:
        with session_scope(self.home) as session:
            query = select(TaskItem).order_by(TaskItem.created_at.desc())
            if status:
                query = query.where(TaskItem.status == status)
            return list(session.execute(query).scalars().all())

    def resume_task(self, task_id: str, message: str | None = None) -> TaskItem:
        with session_scope(self.home) as session:
            task = _get_task(session, task_id)
            if message is not None:
                task.input_text = message
                task.title = _default_title(message, task.workflow_id)
            task.status = "pending"
            task.error = None
            record_event(session, "task.resumed", {"task_id": task.id})
            return task

    def run_task(self, task_id: str, *, run_mode: str = "normal") -> TaskRunOutcome:
        with session_scope(self.home) as session:
            task = _get_task(session, task_id)
            workflow_id = task.workflow_id
            input_text = task.input_text
            task.status = "running"
            task.error = None
            task.run_mode = run_mode
            record_event(session, "task.started", {"task_id": task.id, "workflow_id": workflow_id})

        try:
            workflow = WorkflowRegistry(load_config(self.home)).get(workflow_id)
            result = WorkflowRunner(self.home).run(workflow, input_text, channel="task", run_mode=run_mode)
        except WorkflowRunFailed as exc:
            with session_scope(self.home) as session:
                task = _get_task(session, task_id)
                task.status = "failed"
                task.run_id = exc.run_id
                task.error = exc.message
                record_event(session, "task.failed", {"task_id": task.id, "error": exc.message}, run_id=exc.run_id)
            return TaskRunOutcome(task_id=task_id, status="failed", run_id=exc.run_id, output="", error=exc.message)
        except Exception as exc:
            with session_scope(self.home) as session:
                task = _get_task(session, task_id)
                task.status = "failed"
                task.error = str(exc)
                record_event(session, "task.failed", {"task_id": task.id, "error": str(exc)})
            return TaskRunOutcome(task_id=task_id, status="failed", run_id=None, output="", error=str(exc))

        with session_scope(self.home) as session:
            task = _get_task(session, task_id)
            task.status = "completed"
            task.run_id = result.run_id
            task.error = None
            record_event(session, "task.completed", {"task_id": task.id}, run_id=result.run_id)
        return TaskRunOutcome(task_id=task_id, status="completed", run_id=result.run_id, output=result.output)

    def add_schedule(
        self,
        *,
        workflow_id: str,
        cron: str,
        input_text: str = "",
        status: str = "active",
        max_consecutive_failures: int = DEFAULT_SCHEDULE_FAILURE_THRESHOLD,
    ) -> Schedule:
        if status not in SCHEDULE_STATUSES:
            raise ValueError("Schedule status must be active or paused")
        if max_consecutive_failures < 1:
            raise ValueError("Schedule max consecutive failures must be at least 1")
        validate_cron(cron)
        with session_scope(self.home) as session:
            schedule = Schedule(
                id=str(uuid4()),
                workflow_id=workflow_id,
                cron=cron,
                input_text=input_text,
                status=status,
                max_consecutive_failures=max_consecutive_failures,
            )
            session.add(schedule)
            record_event(
                session,
                "schedule.created",
                {
                    "schedule_id": schedule.id,
                    "workflow_id": workflow_id,
                    "cron": cron,
                    "max_consecutive_failures": max_consecutive_failures,
                },
            )
            return schedule

    def list_schedules(self) -> list[Schedule]:
        with session_scope(self.home) as session:
            return list(session.execute(select(Schedule).order_by(Schedule.created_at.desc())).scalars().all())

    def remove_schedule(self, schedule_id: str) -> None:
        with session_scope(self.home) as session:
            schedule = _get_schedule(session, schedule_id)
            session.delete(schedule)
            record_event(session, "schedule.removed", {"schedule_id": schedule_id})

    def run_schedule_now(self, schedule_id: str) -> ScheduleRunOutcome:
        with session_scope(self.home) as session:
            schedule = _get_schedule(session, schedule_id)
            if schedule.status != "active":
                raise ValueError(f"Schedule is not active: {schedule_id}")
            workflow_id = schedule.workflow_id
            input_text = schedule.input_text

        task = self.add_task(
            workflow_id=workflow_id,
            input_text=input_text,
            title=f"Scheduled {workflow_id}",
            source="scheduler",
            schedule_id=schedule_id,
            run_mode="scheduler",
        )
        outcome = self.run_task(task.id, run_mode="scheduler")
        with session_scope(self.home) as session:
            schedule = _get_schedule(session, schedule_id)
            schedule.last_task_id = task.id
            schedule.last_run_id = outcome.run_id
            _record_schedule_outcome(session, schedule, outcome)
            record_event(
                session,
                "schedule.triggered",
                {
                    "schedule_id": schedule_id,
                    "task_id": task.id,
                    "status": outcome.status,
                    "consecutive_failures": schedule.consecutive_failures,
                },
                run_id=outcome.run_id,
            )
        return ScheduleRunOutcome(
            schedule_id=schedule_id,
            task_id=task.id,
            run_id=outcome.run_id,
            status=outcome.status,
            output=outcome.output,
            error=outcome.error,
        )

    def run_due_schedules(self, now: datetime | None = None) -> list[ScheduleRunOutcome]:
        current = now or datetime.now(timezone.utc)
        with session_scope(self.home) as session:
            schedules = list(session.execute(select(Schedule).where(Schedule.status == "active")).scalars().all())

        outcomes: list[ScheduleRunOutcome] = []
        for schedule in schedules:
            try:
                due = _cron_due(schedule.cron, current)
            except ValueError as exc:
                with session_scope(self.home) as session:
                    refreshed = _get_schedule(session, schedule.id)
                    refreshed.status = "paused"
                    refreshed.paused_reason = f"invalid cron: {exc}"
                    refreshed.last_error = str(exc)
                    record_event(
                        session,
                        "schedule.paused",
                        {"schedule_id": schedule.id, "reason": refreshed.paused_reason},
                    )
                continue
            if not due:
                continue
            if schedule.last_triggered_at is not None and _same_minute(schedule.last_triggered_at, current):
                continue
            outcome = self.run_schedule_now(schedule.id)
            with session_scope(self.home) as session:
                refreshed = _get_schedule(session, schedule.id)
                refreshed.last_triggered_at = current
            outcomes.append(outcome)
        return outcomes

    def scan_inbox_once(
        self,
        *,
        workflow_id: str,
        inbox_dir: Path | None = None,
        limit: int = DEFAULT_INBOX_BATCH_LIMIT,
        batch_id: str | None = None,
        retry_after_seconds: int = 0,
    ) -> list[TaskItem]:
        if limit < 1:
            raise ValueError("Inbox batch limit must be at least 1")
        if retry_after_seconds < 0:
            raise ValueError("Inbox retry-after seconds must be zero or greater")
        root = (inbox_dir or (self.home / "data" / "inbox")).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        current_batch_id = batch_id or str(uuid4())
        retry_after_at = (
            datetime.now(timezone.utc) + timedelta(seconds=retry_after_seconds) if retry_after_seconds > 0 else None
        )
        created: list[TaskItem] = []
        for path in sorted(child for child in root.iterdir() if child.is_file()):
            if len(created) >= limit:
                break
            source_path = str(path)
            source_hash = _file_sha256(path)
            with session_scope(self.home) as session:
                existing = session.execute(
                    select(TaskItem)
                    .where(TaskItem.source == "file_inbox")
                    .where(or_(TaskItem.source_path == source_path, TaskItem.source_hash == source_hash))
                ).first()
                if existing is not None:
                    continue
                record_event(
                    session,
                    "inbox.file_detected",
                    {
                        "path": source_path,
                        "workflow_id": workflow_id,
                        "source_hash": source_hash,
                        "batch_id": current_batch_id,
                    },
                )
            created.append(
                self.add_task(
                    workflow_id=workflow_id,
                    input_text=source_path,
                    title=f"Inbox file: {path.name}",
                    source="file_inbox",
                    source_path=source_path,
                    source_hash=source_hash,
                    batch_id=current_batch_id,
                    retry_after_at=retry_after_at,
                )
            )
        return created


def validate_cron(cron: str) -> None:
    fields = cron.split()
    if len(fields) != 5:
        raise ValueError("Cron expression must have five fields")
    ranges = (
        ("minute", fields[0], 0, 59, False),
        ("hour", fields[1], 0, 23, False),
        ("day", fields[2], 1, 31, False),
        ("month", fields[3], 1, 12, False),
        ("weekday", fields[4], 0, 7, True),
    )
    for name, field, minimum, maximum, allow_sunday_alias in ranges:
        _cron_field_values(field, minimum, maximum, allow_sunday_alias, name=name)


def _default_title(input_text: str, workflow_id: str) -> str:
    stripped = input_text.strip()
    return stripped[:80] if stripped else workflow_id


def _get_task(session, task_id: str) -> TaskItem:
    task = session.get(TaskItem, task_id)
    if task is None:
        raise ValueError(f"Unknown task: {task_id}")
    return task


def _get_schedule(session, schedule_id: str) -> Schedule:
    schedule = session.get(Schedule, schedule_id)
    if schedule is None:
        raise ValueError(f"Unknown schedule: {schedule_id}")
    return schedule


def _cron_due(cron: str, now: datetime) -> bool:
    fields = cron.split()
    validate_cron(cron)
    minute, hour, day, month, weekday = fields
    cron_weekday = (now.weekday() + 1) % 7
    return (
        _cron_field_matches(minute, now.minute, 0, 59)
        and _cron_field_matches(hour, now.hour, 0, 23)
        and _cron_field_matches(day, now.day, 1, 31)
        and _cron_field_matches(month, now.month, 1, 12)
        and _cron_field_matches(weekday, cron_weekday, 0, 7, allow_sunday_alias=True)
    )


def _cron_field_matches(
    field: str,
    value: int,
    minimum: int,
    maximum: int,
    *,
    allow_sunday_alias: bool = False,
) -> bool:
    values = _cron_field_values(field, minimum, maximum, allow_sunday_alias)
    return value in values


def _cron_field_values(
    field: str,
    minimum: int,
    maximum: int,
    allow_sunday_alias: bool = False,
    *,
    name: str = "field",
) -> set[int]:
    values: set[int] = set()
    for raw_part in field.split(","):
        part = raw_part.strip()
        if not part:
            raise ValueError(f"Cron {name} contains an empty list item")

        base, step = _split_cron_step(part, name)
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            start = _parse_cron_int(start_text, minimum, maximum, allow_sunday_alias, name)
            end = _parse_cron_int(end_text, minimum, maximum, allow_sunday_alias, name)
            if start > end:
                raise ValueError(f"Cron {name} range start must not exceed range end")
        else:
            start = _parse_cron_int(base, minimum, maximum, allow_sunday_alias, name)
            end = start

        for item in range(start, end + 1, step):
            values.add(0 if allow_sunday_alias and item == 7 else item)
    if not values:
        raise ValueError(f"Cron {name} must not be empty")
    return values


def _split_cron_step(part: str, name: str) -> tuple[str, int]:
    if "/" not in part:
        return part, 1
    base, step_text = part.split("/", 1)
    if not base or not step_text:
        raise ValueError(f"Cron {name} step is incomplete")
    step = _parse_positive_int(step_text, f"Cron {name} step")
    return base, step


def _parse_cron_int(text: str, minimum: int, maximum: int, allow_sunday_alias: bool, name: str) -> int:
    value = _parse_positive_int(text, f"Cron {name} value", allow_zero=True)
    if value < minimum or value > maximum:
        raise ValueError(f"Cron {name} value must be between {minimum} and {maximum}")
    if allow_sunday_alias and value == 7:
        return value
    return value


def _parse_positive_int(text: str, label: str, *, allow_zero: bool = False) -> int:
    if not text.isdigit():
        raise ValueError(f"{label} must be an integer")
    value = int(text)
    if value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"{label} must be greater than zero")
    return value


def _same_minute(left: datetime, right: datetime) -> bool:
    return (
        left.year,
        left.month,
        left.day,
        left.hour,
        left.minute,
    ) == (
        right.year,
        right.month,
        right.day,
        right.hour,
        right.minute,
    )


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record_schedule_outcome(session, schedule: Schedule, outcome: TaskRunOutcome) -> None:
    if outcome.status == "completed":
        schedule.consecutive_failures = 0
        schedule.last_error = None
        schedule.paused_reason = None
        return

    schedule.consecutive_failures += 1
    schedule.last_error = outcome.error
    if schedule.consecutive_failures < schedule.max_consecutive_failures:
        return

    schedule.status = "paused"
    schedule.paused_reason = (
        f"auto-paused after {schedule.consecutive_failures} consecutive failures"
    )
    record_event(
        session,
        "schedule.paused",
        {"schedule_id": schedule.id, "reason": schedule.paused_reason},
        run_id=outcome.run_id,
    )
