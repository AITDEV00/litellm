"""Pins the source-link tunnel ingress annotations and path set.

The grpctunnel reverse tunnel is one long-lived client-streaming POST whose
body accumulates every pushed RPC request for the tunnel's whole lifetime.
nginx's default client_max_body_size (1m) kills that stream with
"client intended to send too large chunked body" once the cumulative bytes
pass 1MB, which drops every in-flight dispatch as a 502 and forces a tunnel
reconnect. The proxy-body-size: "0" annotation removes the cap for this
ingress only; removing it reintroduces the failure, so this test fails first.

Run with the controller's test extra:

    cd oicm-litellm && uv run --extra test --python 3.13 \
        python -m pytest tests/deploy/test_source_link_ingress.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest
import yaml

_INGRESS: Final = (
    Path(__file__).resolve().parents[2] / "deploy" / "overlays" / "dev" / "mcp-source-broker" / "tunnel-ingress.yaml"
)

_PATCH: Final = (
    Path(__file__).resolve().parents[2] / "deploy" / "overlays" / "dev" / "mcp-source-broker" / "source-link-patch.yaml"
)


@pytest.fixture(scope="module")
def ingress() -> dict:
    return yaml.safe_load(_INGRESS.read_text())


@pytest.fixture(scope="module")
def patch() -> list[dict]:
    return list(yaml.safe_load_all(_PATCH.read_text()))


def test_reverse_tunnel_body_size_is_unlimited(ingress: dict) -> None:
    annotations = ingress["metadata"]["annotations"]
    assert annotations.get("nginx.ingress.kubernetes.io/proxy-body-size") == "0", (
        "the reverse tunnel is one unbounded client-streaming POST; without"
        " proxy-body-size 0, nginx kills it at the 1m default once the pushed"
        " RPC bytes accumulate past 1MB"
    )


def test_tunnel_streaming_annotations_survive(ingress: dict) -> None:
    annotations = ingress["metadata"]["annotations"]
    assert annotations["nginx.ingress.kubernetes.io/backend-protocol"] == "GRPC"
    assert annotations["nginx.ingress.kubernetes.io/proxy-buffering"] == "off"
    assert annotations["nginx.ingress.kubernetes.io/proxy-request-buffering"] == "off", (
        "request buffering would drain the pushed-RPC stream into a disk buffer instead of streaming it to the sidecar"
    )


def test_tunnel_service_path_is_published(ingress: dict) -> None:
    paths = {p["path"] for rule in ingress["spec"]["rules"] for p in rule["http"]["paths"]}
    assert "/grpctunnel.v1.TunnelService/" in paths


def test_tunnel_paths_exclude_the_deleted_reverse_h2_routes(ingress: dict) -> None:
    """The reverse-h2 data plane is deleted, so its routes must not be routed.

    The worker no longer serves those handlers, so a leftover path here sends
    public traffic to a 404 and quietly re-advertises a protocol nobody speaks.
    Publishing only the tunnel route is what keeps the ingress honest.
    """
    paths = {p["path"] for rule in ingress["spec"]["rules"] for p in rule["http"]["paths"]}
    assert paths == {"/grpctunnel.v1.TunnelService/"}, (
        f"the ingress must publish only the grpctunnel route, found {sorted(paths)}"
    )


def test_sidecar_patch_carries_no_transport_variable(patch: list[dict]) -> None:
    """The worker has one plane, so the transport variable would only mislead.

    It is connector-only and vestigial now: the worker ignores it entirely. A
    reader of this manifest should not be told a transport is being selected.
    """
    deployments = [doc for doc in patch if doc.get("kind") == "Deployment"]
    assert len(deployments) == 1, "the patch should carry exactly one Deployment"
    env = {
        entry["name"]
        for container in deployments[0]["spec"]["template"]["spec"]["containers"]
        if container["name"] == "source-link"
        for entry in container.get("env", [])
    }
    assert env, "the source-link container should still declare its env"
    assert "MCP_SOURCE_LINK_TRANSPORT" not in env


def test_names_describe_the_tunnel_not_the_deleted_plane(ingress: dict, patch: list[dict]) -> None:
    """Nothing this overlay publishes should still be named after reverse-h2.

    The ingress resource, its file, and the Service port are the three things a
    reader or an operator types by hand. Leaving any of them named for a deleted
    protocol makes the surviving name look like a leftover that is safe to drop.
    """
    assert ingress["metadata"]["name"] == "litellm-mcp-tunnel-dev"
    assert _INGRESS.name == "tunnel-ingress.yaml"
    paths = ingress["spec"]["rules"][0]["http"]["paths"]
    assert {p["backend"]["service"]["port"]["name"] for p in paths} == {"link-tunnel"}
    services = [doc for doc in patch if doc.get("kind") == "Service"]
    assert len(services) == 1, "the patch should carry exactly one Service"
    ports = {port["name"] for port in services[0]["spec"]["ports"]}
    assert "link-tunnel" in ports and "link-reverse" not in ports
