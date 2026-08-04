from example_service.infrastructure.config import Config
from example_service.mcp_server import (
    _extra_allowed_hosts,
    _http_token,
    build_transport_security,
    connector_urls,
    render_urls,
)


def test_http_token_generated_and_stable(tmp_path) -> None:
    config = Config(data_dir=tmp_path, data_dir_source="test")
    first = _http_token(config)
    second = _http_token(config)
    assert first == second
    assert len(first) > 20
    token_path = tmp_path / "mcp-http-token"
    assert token_path.exists()
    assert oct(token_path.stat().st_mode)[-3:] == "600"


def test_http_token_rotation_changes_value(tmp_path) -> None:
    config = Config(data_dir=tmp_path, data_dir_source="test")
    first = _http_token(config)
    second = _http_token(config, rotate=True)
    assert first != second


def test_extra_allowed_hosts_merges_cli_and_env() -> None:
    hosts = _extra_allowed_hosts(
        ["a.example"], env={"EXAMPLE_SERVICE_ALLOWED_HOSTS": "b.example,c.example"}
    )
    assert hosts == ["a.example", "b.example", "c.example"]


def test_extra_allowed_hosts_strips_pasted_url() -> None:
    hosts = _extra_allowed_hosts(["https://a.example/mcp/token"], env={})
    assert hosts == ["a.example"]


def test_connector_urls_include_token() -> None:
    pairs = connector_urls("tok123", ["tunnel.example"], port=9000)
    labels = dict(pairs)
    assert labels["MCP over HTTP"] == "http://127.0.0.1:9000/mcp/tok123"
    assert labels["Tunnel MCP connector"] == "https://tunnel.example/mcp/tok123"


def test_render_urls_formats_lines() -> None:
    lines = render_urls("tok123", [])
    assert lines == ["MCP over HTTP: http://127.0.0.1:8787/mcp/tok123"]


def test_transport_security_always_includes_loopback() -> None:
    settings = build_transport_security(["tunnel.example"])
    assert "127.0.0.1:*" in settings.allowed_hosts
    assert "tunnel.example" in settings.allowed_hosts
    assert settings.enable_dns_rebinding_protection is True
