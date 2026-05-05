from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import select

from agentend.config import load_config
from agentend.core.conversation import ConversationService
from agentend.core.profile import load_agent_profile
from agentend.core.workflow_registry import WorkflowRegistry
from agentend.core.workflow_runner import WorkflowRunner
from agentend.db.models import Run
from agentend.db.session import session_scope


class TelegramMessageRouter:
    def __init__(self, home: Path):
        self.home = home.expanduser().resolve()

    def handle_text(self, chat_id: str, user_id: str, text: str) -> str:
        stripped = text.strip()
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
            result = WorkflowRunner(self.home).run(workflow, input_text, channel="telegram")
            return f"Run: {result.run_id}\n{result.output}"
        if stripped == "/status":
            with session_scope(self.home) as session:
                run = session.execute(select(Run).order_by(Run.created_at.desc())).scalars().first()
                if run is None:
                    return "No runs."
                return f"Run: {run.id}\nStatus: {run.status}"
        if stripped == "/cancel":
            return "No active run to cancel."
        if stripped == "/agent":
            profile = load_agent_profile(load_config(self.home))
            return f"Agent profile: {profile.path}\nHash: {profile.digest}"

        response = ConversationService(self.home).handle_message(
            channel="telegram",
            external_user_id=f"{chat_id}:{user_id}",
            text=text,
        )
        return f"Run: {response.run_id}\n{response.content}"


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
