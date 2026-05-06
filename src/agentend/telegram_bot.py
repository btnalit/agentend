from __future__ import annotations

import json
import os
from pathlib import Path

from sqlalchemy import select

from agentend.config import load_config
from agentend.core.conversation import ConversationService
from agentend.core.events import record_event
from agentend.core.profile import load_agent_profile
from agentend.core.secrets import redact_text
from agentend.core.workflow_registry import WorkflowRegistry
from agentend.core.workflow_runner import WorkflowRunner
from agentend.db.models import ClarificationRequest, Conversation, Run
from agentend.db.session import init_database
from agentend.db.session import session_scope


class TelegramMessageRouter:
    def __init__(self, home: Path):
        self.home = home.expanduser().resolve()
        init_database(self.home)

    def handle_text(self, chat_id: str, user_id: str, text: str) -> str:
        stripped = text.strip()
        external_user_id = _telegram_external_user_id(chat_id, user_id)
        if stripped == "/start":
            return "AgentEnd Lite is ready. Send a message or use /workflows."
        if stripped == "/help":
            return "/start /new /workflows /run <workflow_id> <input> /status /cancel /agent"
        if stripped == "/new":
            return "New Telegram conversation is ready."
        if stripped == "/workflows":
            workflows, errors = WorkflowRegistry(load_config(self.home)).list_workflows()
            lines = [workflow.id for workflow in workflows]
            lines.extend(f"ERROR {error.path.name}: {error.message}" for error in errors)
            return "\n".join(lines) if lines else "No workflows."
        if stripped.startswith("/run"):
            parts = stripped.split(maxsplit=2)
            if len(parts) < 2:
                return "Usage: /run <workflow_id> <input>"
            workflow_id = parts[1]
            input_text = parts[2] if len(parts) > 2 else ""
            workflow = WorkflowRegistry(load_config(self.home)).get(workflow_id)
            result = WorkflowRunner(self.home).run(
                workflow,
                input_text,
                channel="telegram",
                external_user_id=external_user_id,
            )
            return self._run_reply(result.run_id, result.output)
        if stripped == "/status":
            with session_scope(self.home) as session:
                run = (
                    session.execute(
                        select(Run)
                        .join(Conversation, Run.conversation_id == Conversation.id)
                        .where(Conversation.channel == "telegram")
                        .where(Conversation.external_user_id == external_user_id)
                        .order_by(Run.created_at.desc())
                    )
                    .scalars()
                    .first()
                )
                if run is None:
                    return "No runs."
                return f"Run: {run.id}\nStatus: {run.status}"
        if stripped == "/cancel":
            return self._cancel_pending_run(external_user_id)
        if stripped == "/agent":
            profile = load_agent_profile(load_config(self.home))
            return f"Agent profile hash: {profile.digest}"

        pending_run_id = self._pending_telegram_run_id(external_user_id)
        if pending_run_id is not None:
            result = WorkflowRunner(self.home).resume(
                pending_run_id,
                answer=text,
                expected_channel="telegram",
                expected_external_user_id=external_user_id,
            )
            return self._run_reply(result.run_id, result.output)

        response = ConversationService(self.home).handle_message(
            channel="telegram",
            external_user_id=external_user_id,
            text=text,
        )
        return self._run_reply(response.run_id, response.content, omit_raw_tool_output=False)

    def _pending_telegram_run_id(self, external_user_id: str) -> str | None:
        with session_scope(self.home) as session:
            run = (
                session.execute(
                    select(Run)
                    .join(Conversation, Run.conversation_id == Conversation.id)
                    .join(ClarificationRequest, ClarificationRequest.run_id == Run.id)
                    .where(Conversation.channel == "telegram")
                    .where(Conversation.external_user_id == external_user_id)
                    .where(Run.status == "waiting_input")
                    .where(ClarificationRequest.status == "pending")
                    .order_by(Run.created_at.desc())
                )
                .scalars()
                .first()
            )
            return run.id if run is not None else None

    def _cancel_pending_run(self, external_user_id: str) -> str:
        with session_scope(self.home) as session:
            row = (
                session.execute(
                    select(Run, ClarificationRequest)
                    .join(Conversation, Run.conversation_id == Conversation.id)
                    .join(ClarificationRequest, ClarificationRequest.run_id == Run.id)
                    .where(Conversation.channel == "telegram")
                    .where(Conversation.external_user_id == external_user_id)
                    .where(Run.status == "waiting_input")
                    .where(ClarificationRequest.status == "pending")
                    .order_by(Run.created_at.desc())
                )
                .first()
            )
            if row is None:
                return "No active run to cancel."
            run, request = row
            run.status = "cancelled"
            request.status = "cancelled"
            record_event(session, "run.cancelled", {"source": "telegram"}, run_id=run.id)
            return f"Run: {run.id}\nStatus: cancelled"

    def _run_reply(self, run_id: str, output: str, *, omit_raw_tool_output: bool = True) -> str:
        if omit_raw_tool_output and _looks_like_raw_tool_output(output):
            safe_output = "Output omitted from Telegram. Inspect the run locally."
        else:
            safe_output = output
        return f"Run: {run_id}\n{self._safe_text(safe_output)}"

    def _safe_text(self, text: str) -> str:
        redacted = redact_text(self.home, text)
        redacted = redacted.replace(str(self.home), "[AGENTEND_HOME]")
        redacted = redacted.replace(str(self.home).replace("\\", "/"), "[AGENTEND_HOME]")
        return redacted


def _telegram_external_user_id(chat_id: str, user_id: str) -> str:
    return f"{chat_id}:{user_id}"


def _looks_like_raw_tool_output(text: str) -> bool:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    raw_keys = {
        "artifacts",
        "cwd",
        "deleted",
        "dst",
        "entries",
        "exit_code",
        "is_dir",
        "matches",
        "path",
        "sha256",
        "size_bytes",
        "src",
        "stderr",
        "stdout",
        "workspace",
    }
    return bool(raw_keys.intersection(payload))


def serve_telegram(home: Path) -> None:
    load_config(home)
    token_env = "TELEGRAM_BOT_TOKEN"
    token = os.environ.get(token_env)
    if not token:
        raise RuntimeError(f"{token_env} is not set")

    from telegram import Update
    from telegram.ext import Application, ContextTypes, MessageHandler, filters

    router = TelegramMessageRouter(home)

    async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is None or update.effective_user is None or update.message is None:
            return
        reply = router.handle_text(
            chat_id=str(update.effective_chat.id),
            user_id=str(update.effective_user.id),
            text=update.message.text or "",
        )
        for chunk in _split_telegram_message(reply):
            await update.message.reply_text(chunk)

    app = Application.builder().token(token).build()
    app.add_handler(MessageHandler(filters.TEXT, handle_message))
    app.run_polling()


def _split_telegram_message(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    return [text[index : index + limit] for index in range(0, len(text), limit)]
