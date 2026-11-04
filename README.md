# thatrobot2api

OpenAI-compatible reverse proxy for `platform.thatrobot.ai`. One upstream call per request:

```
client --(OpenAI JSON)--> localhost:8080 --(Cookie: tr_session=...)--> platform.thatrobot.ai/api/chat
```

## Upstream contract (verified live, 2026-11-04)

| Property | Observed |
|---|---|
| Endpoint | `POST /api/chat`, body `{"model", "messages":[{"role","content"}]}` |
| Response | `{"content": "<text>", "sources": []}` — no `usage`, no `tool_calls`, `sources` always empty |
| Streaming | not supported upstream (`stream:true` is ignored) |
| Models | `claude-haiku`, `claude-sonnet`, `claude-opus`, `gpt-oss-120b`, `gpt-oss-20b`, `gemma-3-27b`, `deepseek-v3.2`, `qwen3-235b`, `mistral-large-3`, `nova-pro` |
| Ignored params | `temperature`, `top_p`, `max_tokens`, `stop`, `n` (n>1 is rejected by this proxy) |
| Auth | `tr_session` cookie (8-hour JWT). Missing/invalid → `401 {"error":"Not authenticated"}` |
| Rate limits | no `x-ratelimit-*` headers observed; 3 concurrent requests succeeded |

## Proxy surface

* `POST /v1/chat/completions` — JSON or emulated SSE (`stream:true`)
* `GET /v1/models` — model ids autodiscovered from the workspace bundle (see below)
* `GET /healthz`

## Model discovery

The platform has no model-listing endpoint, so the proxy scrapes the authenticated
`/workspace` page, follows its `_next/static/chunks/*.js` imports, and reads the
`{group, id, label}` catalog compiled into the bundle.

* Refresh runs at startup, then at most once every 10 minutes, and is triggered lazily
  by `GET /v1/models` (single-flight; a failed scrape never blocks requests).
* Successful scrape replaces the allowlist; fewer than 3 ids counts as failure.
* On failure the compiled-in list in `src/thatrobot2api/models.py` stays in effect and
  `{"event":"models_refresh_failed"}` is logged.
* Success logs `{"event":"models_refreshed","count":N,"source":"workspace_bundle"}`.
* The allowlist for `/v1/chat/completions` is the live registry, so a newly discovered
  id is accepted as soon as `/v1/models` has refreshed.

## Quick start

```bash
uv sync --extra dev
printf '%s' 'eyJ...your tr_session JWT...' > cookie.txt   # or use a Netscape cookie file
chmod 600 cookie.txt

PROXY_API_KEY=local-secret uv run thatrobot2api
```

Or copy `.env.example` to `.env`, fill it in, and `set -a; source .env; set +a` before launching (the app reads the process environment, not `.env` files).

```bash
curl -s http://127.0.0.1:8080/v1/chat/completions \
  -H 'Authorization: Bearer local-secret' -H 'Content-Type: application/json' \
  -d '{"model":"claude-sonnet","messages":[{"role":"user","content":"Reply with exactly: PONG"}]}'
```

Any OpenAI client works with `base_url=http://127.0.0.1:8080/v1` and `api_key=<PROXY_API_KEY>`.

### OMP

`~/.omp/agent/models.yml`:

```yaml
providers:
  thatrobot:
    baseUrl: http://127.0.0.1:8080/v1
    apiKey: local-secret
    authHeader: true
    api: openai-completions
    discovery:
      type: openai-models-list   # pulls /v1/models, so new ids appear automatically
      timeoutMs: 20000
    modelOverrides:              # upstream has no tool calling
      claude-sonnet: { supportsTools: false }
      # ... one entry per discovered id
```

Without `supportsTools: false`, OMP sends `tools` and the proxy returns
`400 unsupported_parameter`. Verified end to end: `omp -p --model thatrobot/claude-sonnet`
answers, and a config without the overrides fails with that 400.

## Configuration

| Env | Default | Meaning |
|---|---|---|
| `PROXY_API_KEY` | required | bearer token clients must send |
| `TR_SESSION_FILE` | `./cookie.txt` | raw JWT **or** Netscape cookie file containing `tr_session`; re-read on every request |
| `UPSTREAM_BASE_URL` | `https://platform.thatrobot.ai` | upstream origin |
| `UPSTREAM_TIMEOUT` | `180` | seconds |
| `HOST` / `PORT` | `127.0.0.1` / `8080` | bind address |
| `LOG_PROMPTS` | off | `1` adds prompt/completion text to JSON logs |

Logs are one JSON object per line on stdout: request id, model, stream, status, latency, char counts, estimated tokens. The cookie/JWT is never logged.

## Session expiry

The session JWT lasts ~8 hours. When it lapses the proxy returns:

```json
{"error":{"message":"Upstream rejected the session cookie. Refresh TR_SESSION_FILE ...","type":"session_error","code":"session_expired"}}
```

Refresh by logging into `platform.thatrobot.ai` and writing the new `tr_session` value to `TR_SESSION_FILE`; the next request picks it up with no restart.

## Known limitations

* No incremental streaming: the upstream answers in one JSON body, so SSE chunks are emitted after the full answer arrives.
* No tool calling, function calling, images, or structured output — `tools`, `tool_choice`, `response_format`, `n>1`, `logprobs`, `audio`, `modalities` return `400 unsupported_parameter`.
* Sampling parameters are ignored by the upstream; the proxy accepts and drops them rather than failing clients.
* `usage` is estimated as `ceil(chars / 4)`; it is not provider-reported.
* The upstream's CloudFront WAF (SSRF managed rule) rejects any body containing a URL
  whose host is loopback/private: `http://127.0.0.1/x`, `http://localhost`,
  `ftp://127.0.0.1`, `ws://127.0.0.1` all return a CloudFront 403. The proxy therefore
  strips the scheme from such URLs before forwarding (`http://127.0.0.1:9222` becomes
  `127.0.0.1:9222`); the host, port, and path reach the model unchanged. Every rewrite
  is counted in the `rewritten_private_urls` log field. A surviving CDN block maps to
  `502 upstream_blocked`.

## Docker

```bash
docker build -t thatrobot2api .
docker run --rm -p 127.0.0.1:8080:8080 \
  -e PROXY_API_KEY=local-secret -v "$PWD/cookie.txt:/app/cookie.txt:ro" thatrobot2api
```

## Tests

```bash
uv run pytest
```
