from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

from agentend.config import AppConfig
from agentend.core.secrets import redact_text


@dataclass(frozen=True)
class LLMTestResult:
    ok: bool
    message: str


@dataclass(frozen=True)
class LLMUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class LLMResponse:
    content: str
    provider: str
    model: str
    usage: LLMUsage


class LLMRouter:
    def __init__(self, config: AppConfig):
        self.config = config

    def test(self) -> LLMTestResult:
        if self.config.llm.provider == "fake":
            return LLMTestResult(ok=True, message="fake provider configured")

        api_key_env = self.config.llm.provider_config.api_key_env
        if not os.environ.get(api_key_env):
            return LLMTestResult(ok=False, message=f"LLM provider secret is not set: {api_key_env}")
        try:
            response = self.complete_response("ping")
        except Exception as exc:
            return LLMTestResult(ok=False, message=redact_text(self.config.home, str(exc)))
        return LLMTestResult(
            ok=True,
            message=(
                f"{response.provider}/{response.model} responded "
                f"(input_tokens={response.usage.input_tokens}, output_tokens={response.usage.output_tokens})"
            ),
        )

    def complete(self, prompt: str) -> str:
        return self.complete_response(prompt).content

    def complete_response(self, prompt: str) -> LLMResponse:
        if self.config.llm.provider == "fake":
            content = f"Fake LLM: {prompt}"
            return LLMResponse(
                content=content,
                provider="fake",
                model=self.config.llm.model,
                usage=LLMUsage(
                    input_tokens=_estimate_tokens(prompt),
                    output_tokens=_estimate_tokens(content),
                    total_tokens=_estimate_tokens(prompt) + _estimate_tokens(content),
                ),
            )

        api_key_env = self.config.llm.provider_config.api_key_env
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise RuntimeError(f"LLM provider secret is not set: {api_key_env}")

        base_url = self.config.llm.provider_config.base_url.rstrip("/")
        if not base_url:
            raise RuntimeError(f"LLM provider base_url is not configured: {self.config.llm.provider}")
        url = f"{base_url}/chat/completions"
        payload = {
            "model": self.config.llm.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.config.llm.temperature,
            "max_tokens": self.config.llm.max_tokens,
        }
        try:
            response = httpx.post(
                url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:500]
            raise RuntimeError(
                f"LLM provider request failed with status {exc.response.status_code}: {body}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise TimeoutError("LLM provider request timed out") from exc
        except httpx.RequestError as exc:
            raise RuntimeError(f"LLM provider network error: {exc}") from exc
        except ValueError as exc:
            raise RuntimeError("LLM provider returned invalid JSON") from exc

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("LLM provider response is missing choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if content is None:
            raise RuntimeError("LLM provider response is missing message content")

        usage_payload = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        input_tokens = int(usage_payload.get("prompt_tokens") or _estimate_tokens(prompt))
        output_tokens = int(usage_payload.get("completion_tokens") or _estimate_tokens(str(content)))
        total_tokens = int(usage_payload.get("total_tokens") or input_tokens + output_tokens)
        return LLMResponse(
            content=str(content),
            provider=self.config.llm.provider,
            model=str(data.get("model") or self.config.llm.model),
            usage=LLMUsage(input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens),
        )


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)
