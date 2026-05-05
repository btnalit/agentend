from __future__ import annotations

import os
from dataclasses import dataclass

from agentend.config import AppConfig


@dataclass(frozen=True)
class LLMTestResult:
    ok: bool
    message: str


class LLMRouter:
    def __init__(self, config: AppConfig):
        self.config = config

    def test(self) -> LLMTestResult:
        if self.config.llm.provider == "fake":
            return LLMTestResult(ok=True, message="fake provider configured")

        api_key_env = self.config.llm.provider_config.api_key_env
        if not os.environ.get(api_key_env):
            return LLMTestResult(ok=False, message=f"{api_key_env} is not set")
        return LLMTestResult(ok=True, message=f"{self.config.llm.provider}/{self.config.llm.model} is configured")

    def complete(self, prompt: str) -> str:
        if self.config.llm.provider == "fake":
            return f"Fake LLM: {prompt}"
        return f"Echo: {prompt}"
