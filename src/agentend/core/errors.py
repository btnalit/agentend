from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy.orm import Session

from agentend.db.models import ErrorRecord


@dataclass(frozen=True)
class ClassifiedError:
    code: str
    message: str
    retryable: bool
    suggested_action: str


def classify_exception(exc: Exception) -> ClassifiedError:
    message = str(exc)
    lowered = message.lower()
    if "unknown tool" in lowered:
        return ClassifiedError("tool_not_found", message, False, "Run tools.discover or check the tool name.")
    if "budget_exceeded" in lowered or "budget exceeded" in lowered:
        return ClassifiedError("budget_exceeded", message, False, "Raise the workflow budget or reduce the context/output size.")
    if "is not set" in lowered or "not configured" in lowered or "missing" in lowered:
        return ClassifiedError("missing_config", message, False, "Configure the missing setting or secret.")
    if "timeout" in lowered or isinstance(exc, TimeoutError):
        return ClassifiedError("timeout", message, True, "Retry or increase the timeout.")
    if "status 401" in lowered or "status 403" in lowered or "unauthorized" in lowered or "forbidden" in lowered:
        return ClassifiedError("permission_error", message, False, "Check provider credentials and permissions.")
    if "status 5" in lowered or "network error" in lowered or "provider request failed" in lowered:
        return ClassifiedError("network_error", message, True, "Retry or switch provider.")
    if isinstance(exc, PermissionError) or "blocked" in lowered or "permission" in lowered:
        return ClassifiedError("permission_error", message, False, "Adjust the action policy or request user input.")
    if isinstance(exc, (KeyError, TypeError, ValueError)) and "unknown tool" not in lowered:
        return ClassifiedError("schema_error", message, False, "Check the tool input schema.")
    if "network" in lowered or "connection" in lowered:
        return ClassifiedError("network_error", message, True, "Retry or switch provider.")
    return ClassifiedError("unknown", message, False, "Inspect the run log.")


def record_error(
    session: Session,
    exc: Exception,
    *,
    source: str,
    run_id: str | None = None,
    step_id: str | None = None,
) -> ClassifiedError:
    classified = classify_exception(exc)
    session.add(
        ErrorRecord(
            id=str(uuid4()),
            run_id=run_id,
            step_id=step_id,
            source=source,
            error_code=classified.code,
            message=classified.message,
            retryable="true" if classified.retryable else "false",
            suggested_action=classified.suggested_action,
        )
    )
    return classified
