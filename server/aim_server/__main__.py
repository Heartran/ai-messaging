"""Entrypoint: `python -m aim_server`.

Configuration is validated before anything binds: an unset, wildcard or
non-Tailscale AIM_HOST stops the process with an explanation instead of
ever listening outside the tailnet.
"""

from __future__ import annotations

import logging
import sys

import uvicorn

from .config import ConfigError, load_config
from .main import create_app


def run() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    app = create_app(config.db_path, retention_days=config.retention_days)
    logging.getLogger("aim_server").info(
        "binding on %s:%d (tailnet-only), db=%s, retention=%s",
        config.host,
        config.port,
        config.db_path,
        f"{config.retention_days} days" if config.retention_days else "keep forever",
    )
    uvicorn.run(app, host=config.host, port=config.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
