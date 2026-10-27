#!/usr/bin/env python3
"""Sandbox-only launcher for the AI Messaging server. Not part of the app.

The server binds a Tailscale address and nothing else — that perimeter *is*
its security model (server/aim_server/config.py). This sandbox has no
tailnet, so it starts with the project's own dev switch
(``AIM_ALLOW_LOOPBACK=1``), which makes the server listen on 127.0.0.1:
correct, and unreachable from outside the container.

This launcher closes that gap without touching the app:

* uvicorn ``--reload`` so edits under ``server/`` show up without a rebuild
  (the install is editable, see AGENTS.md);
* a TCP forwarder on ``0.0.0.0:3000`` → the server's loopback bind, which is
  the port the sandbox preview publishes.

Nothing here relaxes the app: configuration validation, participant tokens
and the Tailscale/loopback rules run exactly as they do in production.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading

import uvicorn

from aim_server.config import load_config
from aim_server.main import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("base44.dev")

CONFIG = load_config()
PUBLIC_PORT = int(os.environ.get("AIM_PUBLIC_PORT", "3000"))
RELOAD_DIR = os.environ.get("AIM_RELOAD_DIR", os.path.join(os.path.dirname(__file__), "..", "server"))
BUFFER_BYTES = 65536


def app_factory():
    """ASGI app factory for ``uvicorn --factory`` (re-imported on each reload)."""
    config = load_config()
    return create_app(
        config.db_path,
        retention_days=config.retention_days,
        operator_key=config.operator_key,
    )


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Copy one direction of the proxied connection until it ends."""
    try:
        while True:
            chunk = await reader.read(BUFFER_BYTES)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (OSError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


async def _handle(client_reader, client_writer) -> None:
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(
            CONFIG.host, CONFIG.port
        )
    except OSError as exc:
        log.warning(
            "bridge: cannot reach %s:%d (%s)", CONFIG.host, CONFIG.port, exc
        )
        client_writer.close()
        return
    await asyncio.gather(
        _pump(client_reader, upstream_writer),
        _pump(upstream_reader, client_writer),
    )


async def _serve_bridge() -> None:
    server = await asyncio.start_server(_handle, "0.0.0.0", PUBLIC_PORT)
    log.info(
        "bridge: 0.0.0.0:%d -> %s:%d (sandbox preview only)",
        PUBLIC_PORT,
        CONFIG.host,
        CONFIG.port,
    )
    async with server:
        await server.serve_forever()


def _run_bridge() -> None:
    try:
        asyncio.run(_serve_bridge())
    except Exception:  # a dead bridge must not take the server down with it
        log.exception("bridge failed")


if __name__ == "__main__":
    if os.environ.get("BASE44_PREVIEW_MODE") == "1":
        log.info("BASE44_PREVIEW_MODE=1: sandbox preview mode")
    log.info("server: %s:%d (dev), db=%s", CONFIG.host, CONFIG.port, CONFIG.db_path)
    threading.Thread(target=_run_bridge, daemon=True).start()
    uvicorn.run(
        "base44_dev_server:app_factory",
        factory=True,
        host=CONFIG.host,
        port=CONFIG.port,
        reload=True,
        reload_dirs=[RELOAD_DIR],
        log_level="info",
    )
