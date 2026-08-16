import pytest

from aim_server.config import ConfigError, load_config, validate_host


def test_missing_host_is_refused():
    with pytest.raises(ConfigError, match="AIM_HOST is not set"):
        load_config({})


def test_wildcard_bind_is_refused():
    with pytest.raises(ConfigError, match="every interface"):
        validate_host("0.0.0.0")
    with pytest.raises(ConfigError, match="every interface"):
        validate_host("::")


def test_hostname_is_refused():
    with pytest.raises(ConfigError, match="not a literal IP"):
        validate_host("pc-federico.tailnet.ts.net")


def test_public_and_lan_addresses_are_refused():
    for bad in ("8.8.8.8", "192.168.1.10", "10.0.0.5", "172.16.0.2"):
        with pytest.raises(ConfigError, match="not a Tailscale address"):
            validate_host(bad)


def test_tailscale_addresses_are_accepted():
    assert validate_host("100.64.0.1") == "100.64.0.1"
    assert validate_host("100.101.102.103") == "100.101.102.103"
    assert validate_host("fd7a:115c:a1e0::1") == "fd7a:115c:a1e0::1"


def test_cgnat_boundary():
    # 100.64.0.0/10 spans 100.64.0.0 – 100.127.255.255
    assert validate_host("100.127.255.255")
    with pytest.raises(ConfigError):
        validate_host("100.128.0.1")
    with pytest.raises(ConfigError):
        validate_host("100.63.255.255")


def test_loopback_requires_explicit_flag():
    with pytest.raises(ConfigError, match="AIM_ALLOW_LOOPBACK"):
        validate_host("127.0.0.1")
    assert validate_host("127.0.0.1", allow_loopback=True) == "127.0.0.1"


def test_full_config_defaults():
    config = load_config({"AIM_HOST": "100.64.0.1"})
    assert config.port == 8422
    assert config.db_path == "./data/aim.db"
    assert config.retention_days is None


def test_retention_parsing():
    assert load_config({"AIM_HOST": "100.64.0.1"}).retention_days is None
    assert (
        load_config({"AIM_HOST": "100.64.0.1", "AIM_RETENTION_DAYS": "0"}).retention_days
        is None
    )
    assert (
        load_config({"AIM_HOST": "100.64.0.1", "AIM_RETENTION_DAYS": "90"}).retention_days
        == 90
    )
    with pytest.raises(ConfigError):
        load_config({"AIM_HOST": "100.64.0.1", "AIM_RETENTION_DAYS": "-1"})
    with pytest.raises(ConfigError):
        load_config({"AIM_HOST": "100.64.0.1", "AIM_RETENTION_DAYS": "many"})


def test_bad_port():
    with pytest.raises(ConfigError):
        load_config({"AIM_HOST": "100.64.0.1", "AIM_PORT": "0"})
    with pytest.raises(ConfigError):
        load_config({"AIM_HOST": "100.64.0.1", "AIM_PORT": "http"})
