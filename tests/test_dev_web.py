"""Tests for the development web chat wrapper."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import DeltaToolCall
from starlette.responses import JSONResponse

from ruperto.config import Settings
from ruperto.db import create_database_runtime
from ruperto.dev_web import (
    WEB_TOOL_NAME,
    build_web_chat_response,
    build_web_chat_stream,
    build_web_identity,
    create_web_chat_app,
    extract_latest_tool_reply,
    extract_latest_user_message,
    run_web_chat,
    submit_customer_message,
    web_chat_model,
    web_chat_stream,
)

pytestmark = pytest.mark.anyio
WEB_CHAT_PORT = 9000
HTTP_BAD_REQUEST = 400
HTTP_OK = 200


def build_settings(tmp_path: Path) -> Settings:
    """Create isolated settings for development web tests."""
    return Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'web-chat.db'}",
        store_name="Rotisería Test",
        bot_name="Ruperto Test",
        gemini_api_key=SecretStr("test-key"),
    )


def test_extract_latest_user_message_prefers_last_user_prompt():
    """The wrapper reads the latest user message from converted history."""
    messages: Sequence[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="Hola")]),
        ModelResponse(parts=[TextPart(content="Respuesta previa")], model_name="test"),
        ModelRequest(parts=[UserPromptPart(content="Quiero una hamburguesa")]),
    ]

    assert extract_latest_user_message(messages) == "Quiero una hamburguesa"


def test_extract_latest_user_message_supports_multipart_and_errors():
    """The wrapper accepts multipart prompts and fails explicitly without user text."""
    multipart_messages: Sequence[ModelMessage] = [
        ModelResponse(parts=[TextPart(content="Respuesta previa")], model_name="test"),
        ModelRequest(
            parts=[
                UserPromptPart(content=["Hola", "Quiero una pizza"]),
                SystemPromptPart(content="ignorar"),
            ]
        ),
    ]
    assert extract_latest_user_message(multipart_messages) == "Hola\nQuiero una pizza"

    with pytest.raises(ValueError, match="No se encontró un mensaje de usuario"):
        extract_latest_user_message([ModelResponse(parts=[TextPart(content="Sin usuario")], model_name="test")])


def test_extract_latest_tool_reply_returns_wrapper_tool_output():
    """The wrapper can recover the latest delegated backend reply."""
    messages: Sequence[ModelMessage] = [
        ModelRequest(parts=[ToolReturnPart(tool_name=WEB_TOOL_NAME, content="Pedido confirmado")]),
    ]

    assert extract_latest_tool_reply(messages) == "Pedido confirmado"


def test_extract_latest_tool_reply_skips_non_requests():
    """The wrapper ignores assistant-only messages when searching tool replies."""
    messages: Sequence[ModelMessage] = [
        ModelResponse(parts=[TextPart(content="Texto")], model_name="test"),
        ModelRequest(parts=[ToolReturnPart(tool_name=WEB_TOOL_NAME, content="Pedido confirmado")]),
    ]
    assert extract_latest_tool_reply(messages) == "Pedido confirmado"


def test_extract_latest_tool_reply_ignores_previous_turn_replies():
    """A new user prompt must force a fresh backend tool call."""
    messages: Sequence[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="Quiero una pizza")]),
        ModelRequest(parts=[ToolReturnPart(tool_name=WEB_TOOL_NAME, content="Tenemos muzza")]),
        ModelResponse(parts=[TextPart(content="Tenemos muzza")], model_name="test"),
        ModelRequest(parts=[UserPromptPart(content="¿Tenés otras?")]),
    ]

    assert extract_latest_tool_reply(messages) is None
    response = build_web_chat_response(messages)
    assert response.parts[0].part_kind == "tool-call"


def test_build_web_identity_uses_chat_id():
    """Development web identities derive from the frontend chat id."""
    assert build_web_identity("chat-123") == "web:chat-123"


def test_build_web_chat_response_requests_tool_before_returning_text():
    """The wrapper first asks for the backend tool and then returns plain text."""
    tool_call = build_web_chat_response([ModelRequest(parts=[UserPromptPart(content="Hola")])])
    assert tool_call.parts[0].part_kind == "tool-call"

    final = build_web_chat_response(
        [ModelRequest(parts=[ToolReturnPart(tool_name=WEB_TOOL_NAME, content="Todo bien")])]
    )
    assert final.parts[0].part_kind == "text"


def test_web_chat_model_wraps_response_builder():
    """The function-model entrypoint delegates to the same response builder."""
    response = web_chat_model([ModelRequest(parts=[UserPromptPart(content="Hola")])], info=None)  # type: ignore[arg-type]
    assert response.parts[0].part_kind == "tool-call"


async def test_build_web_chat_stream_emits_tool_call_then_text():
    """The streaming wrapper first emits a tool call and then the final text reply."""
    pending = [chunk async for chunk in build_web_chat_stream([ModelRequest(parts=[UserPromptPart(content="Hola")])])]
    assert isinstance(pending[0][0], DeltaToolCall)

    completed = [
        chunk
        async for chunk in build_web_chat_stream(
            [ModelRequest(parts=[ToolReturnPart(tool_name=WEB_TOOL_NAME, content="Pedido confirmado")])]
        )
    ]
    assert completed == ["Pedido confirmado"]


async def test_web_chat_stream_wraps_stream_builder():
    """The streaming entrypoint delegates to the same async builder."""
    pending = [
        chunk async for chunk in web_chat_stream([ModelRequest(parts=[UserPromptPart(content="Hola")])], info=None)
    ]  # type: ignore[arg-type]
    assert isinstance(pending[0][0], DeltaToolCall)


async def test_submit_customer_message_uses_real_service(mocker, tmp_path: Path):
    """The wrapper tool forwards the message through the ordering service."""
    fake_result = mocker.Mock()
    fake_result.reply.reply_text = "Hola, ¿qué querés pedir?"
    handle = mocker.patch("ruperto.dev_web.OrderingAssistantService.handle_customer_message", return_value=fake_result)
    ctx = mocker.Mock()
    ctx.deps.session_factory = create_database_runtime(build_settings(tmp_path)).session_factory
    ctx.deps.settings = build_settings(tmp_path)
    ctx.deps.external_user_id = "web:chat-123"

    reply_text = await submit_customer_message(ctx, "Hola")

    assert reply_text == "Hola, ¿qué querés pedir?"
    handle.assert_awaited_once()


async def test_create_web_chat_app_serves_ui_and_chat(mocker, tmp_path: Path):
    """The custom web app serves the official UI and derives a stable web identity."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    mocker.patch("ruperto.dev_web._get_ui_html", return_value=b"<html>ui</html>")

    observed: dict[str, object] = {}

    async def fake_dispatch_request(request, *, agent, deps, **kwargs):
        observed["external_user_id"] = deps.external_user_id
        return JSONResponse({"ok": True, "user_id": deps.external_user_id})

    mocker.patch("ruperto.dev_web.VercelAIAdapter.dispatch_request", side_effect=fake_dispatch_request)
    app = create_web_chat_app(settings=settings, session_factory=runtime.session_factory)

    with TestClient(app) as client:
        index_response = client.get("/")
        options_response = client.options("/api/chat")
        configure_response = client.get("/api/configure")
        health_response = client.get("/api/health")
        chat_response = client.post(
            "/api/chat",
            json={
                "trigger": "submit-message",
                "id": "chat-123",
                "messages": [{"id": "m1", "role": "user", "parts": [{"type": "text", "text": "Hola"}]}],
                "model": "ruperto-dev-web",
            },
        )
        invalid_model_response = client.post(
            "/api/chat",
            json={
                "trigger": "submit-message",
                "id": "chat-123",
                "messages": [{"id": "m1", "role": "user", "parts": [{"type": "text", "text": "Hola"}]}],
                "model": "otro-modelo",
            },
        )

    assert index_response.text == "<html>ui</html>"
    assert options_response.status_code == HTTP_OK
    assert configure_response.json()["models"][0]["id"] == "ruperto-dev-web"
    assert health_response.json() == {"ok": True}
    assert chat_response.json()["user_id"] == "web:chat-123"
    assert observed["external_user_id"] == "web:chat-123"
    assert invalid_model_response.status_code == HTTP_BAD_REQUEST

    await runtime.engine.dispose()


def test_extract_latest_tool_reply_returns_none_without_requests():
    """The wrapper returns no tool reply when the history has no model requests."""
    messages: Sequence[ModelMessage] = [ModelResponse(parts=[TextPart(content="Solo texto")], model_name="test")]
    assert extract_latest_tool_reply(messages) is None


def test_run_web_chat_starts_uvicorn(mocker, tmp_path: Path):
    """The entrypoint boots uvicorn with the generated Starlette app."""
    settings = build_settings(tmp_path)
    runtime = create_database_runtime(settings)
    uvicorn_run = mocker.patch("ruperto.dev_web.uvicorn.run")

    assert (
        run_web_chat(
            settings=settings,
            session_factory=runtime.session_factory,
            host="0.0.0.0",
            port=WEB_CHAT_PORT,
        )
        == 0
    )
    uvicorn_run.assert_called_once()
