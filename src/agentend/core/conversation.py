import json
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from agentend.core.agent_run import AgentRunController
from agentend.config import load_config
from agentend.core.events import record_event
from agentend.core.goal_analyzer import analyze_goal
from agentend.core.intent_audit import record_intent_decision
from agentend.core.workflow_registry import WorkflowRegistry
from agentend.core.workflow_runner import WorkflowRunner
from agentend.db.models import Conversation, Message, Run
from agentend.db.session import init_database, session_scope


@dataclass(frozen=True)
class ConversationResponse:
    conversation_id: str
    run_id: str | None
    content: str
    route_type: str = "workflow"
    agent_run_id: str | None = None
    status: str | None = None


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

        intent = goal_analysis.get("intent_decision", {})
        routed_intents = {"task", "tool_action", "workflow_action", "skill_action", "clarification", "blocked"}
        if isinstance(intent, dict) and intent.get("intent_type") in routed_intents:
            result = AgentRunController(self.home).run(
                text,
                channel=channel,
                external_user_id=external_user_id,
                conversation_id=conversation_id,
            )
            with session_scope(self.home) as session:
                intent_record = record_intent_decision(
                    self.home,
                    session,
                    text,
                    intent,
                    conversation_id=conversation_id,
                    run_id=result.linked_run_id,
                    agent_run_id=result.agent_run_id,
                    channel=channel,
                    external_user_id=external_user_id,
                    route_type="agent_run",
                    context_summary={"source": "conversation", "route_type": "agent_run"},
                )
                if result.linked_run_id:
                    run = session.get(Run, result.linked_run_id)
                    if run is not None:
                        result_payload = json.loads(run.result_json or "{}")
                        result_payload["goal_analysis"] = goal_analysis
                        result_payload["intent_decision"] = intent
                        result_payload["intent_decision_id"] = intent_record.id
                        run.result_json = json.dumps(result_payload, ensure_ascii=False, sort_keys=True)
                session.add(
                    Message(
                        id=str(uuid4()),
                        conversation_id=conversation_id,
                        role="assistant",
                        content=result.content,
                        metadata_json=json.dumps(
                            {
                                "agent_run_id": result.agent_run_id,
                                "run_id": result.linked_run_id,
                                "route_type": "agent_run",
                                "intent_decision": intent,
                                "intent_decision_id": intent_record.id,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    )
                )
            return ConversationResponse(
                conversation_id=conversation_id,
                run_id=result.linked_run_id,
                content=result.content,
                route_type="agent_run",
                agent_run_id=result.agent_run_id,
                status=result.status,
            )

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
            intent_record = record_intent_decision(
                self.home,
                session,
                text,
                intent,
                conversation_id=conversation_id,
                run_id=result.run_id,
                channel=channel,
                external_user_id=external_user_id,
                route_type="simple_chat",
                context_summary={"source": "conversation", "route_type": "simple_chat"},
            )
            run = session.get(Run, result.run_id)
            if run is not None:
                result_payload = json.loads(run.result_json)
                result_payload["goal_analysis"] = goal_analysis
                result_payload["intent_decision_id"] = intent_record.id
                run.result_json = json.dumps(result_payload, ensure_ascii=False, sort_keys=True)
            session.add(
                Message(
                    id=str(uuid4()),
                    conversation_id=conversation_id,
                    role="assistant",
                    content=response_text,
                    metadata_json=json.dumps(
                        {"run_id": result.run_id, "intent_decision_id": intent_record.id},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                )
            )

            return ConversationResponse(
                conversation_id=conversation_id,
                run_id=result.run_id,
                content=response_text,
            )
