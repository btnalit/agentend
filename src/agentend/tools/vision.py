from __future__ import annotations

import base64
import json
import os
import re
from hashlib import sha256
from pathlib import Path

import httpx

from agentend.config import VisionProviderConfig, load_config
from agentend.tools.base import ToolContext, ToolResult


class VisionDescribeTool:
    name = "vision.describe"
    description = "Describe an image through a vision provider; fake provider returns image metadata."
    input_schema = {"type": "object", "required": ["path"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        path = _resolve(context.home, input_data["path"])
        metadata = _image_metadata(path)
        provider_name, provider = _provider_from_input(context.home, input_data)
        if provider_name == "fake":
            data = metadata | {"provider": "fake", "model": provider.model, "description": f"fake vision description for {path.name}"}
        else:
            text = _call_real_provider(
                provider_name,
                provider,
                path,
                prompt=str(input_data.get("prompt", "Describe this image in concise, factual terms.")),
                max_tokens=int(input_data.get("max_tokens", 512)),
            )
            data = metadata | {"provider": provider_name, "model": provider.model, "description": text}
        return ToolResult(content=json.dumps(data, ensure_ascii=False, sort_keys=True), data=data)


class VisionOcrTool:
    name = "vision.ocr"
    description = "Extract OCR text from an image; fake provider returns a stable placeholder."
    input_schema = {"type": "object", "required": ["path"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        path = _resolve(context.home, input_data["path"])
        metadata = _image_metadata(path)
        provider_name, provider = _provider_from_input(context.home, input_data)
        if provider_name == "fake":
            data = metadata | {"ocr_text": "", "provider": "fake", "model": provider.model}
        else:
            text = _call_real_provider(
                provider_name,
                provider,
                path,
                prompt=str(input_data.get("prompt", "Extract all visible text from this image. Return only the text when possible.")),
                max_tokens=int(input_data.get("max_tokens", 1024)),
            )
            data = metadata | {"ocr_text": text, "provider": provider_name, "model": provider.model}
        return ToolResult(content=json.dumps(data, ensure_ascii=False, sort_keys=True), data=data)


class VisionExtractChartTool:
    name = "vision.extract_chart"
    description = "Extract chart structure from an image; fake provider returns an empty series skeleton."
    input_schema = {"type": "object", "required": ["path"]}

    def call(self, input_data: dict, context: ToolContext) -> ToolResult:
        path = _resolve(context.home, input_data["path"])
        metadata = _image_metadata(path)
        provider_name, provider = _provider_from_input(context.home, input_data)
        if provider_name == "fake":
            data = metadata | {"provider": "fake", "model": provider.model, "chart_type": "unknown", "series": []}
        else:
            text = _call_real_provider(
                provider_name,
                provider,
                path,
                prompt=str(
                    input_data.get(
                        "prompt",
                        "Extract chart structure from this image. Return JSON with chart_type and series fields when possible.",
                    )
                ),
                max_tokens=int(input_data.get("max_tokens", 1024)),
            )
            data = metadata | {"provider": provider_name, "model": provider.model, "raw_text": text} | _parse_chart_text(text)
        return ToolResult(content=json.dumps(data, ensure_ascii=False, sort_keys=True), data=data)


def _resolve(home: Path, value: object) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else (home / path).resolve()


def _image_metadata(path: Path) -> dict:
    if not path.exists():
        raise ValueError(f"Image not found: {path}")
    data = path.read_bytes()
    return {
        "path": str(path),
        "file_name": path.name,
        "size_bytes": len(data),
        "sha256": sha256(data).hexdigest(),
        "mime": _mime_from_suffix(path),
    }


def _provider_from_input(home: Path, input_data: dict) -> tuple[str, VisionProviderConfig]:
    config = load_config(home)
    provider_name = _normalize_provider(str(input_data.get("provider") or config.vision.provider))
    configured = config.vision.providers.get(provider_name)
    if configured is None:
        raise ValueError(f"Unknown vision provider: {provider_name}")
    provider = VisionProviderConfig(
        api_key_env=str(input_data.get("api_key_env") or configured.api_key_env),
        base_url=str(input_data.get("base_url") or configured.base_url),
        model=str(input_data.get("model") or configured.model),
    )
    if provider_name != "fake" and not os.environ.get(provider.api_key_env):
        raise RuntimeError(f"Vision provider secret is not set: {provider.api_key_env}")
    return provider_name, provider


def _normalize_provider(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    if normalized in {"openai-compatible", "openai-compatible-chat", "openai-chat"}:
        return "openai"
    return normalized


def _call_real_provider(provider_name: str, provider: VisionProviderConfig, path: Path, *, prompt: str, max_tokens: int) -> str:
    if provider_name == "openai":
        return _call_openai_compatible(provider, path, prompt=prompt, max_tokens=max_tokens)
    if provider_name == "gemini":
        return _call_gemini(provider, path, prompt=prompt, max_tokens=max_tokens)
    raise ValueError(f"Unsupported real vision provider: {provider_name}")


def _call_openai_compatible(provider: VisionProviderConfig, path: Path, *, prompt: str, max_tokens: int) -> str:
    url = _openai_chat_url(provider.base_url)
    api_key = os.environ[provider.api_key_env]
    mime = _mime_from_suffix(path)
    payload = {
        "model": provider.model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{_base64(path)}"}},
                ],
            }
        ],
        "max_tokens": max_tokens,
    }
    response = httpx.post(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    return _extract_openai_text(response.json())


def _openai_chat_url(base_url: str) -> str:
    cleaned = base_url.rstrip("/")
    if cleaned.endswith("/chat/completions"):
        return cleaned
    return f"{cleaned}/chat/completions"


def _extract_openai_text(payload: dict) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "\n".join(part for part in parts if part)
    return str(content)


def _call_gemini(provider: VisionProviderConfig, path: Path, *, prompt: str, max_tokens: int) -> str:
    url = _gemini_generate_url(provider.base_url, provider.model)
    api_key = os.environ[provider.api_key_env]
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": _mime_from_suffix(path), "data": _base64(path)}},
                ],
            }
        ],
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    response = httpx.post(
        url,
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    return _extract_gemini_text(response.json())


def _gemini_generate_url(base_url: str, model: str) -> str:
    cleaned = base_url.rstrip("/")
    if cleaned.endswith(":generateContent"):
        return cleaned
    return f"{cleaned}/models/{model}:generateContent"


def _extract_gemini_text(payload: dict) -> str:
    parts: list[str] = []
    for candidate in payload.get("candidates") or []:
        content = candidate.get("content") or {}
        for part in content.get("parts") or []:
            if isinstance(part, dict) and "text" in part:
                parts.append(str(part["text"]))
    return "\n".join(part for part in parts if part)


def _base64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _parse_chart_text(text: str) -> dict:
    candidates = [text]
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        candidates.insert(0, match.group(0))
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        return {
            "chart_type": str(payload.get("chart_type", "unknown")),
            "series": payload.get("series", []),
        }
    return {"chart_type": "unknown", "series": []}


def _mime_from_suffix(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".png":
        return "image/png"
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".gif":
        return "image/gif"
    return "application/octet-stream"


VISION_TOOLS = [VisionDescribeTool(), VisionOcrTool(), VisionExtractChartTool()]
