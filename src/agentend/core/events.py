import json
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from agentend.db.models import EventLog


def record_event(session: Session, event_type: str, payload: dict[str, Any], run_id: str | None = None) -> None:
    session.add(
        EventLog(
            id=str(uuid4()),
            run_id=run_id,
            event_type=event_type,
            payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )
    )
