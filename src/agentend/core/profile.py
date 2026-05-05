from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from agentend.config import AppConfig


@dataclass(frozen=True)
class AgentProfile:
    path: Path
    content: str
    digest: str


def load_agent_profile(config: AppConfig) -> AgentProfile:
    path = config.resolve_home_path(config.data.agent_profile_path)
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    digest = sha256(path.read_bytes() if path.exists() else b"").hexdigest()
    return AgentProfile(path=path, content=content, digest=digest)
