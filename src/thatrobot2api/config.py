from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    proxy_api_key: str
    session_file: Path
    upstream_base_url: str = "https://platform.thatrobot.ai"
    timeout_s: float = 180.0
    log_prompts: bool = False
    host: str = "127.0.0.1"
    port: int = 8080

    @classmethod
    def from_env(cls) -> "Settings":
        key = os.environ.get("PROXY_API_KEY", "").strip()
        if not key:
            raise ConfigError("PROXY_API_KEY is required (bearer token clients must send)")
        session_file = Path(os.environ.get("TR_SESSION_FILE", "cookie.txt")).expanduser()
        try:
            timeout_s = float(os.environ.get("UPSTREAM_TIMEOUT", "180"))
        except ValueError:
            raise ConfigError("UPSTREAM_TIMEOUT must be a number of seconds") from None
        return cls(
            proxy_api_key=key,
            session_file=session_file,
            upstream_base_url=os.environ.get("UPSTREAM_BASE_URL", cls.upstream_base_url),
            timeout_s=timeout_s,
            log_prompts=os.environ.get("LOG_PROMPTS", "").lower() in {"1", "true", "yes"},
            host=os.environ.get("HOST", cls.host),
            port=int(os.environ.get("PORT", str(cls.port))),
        )
