"""Pins the gateway ingress buffering annotations to valid nginx values.

ingress-nginx accepts only ``on`` or ``off`` for ``proxy-buffering`` and
``proxy-request-buffering``. Any other spelling, ``"false"`` most notoriously,
is silently ignored and nginx falls back to its default ``on``. That made the
delivery tier buffer every request body in full before forwarding, which is the
root cause of the transcription burst latency in
docs/incidents/2026-11-18-nemotron-gateway-transcription-latency/incident-report.md.

The prod ingress and its rollback copy are pinned here together: a rollback that
reverted one without the other would silently reintroduce the buffering bug.

Run with the controller's test extra:

    cd oicm-litellm-layer && uv run --extra test --python 3.13 \
        python -m pytest tests/deploy/test_ingress_buffering.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest
import yaml

_LAYER_ROOT: Final = Path(__file__).resolve().parents[2]

# Every manifest that carries the gateway ingress annotations. The rollback copy
# is included on purpose so a rollback cannot reintroduce the buffering bug.
_INGRESS_MANIFESTS: Final = (
    _LAYER_ROOT / "deploy" / "prod" / "litellm-ingress.yaml",
    _LAYER_ROOT / "deploy" / "rollback" / "access.yaml",
)

_BUFFERING_ANNOTATIONS: Final = (
    "nginx.ingress.kubernetes.io/proxy-buffering",
    "nginx.ingress.kubernetes.io/proxy-request-buffering",
)


def _ingresses(path: Path) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(path.read_text()) if doc and doc.get("kind") == "Ingress"]


@pytest.mark.parametrize("manifest", _INGRESS_MANIFESTS, ids=lambda p: str(p.relative_to(_LAYER_ROOT)))
def test_buffering_annotations_use_valid_off(manifest: Path) -> None:
    ingresses = _ingresses(manifest)
    assert ingresses, f"{manifest} declares no Ingress"
    for ingress in ingresses:
        annotations = ingress["metadata"]["annotations"]
        for key in _BUFFERING_ANNOTATIONS:
            assert annotations.get(key) == "off", (
                f'{manifest.name}: {key} must be "off" (nginx ignores any other value, so'
                f' "false" leaves buffering on and serializes the delivery tier), got'
                f" {annotations.get(key)!r}"
            )
