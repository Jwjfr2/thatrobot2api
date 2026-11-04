# AGENTS.md

## Project

OpenAI-compatible reverse proxy for `platform.thatrobot.ai`. The platform exposes a
single undocumented endpoint, `POST /api/chat` with a `tr_session` cookie, returning
`{"content", "sources"}`. This proxy translates OpenAI Chat Completions traffic to and
from that endpoint, emulates SSE, validates models against a live registry, and reports
estimated usage.

## Commands

```bash
uv sync --extra dev                      # install (Python >= 3.12)
uv run pytest                            # test suite (no network)
PROXY_API_KEY=local-secret uv run thatrobot2api
docker build -t thatrobot2api .
docker run --rm -p 127.0.0.1:8080:8080 \
  -e PROXY_API_KEY=local-secret -v "$PWD/cookie.txt:/app/cookie.txt:ro" thatrobot2api
```

Environment: `PROXY_API_KEY` (required), `TR_SESSION_FILE` (default `./cookie.txt`,
raw JWT or Netscape cookie file), `UPSTREAM_BASE_URL`, `UPSTREAM_TIMEOUT`, `HOST`,
`PORT`, `LOG_PROMPTS`.

## Layout

| Path | Purpose |
|---|---|
| `src/thatrobot2api/app.py` | FastAPI routes, request validation, SSE emulation |
| `src/thatrobot2api/upstream.py` | httpx client, cookie parsing, error mapping, token estimate |
| `src/thatrobot2api/discovery.py` | workspace-bundle model discovery + TTL registry |
| `src/thatrobot2api/models.py` | compiled-in fallback model ids |
| `tests/` | contract tests against a mocked upstream |

Never commit `cookie.txt`, `.env`, or any live `tr_session` value. The session JWT lasts
about 8 hours; refresh the file in place so the container sees the new inode.

## Commit messages: Conventional Commits

All commits MUST follow [Conventional Commits 1.0.0](https://www.conventionalcommits.org/):

```
<type>[optional scope][!]: <description>

[optional body]

[optional footer(s)]
```

**Types**: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`,
`chore`, `revert`.

**Scopes** (optional but preferred): `app`, `upstream`, `discovery`, `models`, `config`,
`docker`, `tests`, `docs`.

Rules:

- Description in imperative mood, lowercase, no trailing period, at most 72 characters:
  `feat(discovery): refresh model ids from the workspace bundle`.
- One logical change per commit; do not mix refactors with behavior changes.
- Body explains the problem and the decision, wrapped at 72 characters, when the subject
  alone is not enough.
- Breaking changes use `!` before the colon and a `BREAKING CHANGE:` footer describing the
  migration.
- Reference issues in a footer: `Refs: #12` / `Closes: #12`.
- Reverts use `revert: <subject of the reverted commit>` with the reverted hash in the body.
- Never commit generated artifacts, `.venv/`, `__pycache__/`, or model/browser state.

Examples:

```
feat(upstream): map upstream 401 to a session_expired 502
fix(app): reject n>1 before calling upstream
test(discovery): cover fallback when the bundle scrape fails
docs: document OMP provider setup and supportsTools requirement
build(docker): copy only src and pyproject into the image
refactor(upstream)!: replace ad-hoc cookie parsing with load_session

BREAKING CHANGE: TR_SESSION now accepts only a file path.
```

## Verification expectations

- Run `uv run pytest` before every commit that changes behavior.
- For changes to request/response translation, smoke the real path:
  `curl` an OpenAI-shaped request through the running proxy.
- Keep `/v1/models` consistent with the upstream catalog; discovery failures must fall
  back to `src/thatrobot2api/models.py` rather than returning an empty list.
