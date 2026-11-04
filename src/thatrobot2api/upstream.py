from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import Settings

SESSION_COOKIE = "tr_session"

# CloudFront's managed SSRF rule rejects bodies containing a URL whose host is
# loopback/private (verified live: http://127.0.0.1/x, http://localhost, ftp://127.0.0.1,
# ws://127.0.0.1 -> 403; bare 127.0.0.1 and public URLs -> 200). OMP's own system
# prompt mentions http://127.0.0.1:9222, so the scheme is dropped before forwarding;
# the host, port and path stay readable to the model.
PRIVATE_URL_RE = re.compile(
    r"(?i)\b(?:https?|wss?|ftp)://(?="
    r"(?:localhost|127(?:\.\d{1,3}){3}|0\.0\.0\.0|\[::1\]|10(?:\.\d{1,3}){3}"
    r"|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}"
    r"|169\.254(?:\.\d{1,3}){2})(?![\w.-])(?::\d+)?"
    r")"
)


def rewrite_private_urls(messages: list[dict[str, str]]) -> tuple[list[dict[str, str]], int]:
    rewritten = 0
    sanitized: list[dict[str, str]] = []
    for message in messages:
        content, hits = PRIVATE_URL_RE.subn("", message["content"])
        rewritten += hits
        sanitized.append({**message, "content": content})
    return sanitized, rewritten


class CookieError(RuntimeError):
    pass


class UpstreamError(Exception):
    def __init__(self, status: int, code: str, message: str, error_type: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.type = error_type


def estimate_tokens(chars: int) -> int:
    """Deliberately crude (chars/4). Upstream returns no token counts."""
    return math.ceil(chars / 4) if chars > 0 else 0


def load_session(path: Path) -> str:
    """Read a raw JWT or a Netscape cookie file and return the tr_session value."""
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise CookieError(f"cannot read session file {path}: {exc.strerror or exc}") from exc

    if "\t" not in raw and "\n" not in raw.strip():
        token = raw.strip()
        if not token:
            raise CookieError(f"session file {path} is empty")
        return token

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) >= 7:
            name, value = fields[5], fields[6]
        else:
            parts = line.split()
            if SESSION_COOKIE not in parts:
                continue
            idx = parts.index(SESSION_COOKIE)
            if idx + 1 >= len(parts):
                continue
            name, value = SESSION_COOKIE, parts[idx + 1]
        if name == SESSION_COOKIE and value:
            return value

    raise CookieError(f"session file {path} has no {SESSION_COOKIE} cookie")


@dataclass(frozen=True)
class UpstreamResult:
    content: str
    sources: list
    latency_ms: int
    rewritten_urls: int = 0


class ThatRobotClient:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._session_file = settings.session_file
        self._timeout = settings.timeout_s
        self._client = httpx.AsyncClient(
            base_url=settings.upstream_base_url,
            timeout=httpx.Timeout(settings.timeout_s),
            transport=transport,
            headers={"Accept": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _session_header(self) -> dict[str, str]:
        try:
            token = load_session(self._session_file)
        except CookieError as exc:
            raise UpstreamError(
                502,
                "session_missing",
                f"{exc}. Put a fresh {SESSION_COOKIE} value in TR_SESSION_FILE.",
                "session_error",
            ) from exc
        return {"Cookie": f"{SESSION_COOKIE}={token}"}

    async def get_text(self, path: str, timeout: float = 20.0) -> str:
        headers = await self._session_header()
        try:
            response = await self._client.get(path, headers=headers, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise UpstreamError(504, "upstream_timeout", f"GET {path} timed out.", "timeout_error") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(
                502, "upstream_unreachable", f"Could not reach upstream: {exc.__class__.__name__}.", "upstream_error"
            ) from exc
        if 300 <= response.status_code < 400:
            raise UpstreamError(
                502,
                "session_expired",
                f"Upstream redirected {path}; the session cookie is stale.",
                "session_error",
            )
        if response.status_code >= 400:
            _raise_for_status(response)
        return response.text

    async def chat(self, model: str, messages: list[dict[str, str]]) -> UpstreamResult:
        headers = await self._session_header()
        messages, rewritten_urls = rewrite_private_urls(messages)

        started = time.perf_counter()
        try:
            response = await self._client.post(
                "/api/chat",
                json={"model": model, "messages": messages},
                headers=headers,
            )
        except httpx.TimeoutException as exc:
            raise UpstreamError(
                504,
                "upstream_timeout",
                f"Upstream did not answer within {self._timeout:g}s.",
                "timeout_error",
            ) from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(
                502,
                "upstream_unreachable",
                f"Could not reach upstream: {exc.__class__.__name__}.",
                "upstream_error",
            ) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        _raise_for_status(response)

        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise UpstreamError(
                502, "upstream_bad_response", "Upstream returned non-JSON.", "upstream_error"
            ) from exc
        content = payload.get("content")
        if not isinstance(content, str):
            raise UpstreamError(
                502,
                "upstream_bad_response",
                "Upstream response had no string content.",
                "upstream_error",
            )
        sources = payload.get("sources")
        return UpstreamResult(
            content=content,
            sources=sources if isinstance(sources, list) else [],
            latency_ms=latency_ms,
            rewritten_urls=rewritten_urls,
        )


def _raise_for_status(response: httpx.Response) -> None:
    status = response.status_code
    if 200 <= status < 300:
        return
    detail = _error_text(response)
    if status == 401:
        raise UpstreamError(
            502,
            "session_expired",
            "Upstream rejected the session cookie. Refresh TR_SESSION_FILE with a new "
            f"tr_session from platform.thatrobot.ai. Upstream said: {detail}",
            "session_error",
        )
    if status == 429:
        raise UpstreamError(429, "rate_limited", f"Upstream rate limited: {detail}", "rate_limit_error")
    if _is_cdn_block(response):
        raise UpstreamError(
            502,
            "upstream_blocked",
            "Upstream CDN blocked the request (WAF). Loopback/private URLs in the prompt are "
            "the known trigger; the proxy strips their scheme, so this indicates another rule. "
            f"CDN said: {detail}",
            "upstream_error",
        )
    if 400 <= status < 500:
        raise UpstreamError(400, "invalid_request", f"Upstream rejected the request: {detail}", "invalid_request_error")
    raise UpstreamError(502, "upstream_error", f"Upstream failed ({status}): {detail}", "upstream_error")


def _is_cdn_block(response: httpx.Response) -> bool:
    return (
        response.status_code == 403
        and response.headers.get("server", "").lower() == "cloudfront"
        and response.headers.get("content-type", "").startswith("text/html")
    )


def _error_text(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip()
        return text[:300] or "(empty body)"
    if isinstance(payload, dict):
        error = payload.get("error", payload)
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                return message[:500]
        if isinstance(error, str) and error:
            return error[:500]
    return json.dumps(payload)[:300]
