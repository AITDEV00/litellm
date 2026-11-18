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


import pytest


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
