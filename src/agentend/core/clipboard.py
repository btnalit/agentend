from __future__ import annotations

import os
from pathlib import Path


class ClipboardUnavailable(RuntimeError):
    pass


def read_clipboard() -> str:
    file_backend = _file_backend()
    if file_backend is not None:
        if not file_backend.exists():
            return ""
        return file_backend.read_text(encoding="utf-8")

    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        try:
            return str(root.clipboard_get())
        finally:
            root.destroy()
    except Exception as exc:
        raise ClipboardUnavailable(
            "Clipboard backend is unavailable. Set AGENTEND_CLIPBOARD_FILE for a local file backend."
        ) from exc


def write_clipboard(text: str) -> None:
    file_backend = _file_backend()
    if file_backend is not None:
        file_backend.parent.mkdir(parents=True, exist_ok=True)
        file_backend.write_text(text, encoding="utf-8")
        return

    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        try:
            root.clipboard_clear()
            root.clipboard_append(text)
            root.update()
        finally:
            root.destroy()
    except Exception as exc:
        raise ClipboardUnavailable(
            "Clipboard backend is unavailable. Set AGENTEND_CLIPBOARD_FILE for a local file backend."
        ) from exc


def _file_backend() -> Path | None:
    value = os.environ.get("AGENTEND_CLIPBOARD_FILE")
    if not value:
        return None
    return Path(value).expanduser().resolve()
