"""Prod image pins must name a manifest digest the runtime can resolve.

A prod pin is `<repo>:<tag>@sha256:<digest>`. The kubelet resolves that digest
against the registry, so it must be the MANIFEST digest. The mistake this guards
against happened for real: the pin carried the config-blob digest that
`podman image inspect --format '{{.Digest}}'` prints, every pod went to
ErrImagePull with `not found`, and the rollout could not progress.

These checks are offline on purpose. They assert the shape of the pin and that
the Makefile routes prod pinning through the manifest-digest helper, not that a
particular digest value is correct (that changes every build and is not ours to
pin).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

_LAYER_ROOT: Final = Path(__file__).resolve().parents[2]
_PROD_GATEWAY: Final = _LAYER_ROOT / "deploy" / "overlays" / "prod" / "gateway" / "deployment.yaml"
_PROD_CONTROLLER: Final = _LAYER_ROOT / "deploy" / "prod" / "discovery-controller.yaml"
_MAKEFILE: Final = _LAYER_ROOT / "Makefile"

_IMAGE_LINE: Final = re.compile(r"^\s+image:\s*(\S+)\s*$", re.MULTILINE)
_DIGEST_SUFFIX: Final = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")


def _image_refs(path: Path) -> list[str]:
    return _IMAGE_LINE.findall(path.read_text())


@pytest.mark.parametrize("manifest", [_PROD_GATEWAY, _PROD_CONTROLLER], ids=["gateway", "controller"])
def test_prod_pin_names_a_manifest_digest(manifest: Path) -> None:
    """Every image reference in a prod manifest is tag@sha256:<64 hex>."""
    refs = _image_refs(manifest)
    assert refs, f"no image references found in {manifest}"
    bare = [ref for ref in refs if not _DIGEST_SUFFIX.match(ref)]
    assert not bare, (
        f"{manifest.name} has image references without a sha256 digest: {bare}. "
        "A bare prod tag follows a rebuild and is not reproducible."
    )


@pytest.mark.parametrize("manifest", [_PROD_GATEWAY, _PROD_CONTROLLER], ids=["gateway", "controller"])
def test_prod_pin_does_not_carry_a_config_blob_digest(manifest: Path) -> None:
    """The digest must come from the registry manifest, not `podman image inspect`.

    Both are `sha256:` followed by 64 hex, so the shape alone cannot tell them
    apart. What can be checked offline is provenance: the Makefile's prod pin
    macro must call the manifest-digest helper. This test documents that
    requirement next to the manifest it protects.
    """
    makefile = _MAKEFILE.read_text()
    assert "image_manifest_digest.py" in makefile, (
        "the Makefile no longer resolves digests via scripts/image_manifest_digest.py, "
        "which reads the registry Docker-Content-Digest header"
    )
    assert "pin_prod_image_with_digest" in makefile, (
        "prod pinning must go through pin_prod_image_with_digest so a tag is never written without its manifest digest"
    )


def test_prod_pin_targets_use_the_digest_macro() -> None:
    """The prod pin targets must not fall back to the bare `pin_image` macro."""
    makefile = _MAKEFILE.read_text()
    for target in ("controller-pin-prod", "litellm-src-pin-prod"):
        block = _recipe_for(makefile, target)
        assert "pin_prod_image_with_digest" in block, (
            f"{target} does not use pin_prod_image_with_digest, so it would write a bare tag and strip the digest"
        )
        assert "pin_image," not in block, f"{target} uses the bare pin_image macro, which strips the digest"


def _recipe_for(makefile_text: str, target: str) -> str:
    """Return the recipe lines belonging to `target`."""
    lines = makefile_text.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if line.startswith(f"{target}:"):
            start = index + 1
            break
    if start is None:
        raise AssertionError(f"target {target} not found in the Makefile")
    recipe: list[str] = []
    for line in lines[start:]:
        if line and not line.startswith(("\t", " ")):
            break
        recipe.append(line)
    return "\n".join(recipe)
