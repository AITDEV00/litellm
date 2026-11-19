from __future__ import annotations

import pytest
from pydantic import ValidationError

from litellm.proxy.mcp_source_broker.models import SourceCreate, Target


def test_alias_rejects_the_tool_name_separator() -> None:
    """The alias becomes the MCP server name, which prefixes every tool name.

    '-'(MCP_TOOL_PREFIX_SEPARATOR) would make that prefix ambiguous, so it has
    to be rejected at the schema even though it is a natural alias character.
    """
    base = {
        "owner": "alice",
        "target": Target(),
        "spec": '{"openapi":"3.1.0","paths":{"/v1/pets":{"get":{}}}}',
    }
    assert SourceCreate(alias="pets_api", **base).alias == "pets_api"
    with pytest.raises(ValidationError):
        SourceCreate(alias="pets-api", **base)


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "10.34.104.99",
        "fd00::1",
        "::1",
        "litellm-dev.adeo-litellm.svc",
        "my-svc.ns.svc",
    ],
)
def test_target_accepts_ip_and_service_hosts(host: str) -> None:
    """A source may be a private IP or a Kubernetes service, not only loopback.

    The gateway records the target the connector dials; it must accept the shapes
    the connector can register without becoming a URL or public-hostname field.
    """
    assert Target(host=host).host == host


@pytest.mark.parametrize(
    "host",
    [
        "http://127.0.0.1",
        "example.com",
        "user@10.0.0.1",
        "10.0.0.1/admin",
        "litellm-dev.adeo-litellm",
        "*.svc",
        "",
    ],
)
def test_target_rejects_urls_and_public_hosts(host: str) -> None:
    """A URL, userinfo, path, bare public name or wildcard is not a valid target."""
    with pytest.raises(ValueError):
        Target(host=host)


def test_target_port_bounds() -> None:
    with pytest.raises(ValueError):
        Target(host="127.0.0.1", port=0)
    with pytest.raises(ValueError):
        Target(host="127.0.0.1", port=65536)
    assert Target(host="127.0.0.1", port=65535).port == 65535


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("::ffff:127.0.0.1", "127.0.0.1"),
        ("::ffff:7f00:1", "127.0.0.1"),
        ("::ffff:10.34.104.99", "10.34.104.99"),
        ("0:0:0:0:0:0:0:1", "::1"),
        ("FD00:0:0:0:0:0:0:1", "fd00::1"),
    ],
)
def test_target_normalizes_like_the_go_connector(raw: str, canonical: str) -> None:
    """The gateway must normalize a host exactly as the Go connector's target.Parse.

    An IPv4-mapped IPv6 literal collapses to plain IPv4, and IPv6 is compressed, so
    two spellings of one origin persist as one identity rather than deriving two
    aliases that disagree across the gateway/connector boundary.
    """
    assert Target(host=raw).host == canonical


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "255.255.255.255", "224.0.0.1", "ff02::1", "fe80::1", "169.254.1.1"])
def test_target_rejects_non_unicast_addresses(host: str) -> None:
    """A source origin is a specific unicast host: an unspecified, multicast,
    broadcast, or link-local address must be refused on the gateway as it is on the
    connector, so neither side stores a target the other would deny."""
    with pytest.raises(ValueError):
        Target(host=host)
