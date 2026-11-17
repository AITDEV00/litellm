"""Pins the source-link reverse ingress annotations the tunnel plane needs.

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
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "overlays"
    / "dev"
    / "mcp-source-broker"
    / "reverse-h2-ingress.yaml"
)


@pytest.fixture(scope="module")
def ingress() -> dict:
    return yaml.safe_load(_INGRESS.read_text())


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
        "request buffering would drain the pushed-RPC stream into a disk"
        " buffer instead of streaming it to the sidecar"
    )


def test_tunnel_service_path_is_published(ingress: dict) -> None:
    paths = {
        p["path"]
        for rule in ingress["spec"]["rules"]
        for p in rule["http"]["paths"]
    }
    assert "/grpctunnel.v1.TunnelService/" in paths
