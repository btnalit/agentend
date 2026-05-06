from dataclasses import dataclass
from pathlib import Path


DEFAULT_CONFIG = """[llm]
provider = "fake"
model = "fake-llm"
temperature = 0.2
max_tokens = 4096

[llm.providers.fake]
api_key_env = ""
base_url = ""

[llm.providers.openai]
api_key_env = "OPENAI_API_KEY"
base_url = "https://api.openai.com/v1"

[telegram]
enabled = false
bot_token_env = "TELEGRAM_BOT_TOKEN"

[search]
provider = "fake"

[search.providers.fake]
api_key_env = ""
base_url = ""

[search.providers.brave]
api_key_env = "BRAVE_SEARCH_API_KEY"
base_url = "https://api.search.brave.com/res/v1/web/search"

[vision]
provider = "fake"

[vision.providers.fake]
api_key_env = ""
base_url = ""
model = "fake-vision"

[vision.providers.openai]
api_key_env = "OPENAI_API_KEY"
base_url = "https://api.openai.com/v1"
model = "gpt-4.1"

[vision.providers.gemini]
api_key_env = "GEMINI_API_KEY"
base_url = "https://generativelanguage.googleapis.com/v1beta"
model = "gemini-2.5-flash"

[data]
db_path = "./data/agentend.sqlite"
artifact_dir = "./data/artifacts"
log_dir = "./data/logs"
agent_profile_path = "./agent.md"
workflow_dir = "./workflows/definitions"
"""


DEFAULT_ENV_EXAMPLE = """OPENAI_API_KEY=
TELEGRAM_BOT_TOKEN=
BRAVE_SEARCH_API_KEY=
GEMINI_API_KEY=
"""


DEFAULT_AGENT_PROFILE = """# Agent Profile

You are a local single-agent workflow runtime.

## Working Style
- Reply in Chinese by default.
- Prefer existing workflows.
- Ask for missing information before running an ambiguous workflow.
- Briefly explain tool usage before taking action.

## Capabilities
- You can call registered built-in tools.
- You can call registered MCP tools.
- You can read and write local SQLite memory.
- You can create local artifact files.

## Output Preferences
- Lead with the conclusion.
- Provide executable commands or next steps.
- Do not expose internal chain-of-thought.
"""


DEFAULT_WORKFLOW = """id: simple_chat
name: Simple Chat
description: Minimal LLM-to-final workflow.
nodes:
  - id: answer
    type: llm
    prompt: "{input}"
  - id: final
    type: final
    depends_on: [answer]
"""


@dataclass(frozen=True)
class InitResult:
    home: Path


def initialize_home(home: Path, force: bool = False) -> InitResult:
    resolved = home.expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True)

    (resolved / "data" / "artifacts").mkdir(parents=True, exist_ok=True)
    (resolved / "data" / "logs").mkdir(parents=True, exist_ok=True)
    workflow_dir = resolved / "workflows" / "definitions"
    workflow_dir.mkdir(parents=True, exist_ok=True)

    _write_template(resolved / "config.toml", DEFAULT_CONFIG, force=force)
    _write_template(resolved / ".env.example", DEFAULT_ENV_EXAMPLE, force=force)
    _write_template(resolved / "agent.md", DEFAULT_AGENT_PROFILE, force=force)
    _write_template(workflow_dir / "simple_chat.yaml", DEFAULT_WORKFLOW, force=force)

    from agentend.db.session import init_database

    init_database(resolved)

    return InitResult(home=resolved)


def _write_template(path: Path, content: str, force: bool) -> None:
    if path.exists() and not force:
        return
    path.write_text(content, encoding="utf-8")
