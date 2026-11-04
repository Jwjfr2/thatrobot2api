from __future__ import annotations

import json
import logging
import sys

import uvicorn

from .app import create_app
from .config import ConfigError, Settings


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"thatrobot2api: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    app = create_app(settings)
    logging.getLogger("thatrobot2api").info(
        '{"event":"startup","host":%s,"port":%d,"session_file":%s,"upstream":%s}',
        json.dumps(settings.host),
        settings.port,
        json.dumps(str(settings.session_file)),
        json.dumps(settings.upstream_base_url),
    )
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
