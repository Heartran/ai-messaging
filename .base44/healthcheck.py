#!/usr/bin/env python3
"""Compose healthcheck for the sandbox stack (read-only, no app code).

The container runs two processes — the server and the port bridge — so both
are probed and a failure names which one broke. Uses the server's own
``GET /health`` and the UI route, over plain HTTP with the stdlib only.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

PORT = os.environ.get("AIM_PORT", "8422")
PUBLIC_PORT = os.environ.get("AIM_PUBLIC_PORT", "3000")


def probe(label: str, url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=4) as response:
            response.read(1)
    except (urllib.error.URLError, OSError) as exc:
        print(f"{label}: FAILED {url} ({exc})", file=sys.stderr)
        return False
    print(f"{label}: ok ({url})")
    return True


def main() -> int:
    healthy = probe("server", f"http://127.0.0.1:{PORT}/health")
    # Not `and`: both probes must always run, so the log names every failure.
    healthy = probe("bridge", f"http://127.0.0.1:{PUBLIC_PORT}/ui") and healthy
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
