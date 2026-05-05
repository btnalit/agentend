import json
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from agentend.core.events import record_event
from agentend.db.models import Conversation, Message, Run
from agentend.db.session import init_database, session_scope


@dataclass(frozen=True)
class ConversationResponse:
    conversation_id: str
    run_id: str
    content: str


class ConversationService:
    def __init__(self, home: Path):
        self.home = home.expanduser().resolve()
        init_database(self.home)

    def handle_message(self, channel: str, external_user_id: str, text: str) -> ConversationResponse:
        with session_scope(self.home) as session:
            conversation = session.execute(
                select(Conversation).where(
                    Conversation.channel == channel,
                    Conversation.external_user_id == external_user_id,
                )
            ).scalar_one_or_none()
            if conversation is None:
                conversation = Conversation(
                    id=str(uuid4()),
                    channel=channel,
                    external_user_id=external_user_id,
                    title=text[:80],
                )
                session.add(conversation)
                record_event(
                    session,
                    "conversation.created",
                    {"channel": channel, "external_user_id": external_user_id},
                )

            user_message = Message(
                id=str(uuid4()),
                conversation_id=conversation.id,
                role="user",
                content=text,
            )
            session.add(user_message)
            record_event(session, "message.received", {"conversation_id": conversation.id})

            run = Run(
                id=str(uuid4()),
                conversation_id=conversation.id,
                workflow_id=None,
                status="completed",
                input_json=json.dumps({"message": text}, ensure_ascii=False),
                result_json=json.dumps({"content": f"Echo: {text}"}, ensure_ascii=False),
            )
            session.add(run)
            record_event(session, "run.created", {"conversation_id": conversation.id}, run_id=run.id)
            record_event(session, "run.state_changed", {"status": "completed"}, run_id=run.id)

            response_text = f"Echo: {text}"
            session.add(
                Message(
                    id=str(uuid4()),
                    conversation_id=conversation.id,
                    role="assistant",
                    content=response_text,
                )
            )
            record_event(session, "run.completed", {"conversation_id": conversation.id}, run_id=run.id)

            return ConversationResponse(
                conversation_id=conversation.id,
                run_id=run.id,
                content=response_text,
            )
