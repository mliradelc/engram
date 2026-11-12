"""End-to-end proof that the custom provider reaches a real HTTP endpoint.

The unit tests mock httpx. This one starts an actual HTTP server speaking the
OpenAI chat-completions contract and drives complete_json against it, so the
URL assembly, the Authorization header and the JSON parsing are exercised over
a real socket rather than a mock.
"""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

RECEIVED = []


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        RECEIVED.append(
            {
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "model": body.get("model"),
                "messages": body.get("messages"),
                "response_format": body.get("response_format"),
            }
        )
        auth = self.headers.get("Authorization") or ""
        if "reject-me" in auth:
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"invalid api key"}')
            return
        payload = {
            "choices": [
                {
                    "message": {"content": '{"title": "Inception", "year": 2010}'},
                    "finish_reason": "stop",
                }
            ],
            "model": body.get("model"),
        }
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def live_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}/v1"
    server.shutdown()
    server.server_close()


def test_complete_json_reaches_a_real_openai_compatible_server(live_server):
    from app.core.ai_client import complete_json

    RECEIVED.clear()
    result = asyncio.run(
        complete_json(
            prompt="identify this disc",
            schema={"type": "object", "properties": {"title": {"type": "string"}}},
            provider="custom",
            api_key="sk-live-test",
            model="my-gateway-model",
            base_url=live_server,
        )
    )

    assert result == {"title": "Inception", "year": 2010}

    assert len(RECEIVED) == 1
    seen = RECEIVED[0]
    assert seen["path"] == "/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-live-test"
    assert seen["model"] == "my-gateway-model"
    assert seen["messages"][0]["content"] == "identify this disc"
    assert seen["response_format"] == {"type": "json_object"}


def test_a_real_401_is_classified_and_names_the_endpoint(live_server):
    from app.core.ai_client import complete_json
    from app.core.errors import AIProviderError

    RECEIVED.clear()
    with pytest.raises(AIProviderError) as exc:
        asyncio.run(
            complete_json(
                prompt="x",
                schema=None,
                provider="custom",
                api_key="reject-me",
                model="m",
                base_url=live_server,
                raise_on_error=True,
                retries=0,
            )
        )

    assert exc.value.code == "bad_key"
    assert live_server in str(exc.value)


def test_identify_from_label_works_through_the_custom_endpoint(live_server):
    """The feature the user asked for: disc identification via a custom AI."""
    from app.core.ai_identifier import identify_from_label

    RECEIVED.clear()
    result = asyncio.run(
        identify_from_label(
            "INCEPTION_2010",
            provider="custom",
            api_key="sk-live-test",
            model="my-gateway-model",
            base_url=live_server,
        )
    )

    assert result is not None
    assert result["title"] == "Inception"
    assert RECEIVED[0]["path"] == "/v1/chat/completions"
