"""Browser demo endpoints, end to end in fixture mode with FastAPI's TestClient."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from imbusy.config import Settings
from imbusy.excuse.schema import ExcuseDraft, RefusalKind
from imbusy.fixtures import ScriptedLLM, demo_calls
from imbusy.llm import FakeLLM
from imbusy.main import create_app

WEB_FIXTURE = Path(__file__).parents[1] / "web" / "src" / "fixtures" / "demo-calls.json"


def demo_settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_path=tmp_path / "db.sqlite3",
        IMBUSY_WEB_DIST=tmp_path / "no-web",
        **overrides,
    )


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(demo_settings(tmp_path), ScriptedLLM(token_delay_seconds=0))
    with TestClient(app) as test_client:
        yield test_client


def compile_demo(
    client: TestClient, text: str = "tía Carmen, llaves, en 2 min"
) -> dict[str, object]:
    response = client.post("/demo/compile", json={"text": text, "timezone": "Europe/Madrid"})
    assert response.status_code == 200
    body: dict[str, object] = response.json()
    return body


def read_reply(ws: object) -> str:
    parts: list[str] = []
    while True:
        frame = ws.receive_json()  # type: ignore[attr-defined]
        if frame["type"] == "end":
            return "".join(parts)
        parts.append(frame["token"])
        if frame.get("last"):
            return "".join(parts)


def test_healthz_reports_modes_without_secrets(client: TestClient) -> None:
    body = client.get("/healthz").json()
    assert body == {"ok": True, "demo": True, "model": "fixture", "real_calls": False}


def test_compile_returns_script_and_token(client: TestClient) -> None:
    body = compile_demo(client)
    assert body["status"] == "ok"
    assert body["mode"] == "fixture"
    script = body["script"]
    assert isinstance(script, dict)
    assert script["caller_name"] == "Tía Carmen"
    assert script["voice_id"] == "es-f-aoede"
    assert isinstance(body["token"], str)


def test_compile_english_fixture(client: TestClient) -> None:
    body = compile_demo(client, "my manager Dana, the client moved the call")
    script = body["script"]
    assert isinstance(script, dict) and script["language"] == "en-US"


def test_compile_refusal_returns_message(tmp_path: Path, draft: ExcuseDraft) -> None:
    refusal = draft.model_copy(update={"refusal": RefusalKind.IMPERSONATES_AUTHORITY})
    app = create_app(demo_settings(tmp_path), FakeLLM(structured_responses=[refusal]))
    with TestClient(app) as test_client:
        body = compile_demo(test_client, "que me llame la policía")
    assert body["status"] == "refused"
    assert body["refusal"] == "impersonates_authority"
    assert "policía" in str(body["message"])


def test_compile_rate_limited_per_ip(tmp_path: Path) -> None:
    app = create_app(demo_settings(tmp_path, demo_max_sessions_per_hour=2))
    with TestClient(app) as test_client:
        compile_demo(test_client)
        compile_demo(test_client)
        third = test_client.post("/demo/compile", json={"text": "tía Carmen, llaves"})
    assert third.status_code == 429


def test_demo_socket_full_call(client: TestClient) -> None:
    """Greeting, four prompts, four scripted replies, then `end`."""
    body = compile_demo(client)
    lines = demo_calls()["calls"]["es-ES"]["lines"]
    with client.websocket_connect(f"/demo/ws?token={body['token']}") as ws:
        greeting = read_reply(ws)
        assert greeting.startswith("¿Hola? Oye, soy Carmen")
        ws.send_json({"type": "metric", "turn": 1, "firstAudioMs": 700})
        for user_text, state in zip(
            ["Hola, tía.", "¿Qué ha pasado?", "Vaya.", "Vale, voy."],
            ["hook", "escalate", "ask", "closing"],
            strict=True,
        ):
            ws.send_json(
                {"type": "prompt", "voicePrompt": user_text, "lang": "es-ES", "last": True}
            )
            assert read_reply(ws) == lines[state]
        end = ws.receive_json()
        assert end["type"] == "end"
        assert json.loads(end["handoffData"])["reason"] == "script-complete"


def test_demo_token_is_single_use(client: TestClient) -> None:
    token = compile_demo(client)["token"]
    with client.websocket_connect(f"/demo/ws?token={token}") as ws:
        read_reply(ws)
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(f"/demo/ws?token={token}") as ws,
    ):
        ws.receive_json()


def test_demo_socket_rejects_foreign_origin(client: TestClient) -> None:
    token = compile_demo(client)["token"]
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(
            f"/demo/ws?token={token}", headers={"origin": "https://evil.example"}
        ) as ws,
    ):
        ws.receive_json()


def test_real_call_routes_absent_without_twilio_config(client: TestClient) -> None:
    assert client.post("/telegram/webhook", json={}).status_code == 404
    assert client.post("/twilio/status").status_code == 404


def test_web_fixture_mirrors_backend_fixture() -> None:
    """The page's offline mode and the server's fixture mode play the same call."""
    assert json.loads(WEB_FIXTURE.read_text(encoding="utf-8"))["calls"] == demo_calls()["calls"]


def test_safeword_check_endpoint_uses_the_bot_rules(client: TestClient) -> None:
    assert client.post("/demo/safeword", json={"safeWord": "pink flamingo"}).json() == {
        "ok": True,
        "message": None,
    }
    bad = client.post("/demo/safeword", json={"safeWord": "help"}).json()
    assert bad["ok"] is False and "two words" in bad["message"]


def test_demo_safe_word_shows_the_alert_preview_and_the_caller_winds_down(
    client: TestClient,
) -> None:
    """Keyless end to end: set a safe word and a browser location, say it mid-call."""
    response = client.post(
        "/demo/compile",
        json={
            "text": "tía Carmen, llaves, en 2 min",
            "timezone": "Europe/Madrid",
            "safeWord": "Pink flamingo",
            "name": "Mari",
            "location": {"latitude": 40.416775, "longitude": -3.70379, "accuracy": 30},
        },
    )
    body = response.json()
    assert body["safeWordArmed"] is True
    lines = demo_calls()["calls"]["es-ES"]["lines"]
    with client.websocket_connect(f"/demo/ws?token={body['token']}") as ws:
        read_reply(ws)
        ws.send_json({"type": "prompt", "voicePrompt": "Hola, tía.", "lang": "es-ES", "last": True})
        assert read_reply(ws) == lines["hook"]
        ws.send_json(
            {
                "type": "prompt",
                "voicePrompt": "Uy. Pink flamingo. ¿Qué pasa?",
                "lang": "es-ES",
                "last": True,
            }
        )
        frames = []
        while True:
            frame = ws.receive_json()
            frames.append(frame)
            if frame.get("last"):
                break
        preview = next(f for f in frames if f["type"] == "safeword")
        spoken = "".join(f["token"] for f in frames if f["type"] == "text")
        assert spoken == lines["wrap_up"]
        assert preview["text"].startswith("Mari ha dicho su palabra de seguridad")
        assert preview["mapUrl"].startswith("https://www.openstreetmap.org/?mlat=40.41678")
        ws.send_json({"type": "prompt", "voicePrompt": "Vale.", "lang": "es-ES", "last": True})
        assert read_reply(ws) == lines["closing"]
        assert ws.receive_json()["type"] == "end"


def test_demo_rejects_an_unusable_safe_word(client: TestClient) -> None:
    response = client.post("/demo/compile", json={"text": "tía Carmen, llaves", "safeWord": "help"})
    assert response.status_code == 422
