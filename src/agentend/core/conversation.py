import json
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from agentend.config import load_config
from agentend.core.events import record_event
from agentend.core.goal_analyzer import analyze_goal
from agentend.core.workflow_registry import WorkflowRegistry
from agentend.core.workflow_runner import WorkflowRunner
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
        config = load_config(self.home)
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
            goal_analysis = analyze_goal(self.home, session, text)
            conversation_id = conversation.id

        workflow = WorkflowRegistry(config).get("simple_chat")
        result = WorkflowRunner(self.home).run(
            workflow,
            text,
            channel=channel,
            external_user_id=external_user_id,
            conversation_id=conversation_id,
        )
        response_text = result.output
        with session_scope(self.home) as session:
            run = session.get(Run, result.run_id)
            if run is not None:
                result_payload = json.loads(run.result_json)
                result_payload["goal_analysis"] = goal_analysis
                run.result_json = json.dumps(result_payload, ensure_ascii=False, sort_keys=True)
            session.add(
                Message(
                    id=str(uuid4()),
                    conversation_id=conversation_id,
                    role="assistant",
                    content=response_text,
                    metadata_json=json.dumps({"run_id": result.run_id}, ensure_ascii=False, sort_keys=True),
                )
            )

            return ConversationResponse(
                conversation_id=conversation_id,
                run_id=result.run_id,
                content=response_text,
            )
