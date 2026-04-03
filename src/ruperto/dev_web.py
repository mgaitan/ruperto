"""Development web chat powered by PydanticAI's built-in client UI."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from pydantic import TypeAdapter
from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.ui._web.api import ChatRequestExtra, ConfigureFrontend, ModelInfo, validate_request_options
from pydantic_ai.ui._web.app import _get_ui_html
from pydantic_ai.ui.vercel_ai._adapter import VercelAIAdapter
from pydantic_ai.ui.vercel_ai.request_types import RequestData
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route

from ruperto.assistant import OrderingAssistantService
from ruperto.config import Settings
from ruperto.models import Channel

WEB_MODEL_ID = "ruperto-dev-web"
WEB_TOOL_NAME = "submit_customer_message"
REQUEST_DATA_ADAPTER = TypeAdapter(RequestData)


class MissingWebChatUserMessageError(ValueError):
    """Raised when the wrapper cannot recover the latest user message."""

    def __init__(self) -> None:
        super().__init__("No se encontró un mensaje de usuario para enviar al asistente.")


@dataclass(slots=True)
class WebChatDeps:
    """Dependencies required by the development web chat wrapper."""

    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    external_user_id: str


def extract_latest_user_message(messages: Sequence[ModelMessage]) -> str:
    """Return the latest plain-text user message from the converted history."""
    for message in reversed(messages):
        if not isinstance(message, ModelRequest):
            continue
        for part in reversed(message.parts):
            if not isinstance(part, UserPromptPart):
                continue
            if isinstance(part.content, str):
                return part.content
            chunks = [chunk for chunk in part.content if isinstance(chunk, str)]
            if chunks:
                return "\n".join(chunks)
    raise MissingWebChatUserMessageError


def extract_latest_tool_reply(messages: Sequence[ModelMessage]) -> str | None:
    """Return the latest wrapper tool reply if the current turn already called it."""
    for message in reversed(messages):
        if not isinstance(message, ModelRequest):
            continue
        for part in reversed(message.parts):
            if isinstance(part, ToolReturnPart) and part.tool_name == WEB_TOOL_NAME:
                return str(part.content)
    return None


def build_web_chat_response(messages: Sequence[ModelMessage]) -> ModelResponse:
    """Build the wrapper model response from the current message history."""
    reply_text = extract_latest_tool_reply(messages)
    if reply_text is None:
        return ModelResponse(
            parts=[
                ToolCallPart(
                    WEB_TOOL_NAME,
                    {"message_text": extract_latest_user_message(messages)},
                )
            ],
            model_name=WEB_MODEL_ID,
        )

    return ModelResponse(parts=[TextPart(content=reply_text)], model_name=WEB_MODEL_ID)


def web_chat_model(messages: list[ModelMessage], info: AgentInfo | None) -> ModelResponse:
    """Drive the wrapper agent by always delegating to the real ordering service."""
    return build_web_chat_response(messages)


async def build_web_chat_stream(messages: Sequence[ModelMessage]) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
    """Build the streaming wrapper response from the current message history."""
    reply_text = extract_latest_tool_reply(messages)
    if reply_text is None:
        payload = json.dumps({"message_text": extract_latest_user_message(messages)}, ensure_ascii=False)
        yield {0: DeltaToolCall(name=WEB_TOOL_NAME, json_args=payload, tool_call_id="submit-customer-message")}
        return

    yield reply_text


async def web_chat_stream(
    messages: list[ModelMessage], info: AgentInfo | None
) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
    """Stream the wrapper response so the official web client can render it."""
    async for chunk in build_web_chat_stream(messages):
        yield chunk


web_chat_agent = Agent(
    FunctionModel(function=web_chat_model, stream_function=web_chat_stream, model_name=WEB_MODEL_ID),
    deps_type=WebChatDeps,
    output_type=str,
    instructions=(
        "Este agente de desarrollo no conversa por su cuenta. "
        "Siempre delega cada mensaje al backend transaccional de Ruperto "
        "y devuelve únicamente la respuesta textual generada allí."
    ),
)


@web_chat_agent.tool
async def submit_customer_message(ctx: RunContext[WebChatDeps], message_text: str) -> str:
    """Forward the user's message to the real ordering assistant service."""
    service = OrderingAssistantService(
        session_factory=ctx.deps.session_factory,
        settings=ctx.deps.settings,
    )
    result = await service.handle_customer_message(
        channel=Channel.DEV,
        external_user_id=ctx.deps.external_user_id,
        message_text=message_text,
    )
    return result.reply.reply_text


def build_web_identity(chat_id: str) -> str:
    """Build a stable development identity from the web chat identifier."""
    return f"web:{chat_id}"


def create_web_chat_app(
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    html_source: str | Path | None = None,
) -> Starlette:
    """Create a Starlette app that serves the PydanticAI web client for development."""

    async def options_chat(request: Request) -> Response:
        return Response()

    async def configure_frontend(request: Request) -> Response:
        config = ConfigureFrontend(
            models=[ModelInfo(id=WEB_MODEL_ID, name="Ruperto dev web", builtin_tools=[])],
            builtin_tools=[],
        )
        return JSONResponse(config.model_dump(by_alias=True))

    async def health(request: Request) -> Response:
        return JSONResponse({"ok": True})

    async def post_chat(request: Request) -> Response:
        run_input = REQUEST_DATA_ADAPTER.validate_json(await request.body())
        extra_data = ChatRequestExtra.model_validate(getattr(run_input, "__pydantic_extra__", {}))
        if error := validate_request_options(extra_data, {WEB_MODEL_ID}, set()):
            return JSONResponse({"error": error}, status_code=400)

        deps = WebChatDeps(
            settings=settings,
            session_factory=session_factory,
            external_user_id=build_web_identity(run_input.id),
        )
        return await VercelAIAdapter[WebChatDeps, str].dispatch_request(
            request,
            agent=web_chat_agent,
            deps=deps,
        )

    api_app = Starlette(
        routes=[
            Route("/chat", options_chat, methods=["OPTIONS"]),
            Route("/chat", post_chat, methods=["POST"]),
            Route("/configure", configure_frontend, methods=["GET"]),
            Route("/health", health, methods=["GET"]),
        ]
    )

    async def index(request: Request) -> Response:
        return HTMLResponse(
            content=await _get_ui_html(html_source),
            headers={"Cache-Control": "public, max-age=3600"},
        )

    return Starlette(
        routes=[
            Mount("/api", app=api_app),
            Route("/", index, methods=["GET"]),
            Route("/{id}", index, methods=["GET"]),
        ]
    )


def run_web_chat(*, settings: Settings, session_factory: async_sessionmaker[AsyncSession], host: str, port: int) -> int:
    """Run the development web chat server."""
    app = create_web_chat_app(settings=settings, session_factory=session_factory)
    uvicorn.run(app, host=host, port=port)
    return 0
