from __future__ import annotations

import json
import os
from pathlib import Path

from sqlalchemy import desc, select

from agentend.config import load_config
from agentend.core.clarifications import answer_clarification, pending_clarification_for_run
from agentend.core.conversation import ConversationService
from agentend.core.events import record_event
from agentend.core.profile import load_agent_profile
from agentend.core.secrets import redact_text
from agentend.core.tasks import TaskManager
from agentend.core.workflow_registry import WorkflowRegistry
from agentend.core.workflow_runner import WorkflowRunFailed, WorkflowRunner
from agentend.db.models import AgentIteration, AgentRun, ClarificationRequest, Conversation, Run
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
            try:
                workflow = WorkflowRegistry(load_config(self.home)).get(workflow_id)
                result = WorkflowRunner(self.home).run(
                    workflow,
                    input_text,
                    channel="telegram",
                    external_user_id=external_user_id,
                )
                return self._run_reply(result.run_id, result.output)
            except WorkflowRunFailed as exc:
                return self._error_reply(exc.message, run_id=exc.run_id)
            except Exception as exc:
                return self._error_reply(str(exc))
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
            try:
                result = WorkflowRunner(self.home).resume(
                    pending_run_id,
                    answer=text,
                    expected_channel="telegram",
                    expected_external_user_id=external_user_id,
                )
                return self._run_reply(result.run_id, result.output)
            except WorkflowRunFailed as exc:
                return self._error_reply(exc.message, run_id=exc.run_id)
            except Exception as exc:
                return self._error_reply(str(exc), run_id=pending_run_id)

        response = ConversationService(self.home).handle_message(
            channel="telegram",
            external_user_id=external_user_id,
            text=text,
        )
        return self._run_reply(response.run_id, response.content)

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

    def _error_reply(self, message: str, *, run_id: str | None = None) -> str:
        prefix = f"Run: {run_id}\n" if run_id else ""
        return f"{prefix}Error: {self._safe_text(message)}"

    def _safe_text(self, text: str) -> str:
        redacted = redact_text(self.home, text)
        redacted = redacted.replace(str(self.home), "[AGENTEND_HOME]")
        redacted = redacted.replace(str(self.home).replace("\\", "/"), "[AGENTEND_HOME]")
        return redacted


    def render_goal_view(self, agent_run_id: str) -> str:
        with session_scope(self.home) as session:
            run = session.get(AgentRun, agent_run_id)
            if run is None:
                return f"目标 {agent_run_id[:8]} 不存在。"

            lines = [
                f"🎯 目标：{run.goal}",
                f"状态：{run.status}",
                f"ID：{run_id_short(agent_run_id)}",
            ]

            if run.status == "waiting_input":
                # 通过 linked_run_id 中转查询 pending clarification（D8）
                iteration = session.execute(
                    select(AgentIteration)
                    .where(AgentIteration.agent_run_id == agent_run_id)
                    .where(AgentIteration.linked_run_id.is_not(None))
                    .order_by(desc(AgentIteration.created_at))
                ).scalars().first()

                if iteration and iteration.linked_run_id:
                    clarification = pending_clarification_for_run(session, iteration.linked_run_id)
                    if clarification:
                        lines.append(f"\n⏸ 等待审批：{clarification.question}")
                        lines.append("👆 请使用下方按钮审批")

            return "\n".join(lines)

    def approve_clarification(self, request_id: str, agent_run_id: str) -> str:
        with session_scope(self.home) as session:
            clarification = session.get(ClarificationRequest, request_id)
            if clarification is None or clarification.status != "pending":
                return "审批请求已过期或已处理。"
            try:
                answer_clarification(session, clarification, "approve")
            except ValueError as exc:
                return f"审批失败：{exc}"
            # Enqueue inside the same session → atomic with answer_clarification commit
            TaskManager(self.home).enqueue_resume_intent(
                agent_run_id, run_mode="normal", answer_text="approve", session=session
            )
        return f"✅ 已批准，目标 {agent_run_id[:8]} 继续运行。"

    def reject_clarification(self, request_id: str, agent_run_id: str) -> str:
        with session_scope(self.home) as session:
            clarification = session.get(ClarificationRequest, request_id)
            if clarification is None or clarification.status != "pending":
                return "审批请求已过期或已处理。"
            try:
                answer_clarification(session, clarification, "reject")
            except ValueError as exc:
                return f"拒绝失败：{exc}"
            agent_run = session.get(AgentRun, agent_run_id)
            if agent_run:
                agent_run.status = "cancelled"
                agent_run.stop_reason = "user_rejected"
                from agentend.db.models import utc_now
                agent_run.completed_at = utc_now()
        return f"❌ 已拒绝，目标 {agent_run_id[:8]} 已取消。"


def run_id_short(run_id: str) -> str:
    return run_id[:8] + "…"


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
    if raw_keys.intersection(payload):
        return True
    # Parallel-node results aggregate per-node outputs as JSON-encoded strings;
    # check one level deep so skill results are also caught.
    for value in payload.values():
        if isinstance(value, str):
            try:
                nested = json.loads(value)
                if isinstance(nested, dict) and raw_keys.intersection(nested):
                    return True
            except json.JSONDecodeError:
                pass
    return False


def _pending_goal_keyboard(home: Path, external_user_id: str):
    """Return (goal_text, InlineKeyboardMarkup) if user has a pending approval, else None."""
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        with session_scope(home) as session:
            # Find latest waiting_input AgentRun for this user
            agent_run = session.execute(
                select(AgentRun)
                .where(AgentRun.external_user_id == external_user_id)
                .where(AgentRun.status == "waiting_input")
                .order_by(desc(AgentRun.created_at))
            ).scalars().first()
            if agent_run is None:
                return None
            # D8: lookup via AgentIteration.linked_run_id
            iteration = session.execute(
                select(AgentIteration)
                .where(AgentIteration.agent_run_id == agent_run.id)
                .where(AgentIteration.linked_run_id.is_not(None))
                .order_by(desc(AgentIteration.created_at))
            ).scalars().first()
            if iteration is None or iteration.linked_run_id is None:
                return None
            clarification = pending_clarification_for_run(session, iteration.linked_run_id)
            if clarification is None:
                return None
            router = TelegramMessageRouter(home)
            text = router.render_goal_view(agent_run.id)
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ 批准", callback_data=f"approve:{clarification.id}"),
                InlineKeyboardButton("❌ 拒绝", callback_data=f"reject:{clarification.id}"),
            ]])
            return text, keyboard
    except Exception:
        return None


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
        tg_user_id = str(update.effective_user.id)
        chat_id_str = str(update.effective_chat.id)
        reply = router.handle_text(
            chat_id=chat_id_str,
            user_id=tg_user_id,
            text=update.message.text or "",
        )
        for chunk in _split_telegram_message(reply):
            await update.message.reply_text(chunk)

        # After normal reply, show pending approval if any (with buttons).
        # Bug 4 fix: skip keyboard if reply already contains a goal view (avoids stacked keyboards)
        if "🎯" not in reply and "⏸" not in reply:
            pending = _pending_goal_keyboard(home, _telegram_external_user_id(chat_id_str, tg_user_id))
            if pending:
                goal_text, keyboard = pending
                await update.message.reply_text(goal_text, reply_markup=keyboard)

    app = Application.builder().token(token).build()
    app.add_handler(MessageHandler(filters.TEXT, handle_message))

    from telegram.ext import CallbackQueryHandler as TgCallbackQueryHandler

    router_ref = router  # 闭包引用

    async def handle_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.data is None:
            return
        await query.answer()
        parts = query.data.split(":", 1)
        if len(parts) != 2 or parts[0] not in ("approve", "reject"):
            return
        action, request_id = parts

        # Bug 2 fix: move all awaits outside the session_scope block
        agent_run_id: str | None = None
        early_exit_msg: str | None = None
        with session_scope(home) as session:
            clarification = session.get(ClarificationRequest, request_id)
            if clarification is None:
                early_exit_msg = "审批请求不存在。"
            else:
                iteration = session.execute(
                    select(AgentIteration)
                    .where(AgentIteration.linked_run_id == clarification.run_id)
                    .order_by(desc(AgentIteration.created_at))
                ).scalars().first()
                if iteration is None:
                    early_exit_msg = "找不到关联目标。"
                else:
                    agent_run_id = iteration.agent_run_id

        if early_exit_msg is not None:
            await query.edit_message_text(early_exit_msg)
            return

        # Bug 1 fix: verify caller owns the run
        if query.from_user is None:
            await query.edit_message_text("无法验证身份。")
            return
        effective_chat_id = (
            str(update.effective_chat.id) if update.effective_chat is not None
            else (str(query.message.chat.id) if query.message is not None else "")
        )
        caller_external_id = _telegram_external_user_id(effective_chat_id, str(query.from_user.id))
        with session_scope(home) as session:
            ar = session.get(AgentRun, agent_run_id)
            if ar is None or ar.external_user_id != caller_external_id:
                await query.edit_message_text("您无权操作此审批。")
                return

        if action == "approve":
            reply = router_ref.approve_clarification(request_id, agent_run_id)
        else:
            reply = router_ref.reject_clarification(request_id, agent_run_id)
        await query.edit_message_text(reply)

    app.add_handler(TgCallbackQueryHandler(handle_approval_callback))
    app.run_polling()


def _split_telegram_message(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    return [text[index : index + limit] for index in range(0, len(text), limit)]
