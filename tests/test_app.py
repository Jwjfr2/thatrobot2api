from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from thatrobot2api.app import create_app
from thatrobot2api.config import Settings

AUTH = {"Authorization": "Bearer test-key"}


def build_app(handler, session_file: Path):
    settings = Settings(proxy_api_key="test-key", session_file=session_file)
    return create_app(settings, upstream_transport=httpx.MockTransport(handler))


def write_cookie(tmp_path: Path, value: str = "eyJtest") -> Path:
    path = tmp_path / "cookie.txt"
    path.write_text(value, encoding="utf-8")
    return path


def ok_handler(seen: list[dict] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/api/chat":
            return httpx.Response(200, text="<html></html>")
        if seen is not None:
            seen.append(json.loads(request.content))
        return httpx.Response(200, json={"content": "hello world", "sources": []})

    return handler


async def post(app, body: dict, headers: dict | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as client:
        return await client.post("/v1/chat/completions", json=body, headers=AUTH if headers is None else headers)


def chat_body(**overrides) -> dict:
    body = {"model": "claude-sonnet", "messages": [{"role": "user", "content": "hi"}]}
    body.update(overrides)
    return body


async def test_requires_bearer_token(tmp_path):
    app = build_app(ok_handler(), write_cookie(tmp_path))
    response = await post(app, chat_body(), headers={})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


async def test_models_lists_ten_ids(tmp_path):
    app = build_app(ok_handler(), write_cookie(tmp_path))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as client:
        response = await client.get("/v1/models", headers=AUTH)
    payload = response.json()
    assert payload["object"] == "list"
    assert len(payload["data"]) == 10
    assert payload["data"][0]["owned_by"] == "thatrobot"
    assert {item["id"] for item in payload["data"]} >= {"claude-sonnet", "gpt-oss-120b"}


async def test_chat_maps_request_and_response(tmp_path):
    seen: list[dict] = []
    app = build_app(ok_handler(seen), write_cookie(tmp_path))
    response = await post(
        app,
        chat_body(
            messages=[
                {"role": "system", "content": "be terse"},
                {"role": "user", "content": [{"type": "text", "text": "hi "}, {"type": "text", "text": "there"}]},
            ]
        ),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "hello world"}
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"] == {
        "prompt_tokens": 4,
        "completion_tokens": 3,
        "total_tokens": 7,
    }
    assert seen[0] == {
        "model": "claude-sonnet",
        "messages": [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hi there"},
        ],
    }


async def test_unknown_model_rejected_locally(tmp_path):
    seen: list[dict] = []
    app = build_app(ok_handler(seen), write_cookie(tmp_path))
    response = await post(app, chat_body(model="gpt-4o"))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "model_not_found"
    assert seen == []


@pytest.mark.parametrize("field,value", [("tools", []), ("tool_choice", "auto"), ("response_format", {"type": "text"})])
async def test_unsupported_fields_rejected(tmp_path, field, value):
    app = build_app(ok_handler(), write_cookie(tmp_path))
    response = await post(app, chat_body(**{field: value}))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_parameter"


async def test_n_above_one_rejected(tmp_path):
    app = build_app(ok_handler(), write_cookie(tmp_path))
    assert (await post(app, chat_body(n=2))).status_code == 400
    assert (await post(app, chat_body(n=1))).status_code == 200


async def test_stream_emits_openai_sse_frames(tmp_path):
    app = build_app(ok_handler(), write_cookie(tmp_path))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as client:
        async with client.stream("POST", "/v1/chat/completions", json=chat_body(stream=True), headers=AUTH) as response:
            assert response.headers["content-type"].startswith("text/event-stream")
            raw = "".join([chunk async for chunk in response.aiter_text()])
    events = [line[len("data: ") :] for line in raw.splitlines() if line.startswith("data: ")]
    assert events[-1] == "[DONE]"
    chunks = [json.loads(event) for event in events[:-1]]
    assert [chunk["object"] for chunk in chunks] == ["chat.completion.chunk"] * 3
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert chunks[1]["choices"][0]["delta"] == {"content": "hello world"}
    assert chunks[2]["choices"][0]["finish_reason"] == "stop"


async def test_upstream_401_becomes_session_expired(tmp_path):
    app = build_app(lambda request: httpx.Response(401, json={"error": "Not authenticated"}), write_cookie(tmp_path))
    response = await post(app, chat_body())
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "session_expired"
    assert "TR_SESSION_FILE" in response.json()["error"]["message"]


async def test_upstream_timeout_becomes_504(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("too slow", request=request)

    app = build_app(handler, write_cookie(tmp_path))
    response = await post(app, chat_body())
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "upstream_timeout"


async def test_upstream_500_becomes_502(tmp_path):
    app = build_app(lambda request: httpx.Response(500, text="boom"), write_cookie(tmp_path))
    response = await post(app, chat_body())
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_error"


async def test_missing_cookie_file_is_actionable(tmp_path):
    app = build_app(ok_handler(), tmp_path / "absent.txt")
    response = await post(app, chat_body())
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "session_missing"


WORKSPACE_HTML = '<script src="/_next/static/chunks/page-abc.js" async></script>'
PAGE_JS = 'const x=1;require("static/chunks/cat-def.js")'
CATALOG_JS = (
    'let r=[{group:"Anthropic (Claude)",id:"claude-sonnet",label:"Claude Sonnet"},'
    '{group:"New",id:"new-model-x",label:"New X"},'
    '{group:"New",id:"another-model",label:"Another"}];'
)


def discovery_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/workspace":
        return httpx.Response(200, text=WORKSPACE_HTML)
    if path == "/_next/static/chunks/page-abc.js":
        return httpx.Response(200, text=PAGE_JS)
    if path == "/_next/static/chunks/cat-def.js":
        return httpx.Response(200, text=CATALOG_JS)
    if path == "/api/chat":
        return httpx.Response(200, json={"content": "ok", "sources": []})
    return httpx.Response(404)


async def test_models_are_discovered_from_workspace_bundle(tmp_path):
    app = build_app(discovery_handler, write_cookie(tmp_path))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as client:
        payload = (await client.get("/v1/models", headers=AUTH)).json()
        ids = [item["id"] for item in payload["data"]]
        assert ids == ["claude-sonnet", "new-model-x", "another-model"]

        response = await client.post(
            "/v1/chat/completions",
            json={"model": "new-model-x", "messages": [{"role": "user", "content": "hi"}]},
            headers=AUTH,
        )
    assert response.status_code == 200


async def test_discovery_failure_falls_back_to_static_ids(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/workspace":
            return httpx.Response(500, text="nope")
        return httpx.Response(200, json={"content": "ok", "sources": []})

    app = build_app(handler, write_cookie(tmp_path))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as client:
        payload = (await client.get("/v1/models", headers=AUTH)).json()
        ids = [item["id"] for item in payload["data"]]
    assert len(ids) == 10
    assert "claude-sonnet" in ids
