#!/usr/bin/env python3
# ruff: noqa: T201  -- this is a CLI that prints one digest on stdout for make to capture
"""Resolve the registry MANIFEST digest for an image reference.

Prints the digest that a container runtime resolves when it pulls
`<repo>:<tag>`, so it can be pinned as `<repo>:<tag>@sha256:...` in a manifest.

This exists because the obvious shortcuts give the wrong value:

- `podman image inspect --format '{{.Digest}}'` prints the CONFIG BLOB digest,
  not the manifest digest. Pinning that makes the kubelet fail the pull with
  `NotFound`, because no manifest in the registry carries that digest. That is
  the ImagePullBackOff this script prevents.
- `podman manifest inspect` refuses a single-arch image
  ("Treating single images as manifest lists is not implemented").
- `skopeo inspect` is the right tool but is not installed on every build box.

So the registry's own `Docker-Content-Digest` response header is the source of
truth. That header is exactly what the runtime uses.

Usage:
    python3 scripts/image_manifest_digest.py <registry>/<repo>:<tag>

Exit codes: 0 got a digest, 1 could not (message on stderr).
"""

from __future__ import annotations

import base64
import os
import ssl
import sys
import urllib.error
import urllib.request

_MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    )
)


def _split_ref(ref: str) -> tuple[str, str, str]:
    """Split `<registry>/<repo>:<tag>` into (registry, repo, tag)."""
    if "/" not in ref:
        raise ValueError(f"reference has no registry: {ref!r}")
    registry, _, rest = ref.partition("/")
    if ":" not in rest:
        raise ValueError(f"reference has no tag: {ref!r}")
    repo, _, tag = rest.rpartition(":")
    if not repo or not tag:
        raise ValueError(f"reference is missing a repo or tag: {ref!r}")
    return registry, repo, tag


def _basic_auth() -> str | None:
    """Registry credentials from the same env vars the Makefile exports."""
    user = os.getenv("HARBOR_USER")
    password = os.getenv("HARBOR_PASS")
    if not user or not password:
        return None
    return base64.b64encode(f"{user}:{password}".encode()).decode()


def manifest_digest(ref: str) -> str:
    registry, repo, tag = _split_ref(ref)
    url = f"https://{registry}/v2/{repo}/manifests/{tag}"
    request = urllib.request.Request(
        url,
        method="HEAD",
        headers={"Accept": _MANIFEST_ACCEPT},
    )
    auth = _basic_auth()
    if auth is not None:
        request.add_header("Authorization", f"Basic {auth}")

    # The internal Harbor uses a private CA, so verification is relaxed to match
    # the `--tls-verify=false` every other target here uses.
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(request, timeout=30, context=context) as response:
        digest = response.headers.get("Docker-Content-Digest")
    if not digest:
        raise RuntimeError(f"registry returned no Docker-Content-Digest for {ref}")
    return digest.strip()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.split("Usage:", 1)[1].strip(), file=sys.stderr)
        return 1
    try:
        print(manifest_digest(argv[1]))
    except (urllib.error.URLError, ValueError, RuntimeError) as exc:
        print(f"could not resolve manifest digest for {argv[1]}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
