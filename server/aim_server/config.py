"""Server configuration, loaded from environment variables.

The bind address is the security model: the server must listen only on a
Tailscale address, so the tailnet perimeter is the trust boundary. Nothing
here is ever hardcoded to a specific network (docs/design.md §2, §10).
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass

# Tailscale hands out addresses exclusively from these ranges.
TAILSCALE_IPV4 = ipaddress.ip_network("100.64.0.0/10")  # CGNAT range
TAILSCALE_IPV6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")

DEFAULT_PORT = 8422
DEFAULT_DB_PATH = "./data/aim.db"


class ConfigError(Exception):
    """Raised when the environment does not describe a safe configuration."""


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    db_path: str
    retention_days: int | None  # None = keep everything, forever
    allow_loopback: bool


def validate_host(host: str, allow_loopback: bool = False) -> str:
    """Validate the bind address against the Tailscale-only constraint.

    Accepts only literal IPs inside the Tailscale ranges — never 0.0.0.0,
    never a hostname, never a LAN or public address. Loopback is allowed
    only when explicitly requested (dev/tests without a tailnet).
    """
    if not host or not host.strip():
        raise ConfigError(
            "AIM_HOST is not set. Set it to this machine's Tailscale IP "
            "(run: tailscale ip -4). The server only binds inside the tailnet."
        )
    host = host.strip()
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        raise ConfigError(
            f"AIM_HOST={host!r} is not a literal IP address. Hostnames are "
            "not accepted: they can silently resolve outside the tailnet. "
            "Use the Tailscale IP itself (run: tailscale ip -4)."
        ) from None
    if addr.is_unspecified:
        raise ConfigError(
            f"AIM_HOST={host!r} would bind on every interface. Refusing: "
            "this server must never listen outside the tailnet."
        )
    if addr.is_loopback:
        if allow_loopback:
            return host
        raise ConfigError(
            f"AIM_HOST={host!r} is a loopback address. If you are running "
            "tests or local development without a tailnet, set "
            "AIM_ALLOW_LOOPBACK=1 explicitly. Never do this on a real "
            "deployment."
        )
    if isinstance(addr, ipaddress.IPv4Address) and addr in TAILSCALE_IPV4:
        return host
    if isinstance(addr, ipaddress.IPv6Address) and addr in TAILSCALE_IPV6:
        return host
    raise ConfigError(
        f"AIM_HOST={host!r} is not a Tailscale address (expected an IP in "
        f"{TAILSCALE_IPV4} or {TAILSCALE_IPV6}). The tailnet perimeter is "
        "the security model — binding anywhere else is refused by design."
    )


def load_config(environ: dict[str, str] | None = None) -> Config:
    """Build a validated Config from the environment (or a provided dict)."""
    env = os.environ if environ is None else environ

    allow_loopback = env.get("AIM_ALLOW_LOOPBACK", "").strip() == "1"
    host = validate_host(env.get("AIM_HOST", ""), allow_loopback=allow_loopback)

    raw_port = env.get("AIM_PORT", str(DEFAULT_PORT)).strip()
    try:
        port = int(raw_port)
    except ValueError:
        raise ConfigError(f"AIM_PORT={raw_port!r} is not an integer.") from None
    if not 1 <= port <= 65535:
        raise ConfigError(f"AIM_PORT={port} is outside 1-65535.")

    db_path = env.get("AIM_DB_PATH", DEFAULT_DB_PATH).strip() or DEFAULT_DB_PATH

    raw_retention = env.get("AIM_RETENTION_DAYS", "").strip()
    retention_days: int | None
    if raw_retention in ("", "0"):
        retention_days = None
    else:
        try:
            retention_days = int(raw_retention)
        except ValueError:
            raise ConfigError(
                f"AIM_RETENTION_DAYS={raw_retention!r} is not an integer."
            ) from None
        if retention_days < 0:
            raise ConfigError("AIM_RETENTION_DAYS cannot be negative.")

    return Config(
        host=host,
        port=port,
        db_path=db_path,
        retention_days=retention_days,
        allow_loopback=allow_loopback,
    )
