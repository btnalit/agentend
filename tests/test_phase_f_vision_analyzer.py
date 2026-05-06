import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from agentend.cli import app
from agentend.db.models import ErrorRecord, ToolManifest
from agentend.db.session import session_scope


def test_vision_analyzer_fake_provider_describes_ocr_and_chart(tmp_path: Path) -> None:
    home = tmp_path / "agentend-home"
    image = home / "chart.png"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    image.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
            "0000000c4944415408d7636060600000000400010d0a2db40000000049454e44ae426082"
        )
    )

    described = runner.invoke(app, ["tools", "test", "vision.describe", "--home", str(home), "--input", json.dumps({"path": str(image)})])
    ocr = runner.invoke(app, ["tools", "test", "vision.ocr", "--home", str(home), "--input", json.dumps({"path": str(image)})])
    chart = runner.invoke(app, ["tools", "test", "vision.extract_chart", "--home", str(home), "--input", json.dumps({"path": str(image)})])

    assert described.exit_code == 0
    assert "fake vision description" in described.output
    assert ocr.exit_code == 0
    assert "ocr_text" in ocr.output
    assert chart.exit_code == 0
    assert "series" in chart.output
    with session_scope(home) as session:
        manifest = session.get(ToolManifest, "vision.describe")
        assert manifest is not None
        assert manifest.side_effect == "network_read"


def test_vision_openai_compatible_provider_uses_data_url_without_leaking_secret(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "agentend-home"
    image = home / "chart.png"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    image.write_bytes(_png_1x1())
    monkeypatch.setenv("AGENTEND_OPENAI_VISION_KEY", "openai-vision-secret")
    seen: dict[str, object] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            seen["path"] = self.path
            seen["authorization"] = self.headers.get("Authorization")
            seen["payload"] = json.loads(body)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps({"choices": [{"message": {"content": "Revenue Q1 Q2"}}]}).encode("utf-8")
            )

        def log_message(self, format: str, *args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}/v1"
    try:
        result = runner.invoke(
            app,
            [
                "tools",
                "test",
                "vision.ocr",
                "--home",
                str(home),
                "--input",
                json.dumps(
                    {
                        "path": str(image),
                        "provider": "openai-compatible",
                        "api_key_env": "AGENTEND_OPENAI_VISION_KEY",
                        "base_url": base_url,
                        "model": "vision-test",
                    }
                ),
            ],
        )
    finally:
        server.shutdown()

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["provider"] == "openai"
    assert payload["ocr_text"] == "Revenue Q1 Q2"
    assert "openai-vision-secret" not in result.output
    assert seen["path"] == "/v1/chat/completions"
    assert seen["authorization"] == "Bearer openai-vision-secret"
    request_payload = seen["payload"]
    image_part = request_payload["messages"][0]["content"][1]["image_url"]["url"]
    assert image_part.startswith("data:image/png;base64,")


def test_vision_gemini_provider_uses_inline_data_and_extracts_chart_json(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "agentend-home"
    image = home / "chart.png"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    image.write_bytes(_png_1x1())
    monkeypatch.setenv("AGENTEND_GEMINI_VISION_KEY", "gemini-secret")
    seen: dict[str, object] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            seen["path"] = self.path
            seen["api_key"] = self.headers.get("x-goog-api-key")
            seen["payload"] = json.loads(body)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "candidates": [
                            {
                                "content": {
                                    "parts": [
                                        {"text": '{"chart_type":"bar","series":[{"name":"revenue","values":[1,2]}]}'}
                                    ]
                                }
                            }
                        ]
                    }
                ).encode("utf-8")
            )

        def log_message(self, format: str, *args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}/v1beta"
    try:
        result = runner.invoke(
            app,
            [
                "tools",
                "test",
                "vision.extract_chart",
                "--home",
                str(home),
                "--input",
                json.dumps(
                    {
                        "path": str(image),
                        "provider": "gemini",
                        "api_key_env": "AGENTEND_GEMINI_VISION_KEY",
                        "base_url": base_url,
                        "model": "gemini-test",
                    }
                ),
            ],
        )
    finally:
        server.shutdown()

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["provider"] == "gemini"
    assert payload["chart_type"] == "bar"
    assert payload["series"][0]["name"] == "revenue"
    assert "gemini-secret" not in result.output
    assert seen["path"] == "/v1beta/models/gemini-test:generateContent"
    assert seen["api_key"] == "gemini-secret"
    request_payload = seen["payload"]
    assert request_payload["contents"][0]["parts"][1]["inline_data"]["mime_type"] == "image/png"


def test_vision_missing_real_provider_secret_records_structured_error(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "agentend-home"
    image = home / "chart.png"
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--home", str(home)]).exit_code == 0
    image.write_bytes(_png_1x1())
    monkeypatch.delenv("AGENTEND_MISSING_VISION_KEY", raising=False)

    result = runner.invoke(
        app,
        [
            "tools",
            "test",
            "vision.describe",
            "--home",
            str(home),
            "--input",
            json.dumps({"path": str(image), "provider": "openai", "api_key_env": "AGENTEND_MISSING_VISION_KEY"}),
        ],
    )

    assert result.exit_code == 1
    assert "missing_config" in result.output
    assert "AGENTEND_MISSING_VISION_KEY" in result.output
    with session_scope(home) as session:
        error = session.execute(select(ErrorRecord).where(ErrorRecord.error_code == "missing_config")).scalar_one()
        assert error.message == "Vision provider secret is not set: AGENTEND_MISSING_VISION_KEY"


def _png_1x1() -> bytes:
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
        "0000000c4944415408d7636060600000000400010d0a2db40000000049454e44ae426082"
    )
