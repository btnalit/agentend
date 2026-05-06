from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

from agentend.tools.base import ToolContext, ToolResult


class TelegramSendMessageTool:
    name = "im.telegram.send_message"
    description = "Send a Telegram message, or validate the payload with dry_run."
    input_schema = {"type": "object", "required": ["chat_id", "text"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        chat_id = str(input_data["chat_id"])
        text = str(input_data["text"])
        dry_run = bool(input_data.get("dry_run", False))
        payload = {"chat_id": chat_id, "text": text, "dry_run": dry_run}
        if not dry_run:
            token = os.environ.get("TELEGRAM_BOT_TOKEN")
            if not token:
                raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
            response = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=30)
            payload["status_code"] = response.status_code
            payload["ok"] = response.is_success
        return ToolResult(content=json.dumps(payload, ensure_ascii=False, sort_keys=True), data=payload)


class TelegramSendFileTool:
    name = "im.telegram.send_file"
    description = "Send a Telegram file, or validate the payload with dry_run."
    input_schema = {"type": "object", "required": ["chat_id", "path"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        chat_id = str(input_data["chat_id"])
        path = _resolve(context.home, input_data["path"])
        if not path.exists():
            raise ValueError(f"File not found: {path}")
        caption = str(input_data.get("caption", ""))
        dry_run = bool(input_data.get("dry_run", False))
        payload = {"chat_id": chat_id, "path": str(path), "file_name": path.name, "caption": caption, "dry_run": dry_run}
        if not dry_run:
            token = os.environ.get("TELEGRAM_BOT_TOKEN")
            if not token:
                raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
            with path.open("rb") as handle:
                response = httpx.post(
                    f"https://api.telegram.org/bot{token}/sendDocument",
                    data={"chat_id": chat_id, "caption": caption},
                    files={"document": (path.name, handle)},
                    timeout=60,
                )
            payload["status_code"] = response.status_code
            payload["ok"] = response.is_success
        return ToolResult(content=json.dumps(payload, ensure_ascii=False, sort_keys=True), data=payload)


def _resolve(home: Path, value: object) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else (home / path).resolve()


IM_TOOLS = [TelegramSendMessageTool(), TelegramSendFileTool()]
