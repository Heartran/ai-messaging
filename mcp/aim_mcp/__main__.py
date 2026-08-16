"""Entrypoint: `python -m aim_mcp` (or the `aim-mcp` console script).

Runs the MCP server on stdio — the transport for a local client living
next to its agent. Configuration is resolved before serving so a missing
server URL fails fast with an explanation instead of failing on first use.
"""

from __future__ import annotations

import sys

from .server import configure, mcp


def run() -> int:
    try:
        configure()
    except RuntimeError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
