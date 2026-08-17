"""Fixtures: the MCP client wired to the REAL central server, in-process.

httpx.ASGITransport mounts the FastAPI app directly, so every test
exercises the full stack (tools → HTTP client → server → SQLite) with no
network and no mocks.
"""

# The sys.path bootstrap must run before the aim_server import.
# pylint: disable=wrong-import-position
import sys
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

from aim_server.db import init_db  # noqa: E402
from aim_server.main import create_app  # noqa: E402

from aim_mcp.client import AimClient  # noqa: E402
from aim_mcp.tools import AimTools  # noqa: E402
from aim_mcp.user_config import UserConfig  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def server_app(tmp_path):
    db_path = str(tmp_path / "server.db")
    # ASGITransport does not run the app's lifespan (where the schema is
    # created), so initialize the database explicitly.
    init_db(db_path)
    return create_app(db_path)


def make_tools(tmp_path, server_app, name="client") -> AimTools:
    """One AimTools = one agent with its own user_config file."""
    config = UserConfig.load(tmp_path / f"user_config_{name}.json")
    config.base_url = "http://aim.test"
    client = AimClient(
        "http://aim.test", transport=httpx.ASGITransport(app=server_app)
    )
    return AimTools(config, client)


@pytest.fixture
def tools(tmp_path, server_app) -> AimTools:
    return make_tools(tmp_path, server_app)


@pytest.fixture
def other_tools(tmp_path, server_app) -> AimTools:
    return make_tools(tmp_path, server_app, name="other")
