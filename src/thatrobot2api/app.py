from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .config import Settings
from .discovery import ModelRegistry
from .models import MODEL_CREATED, MODEL_IDS
from .upstream import ThatRobotClient, UpstreamError, estimate_tokens

log = logging.getLogger("thatrobot2api")

UNSUPPORTED_FIELDS = (
    "tools",
    "tool_choice",
    "functions",
    "function_call",
    "response_format",
    "logprobs",
    "top_logprobs",
    "audio",
    "modalities",
)


class ClientError(Exception):
    def __init__(self, status: int, code: str, message: str, error_type: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.type = error_type


def error_response(status: int, code: str, message: str, error_type: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "message": message,
                "type": error_type,
                "param": None,
                "code": code,
            }
        },
    )


def _require_auth(request: Request) -> None:
    expected = request.app.state.settings.proxy_api_key
    header = request.headers.get("authorization", "")
    scheme, _, supplied = header.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.strip(), expected):
        raise ClientError(
            401,
            "invalid_api_key",
            "Missing or invalid bearer token.",
            "invalid_request_error",
        )


def _parse_body(text_body: bytes) -> dict[str, Any]:
    try:
        body = json.loads(text_body)
    except json.JSONDecodeError:
        raise ClientError(400, "invalid_json", "Request body is not valid JSON.", "invalid_request_error") from None
    if not isinstance(body, dict):
        raise ClientError(400, "invalid_request", "Request body must be a JSON object.", "invalid_request_error")
    return body


def _flatten_content(content: Any, index: int) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if not (isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)):
                raise ClientError(
                    400,
                    "unsupported_content",
                    f"messages[{index}] contains a non-text part; only text is supported upstream.",
                    "invalid_request_error",
                )
            parts.append(part["text"])
        return "".join(parts)
    raise ClientError(
        400,
        "invalid_request",
        f"messages[{index}].content must be a string or a list of text parts.",
        "invalid_request_error",
    )


def _validate(body: dict[str, Any], allowed: tuple[str, ...]) -> tuple[str, list[dict[str, str]], bool]:
    for field in UNSUPPORTED_FIELDS:
        if field in body:
            raise ClientError(
                400,
                "unsupported_parameter",
                f"'{field}' is not supported by the thatrobot upstream.",
                "invalid_request_error",
            )
    if body.get("n") not in (None, 1):
        raise ClientError(
            400,
            "unsupported_parameter",
            "'n' is not supported by the thatrobot upstream.",
            "invalid_request_error",
        )

    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ClientError(400, "invalid_request", "'model' is required.", "invalid_request_error")
    if model not in allowed:
        raise ClientError(
            400,
            "model_not_found",
            f"model '{model}' is not available. See GET /v1/models.",
            "invalid_request_error",
        )

    stream = body.get("stream", False)
    if not isinstance(stream, bool):
        raise ClientError(400, "invalid_request", "'stream' must be a boolean.", "invalid_request_error")

    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise ClientError(
            400, "invalid_request", "'messages' must be a non-empty array.", "invalid_request_error"
        )

    messages: list[dict[str, str]] = []
    for index, message in enumerate(raw_messages):
        if not isinstance(message, dict):
            raise ClientError(400, "invalid_request", f"messages[{index}] must be an object.", "invalid_request_error")
        role = message.get("role")
        if role not in ("system", "user", "assistant"):
            raise ClientError(
                400,
                "invalid_request",
                f"messages[{index}].role must be system, user or assistant (got {role!r}); "
                "tool/function roles need tool support.",
                "invalid_request_error",
            )
        messages.append({"role": role, "content": _flatten_content(message.get("content"), index)})

    return model, messages, stream


def _prompt_chars(messages: list[dict[str, str]]) -> int:
    return sum(len(message["content"]) for message in messages)


def _log_event(**fields: Any) -> None:
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **fields}
    log.info(json.dumps(record, ensure_ascii=False))


def _chunk(completion_id: str, model: str, created: int, delta: dict[str, Any], finish: str | None) -> str:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _sse_stream(completion_id: str, model: str, content: str) -> Any:
    created = int(time.time())
    yield _chunk(completion_id, model, created, {"role": "assistant"}, None)
    if content:
        yield _chunk(completion_id, model, created, {"content": content}, None)
    yield _chunk(completion_id, model, created, {}, "stop")
    yield "data: [DONE]\n\n"


def create_app(
    settings: Settings | None = None,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    client = ThatRobotClient(settings, transport=upstream_transport)
    registry = ModelRegistry(MODEL_IDS, client)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.client = client
        refresh_task = asyncio.create_task(registry.ensure_fresh())
        yield
        refresh_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await refresh_task
        await client.aclose()

    app = FastAPI(title="thatrobot2api", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.client = client
    app.state.registry = registry

    @app.exception_handler(ClientError)
    async def _client_error_handler(request: Request, exc: ClientError) -> JSONResponse:
        return error_response(exc.status, exc.code, exc.message, exc.type)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/models")
    async def list_models(request: Request) -> dict[str, Any]:
        _require_auth(request)
        await registry.ensure_fresh()
        return {
            "object": "list",
            "data": [
                {
                    "id": model_id,
                    "object": "model",
                    "created": MODEL_CREATED,
                    "owned_by": "thatrobot",
                }
                for model_id in registry.ids
            ],
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        _require_auth(request)
        body = _parse_body(await request.body())
        model, messages, stream = _validate(body, registry.ids)
        prompt_chars = _prompt_chars(messages)
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        started = time.perf_counter()
        completion_id = f"chatcmpl-{uuid.uuid4().hex}"

        try:
            result = await client.chat(model, messages)
        except UpstreamError as exc:
            fields: dict[str, Any] = {
                "event": "chat_error",
                "request_id": request_id,
                "model": model,
                "stream": stream,
                "status": exc.status,
                "code": exc.code,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "prompt_chars": prompt_chars,
            }
            if settings.log_prompts:
                fields["prompt"] = body.get("messages")
            _log_event(**fields)
            return error_response(exc.status, exc.code, exc.message, exc.type)

        prompt_tokens = estimate_tokens(prompt_chars)
        completion_tokens = estimate_tokens(len(result.content))
        fields = {
            "event": "chat",
            "request_id": request_id,
            "model": model,
            "stream": stream,
            "status": 200,
            "latency_ms": result.latency_ms,
            "prompt_chars": prompt_chars,
            "completion_chars": len(result.content),
            "est_prompt_tokens": prompt_tokens,
            "est_completion_tokens": completion_tokens,
            "sources_count": len(result.sources),
        }
        if settings.log_prompts:
            fields["completion"] = result.content
        _log_event(**fields)

        if stream:
            return StreamingResponse(
                _sse_stream(completion_id, model, result.content),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        return JSONResponse(
            content={
                "id": completion_id,
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": result.content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            }
        )

    return app
