import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from hashlib import sha256
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import CostUsage, Run
from agentend.db.session import session_scope


def test_llm_config_can_be_set_and_reported(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()

    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    set_result = runner.invoke(
        app,
        ["llm", "set", "--home", str(home), "--provider", "openai", "--model", "gpt-4.1-mini"],
    )
    current = runner.invoke(app, ["llm", "current", "--home", str(home)])

    assert set_result.exit_code == 0
    assert current.exit_code == 0
    assert "openai" in current.output
    assert "gpt-4.1-mini" in current.output


def test_llm_test_reports_missing_api_key(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    assert runner.invoke(
        app,
        ["llm", "set", "--home", str(home), "--provider", "openai", "--model", "gpt-4.1-mini"],
    ).exit_code == 0

    result = runner.invoke(app, ["llm", "test", "--home", str(home)])

    assert result.exit_code == 1
    assert "OPENAI_API_KEY" in result.output


def test_openai_compatible_llm_test_and_workflow_use_real_http_fixture(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    with _LLMFixture() as fixture:
        _configure_openai_llm(home, fixture.base_url)
        monkeypatch.setenv("AGENTEND_TEST_OPENAI_KEY", "secret-value-that-must-not-leak")

        tested = runner.invoke(app, ["llm", "test", "--home", str(home)])
        workflow = runner.invoke(app, ["workflows", "run", "simple_chat", "--home", str(home), "--input", "hello"])

    assert tested.exit_code == 0, tested.output
    assert "openai/fixture-model responded" in tested.output
    assert workflow.exit_code == 0, workflow.output
    assert "fixture: hello" in workflow.output
    assert fixture.requests == ["ping", "hello"]
    with session_scope(home) as session:
        usage = session.execute(select(CostUsage)).scalar_one()
        assert usage.provider == "openai"
        assert usage.model == "fixture-model"
        assert usage.input_tokens == 3
        assert usage.output_tokens == 5
        assert usage.total_tokens == 8
        assert usage.usage_source == "provider"


def test_openai_compatible_llm_accepts_full_chat_completions_endpoint(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0

    with _LLMFixture() as fixture:
        _configure_openai_llm(home, f"{fixture.base_url}/chat/completions")
        monkeypatch.setenv("AGENTEND_TEST_OPENAI_KEY", "secret-value-that-must-not-leak")

        tested = runner.invoke(app, ["llm", "test", "--home", str(home)])

    assert tested.exit_code == 0, tested.output
    assert fixture.requests == ["ping"]


def test_workflow_llm_request_includes_context_pack_items(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    (home / "agent.md").write_text("# Agent Profile\n\nQ3_CONTEXT_MARKER\n", encoding="utf-8")

    with _LLMFixture() as fixture:
        _configure_openai_llm(home, fixture.base_url)
        monkeypatch.setenv("AGENTEND_TEST_OPENAI_KEY", "secret-value-that-must-not-leak")

        workflow = runner.invoke(app, ["workflows", "run", "simple_chat", "--home", str(home), "--input", "context input"])

    assert workflow.exit_code == 0, workflow.output
    messages = fixture.message_batches[0]
    assert any("Q3_CONTEXT_MARKER" in message["content"] for message in messages)
    assert messages[-1] == {"role": "user", "content": "context input"}


def test_workflow_llm_keeps_prompt_when_context_budget_is_tight(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    workflow = home / "workflows" / "definitions" / "tight_context_prompt.yaml"
    workflow.write_text(
        """
id: tight_context_prompt
name: Tight Context Prompt
description: Prompt must remain in the request when context limits are tight.
nodes:
  - id: ask
    type: llm
    context:
      max_items: 2
    prompt: "Budget prompt: {input}"
  - id: final
    type: final
    depends_on: [ask]
""".lstrip(),
        encoding="utf-8",
    )

    with _LLMFixture() as fixture:
        _configure_openai_llm(home, fixture.base_url)
        monkeypatch.setenv("AGENTEND_TEST_OPENAI_KEY", "secret-value-that-must-not-leak")

        workflow_run = runner.invoke(
            app,
            ["workflows", "run", "tight_context_prompt", "--home", str(home), "--input", "tight input"],
        )

    assert workflow_run.exit_code == 0, workflow_run.output
    assert fixture.message_batches[0][-1] == {
        "role": "user",
        "content": "Budget prompt: tight input",
    }


def test_chat_run_records_agent_profile_hash_and_llm_config(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    runner = CliRunner()

    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    assert runner.invoke(
        app,
        ["llm", "set", "--home", str(home), "--provider", "fake", "--model", "fake-model"],
    ).exit_code == 0

    profile = home / "agent.md"
    profile.write_text("# Custom Agent\n\nReply briefly.\n", encoding="utf-8")
    expected_hash = sha256(profile.read_bytes()).hexdigest()

    chat = runner.invoke(app, ["chat", "--home", str(home), "--message", "remember profile"])

    assert chat.exit_code == 0
    assert "Fake LLM: remember profile" in chat.output
    with session_scope(home) as session:
        run = session.execute(select(Run)).scalar_one()
        assert run.workflow_id == "simple_chat"
        assert run.agent_profile_path == str(profile)
        assert run.agent_profile_hash == expected_hash
        assert run.llm_provider == "fake"
        assert run.llm_model == "fake-model"


def _configure_openai_llm(home: Path, base_url: str) -> None:
    config = home / "config.toml"
    text = config.read_text(encoding="utf-8")
    start = text.find("[llm]")
    end = text.find("\n[telegram]\n")
    replacement = f"""[llm]
provider = "openai"
model = "fixture-model"
temperature = 0.2
max_tokens = 32

[llm.providers.openai]
api_key_env = "AGENTEND_TEST_OPENAI_KEY"
base_url = "{base_url}"
"""
    config.write_text(replacement + text[end:], encoding="utf-8")


class _LLMFixture:
    def __init__(self) -> None:
        self.requests: list[str] = []
        self.message_batches: list[list[dict[str, str]]] = []
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def __enter__(self) -> "_LLMFixture":
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                assert self.path == "/v1/chat/completions"
                assert self.headers.get("Authorization") == "Bearer secret-value-that-must-not-leak"
                messages = payload["messages"]
                owner.message_batches.append(messages)
                prompt = messages[-1]["content"]
                owner.requests.append(prompt)
                body = json.dumps(
                    {
                        "model": payload["model"],
                        "choices": [{"message": {"content": f"fixture: {prompt}"}}],
                        "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                _ = format, args

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        assert self.server is not None
        self.server.shutdown()
        if self.thread is not None:
            self.thread.join(timeout=2)
        self.server.server_close()

    @property
    def base_url(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.server_port}/v1"
