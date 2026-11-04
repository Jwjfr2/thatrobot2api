from __future__ import annotations

import asyncio
import logging
import re
import time

from .upstream import ThatRobotClient

log = logging.getLogger("thatrobot2api")

# Workspace HTML uses "/_next/static/chunks/x.js"; JS chunks reference
# "static/chunks/x.js" (Next prepends the base path at runtime).
CHUNK_RE = re.compile(r"(?:/_next/)?(static/chunks/[A-Za-z0-9_./-]+\.js)")
MODEL_RE = re.compile(
    r"\{group:[\"']([^\"']{1,64})[\"'],id:[\"']([^\"']{1,64})[\"'],label:[\"']([^\"']{1,128})[\"']\}"
)
MODEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

MAX_CHUNKS = 40
MAX_DEPTH = 2
MAX_CHUNK_BYTES = 2_000_000


async def discover_model_ids(client: ThatRobotClient) -> tuple[str, ...]:
    """Scrape the authenticated workspace bundle for its model catalog.

    The platform exposes no model-listing endpoint; the only ground truth is the
    `{group, id, label}` array compiled into the workspace JS bundle.
    """
    html = await client.get_text("/workspace")
    texts = [html]
    frontier = {match.group(1) for match in CHUNK_RE.finditer(html)}
    fetched: set[str] = set()

    for _ in range(MAX_DEPTH):
        pending = [chunk for chunk in sorted(frontier) if chunk not in fetched][: MAX_CHUNKS - len(fetched)]
        if not pending:
            break
        results = await asyncio.gather(
            *(client.get_text("/_next/" + chunk) for chunk in pending),
            return_exceptions=True,
        )
        frontier = set()
        for chunk, result in zip(pending, results):
            fetched.add(chunk)
            if isinstance(result, BaseException) or len(result) > MAX_CHUNK_BYTES:
                continue
            texts.append(result)
            frontier.update(match.group(1) for match in CHUNK_RE.finditer(result))

    ordered: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for match in MODEL_RE.finditer(text):
            model_id = match.group(2)
            if MODEL_ID_RE.match(model_id) and model_id not in seen:
                seen.add(model_id)
                ordered.append(model_id)
    return tuple(ordered)


class ModelRegistry:
    """Static fallback + TTL-cached bundle discovery, single-flight."""

    def __init__(
        self,
        static_ids: tuple[str, ...],
        client: ThatRobotClient,
        ttl_s: float = 600.0,
    ) -> None:
        self._ids: tuple[str, ...] = static_ids
        self._client = client
        self._ttl_s = ttl_s
        self._last_attempt = 0.0
        self._lock = asyncio.Lock()
        self.source = "static"

    @property
    def ids(self) -> tuple[str, ...]:
        return self._ids

    def _stale(self) -> bool:
        return time.monotonic() - self._last_attempt >= self._ttl_s

    async def ensure_fresh(self) -> None:
        if not self._stale():
            return
        async with self._lock:
            if not self._stale():
                return
            self._last_attempt = time.monotonic()
            try:
                discovered = await discover_model_ids(self._client)
            except Exception as exc:  # noqa: BLE001 - discovery must never break serving
                log.warning('{"event":"models_refresh_failed","error":"%s"}', type(exc).__name__)
                return
            if len(discovered) < 3:
                log.warning('{"event":"models_refresh_empty","found":%d}', len(discovered))
                return
            if discovered != self._ids:
                log.info('{"event":"models_changed","count":%d}', len(discovered))
            log.info('{"event":"models_refreshed","count":%d,"source":"workspace_bundle"}', len(discovered))
            self._ids = discovered
            self.source = "workspace_bundle"
