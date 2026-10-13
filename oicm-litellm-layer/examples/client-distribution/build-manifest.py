#!/usr/bin/env python3
"""Build manifest.json from the staged mirror content.

Reads the staging directory, computes SHA-256 for every artifact, and writes
a manifest.json conforming to the schema in docs/client-distribution/mirror-content.md.

Usage: python3 build-manifest.py <staging-dir>
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

UV_VERSION = "0.10.9"
LITELLM_CLI_VERSION = "1.102.0"
PYTHON_VERSION = "3.13.12"
PYTHON_BUILD_TAG = "20260303"

UV_TARGETS = [
    "x86_64-unknown-linux-gnu",
    "x86_64-unknown-linux-musl",
    "aarch64-unknown-linux-gnu",
    "aarch64-unknown-linux-musl",
    "x86_64-apple-darwin",
    "aarch64-apple-darwin",
    "x86_64-pc-windows-msvc",
    "aarch64-pc-windows-msvc",
]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <staging-dir>", file=sys.stderr)
        sys.exit(1)

    staging = Path(sys.argv[1])
    if not staging.is_dir():
        print(f"error: {staging} is not a directory", file=sys.stderr)
        sys.exit(1)

    manifest: dict = {
        "schema": 2,
        "litellm_cli_version": LITELLM_CLI_VERSION,
        "uv_version": UV_VERSION,
        "python_version": PYTHON_VERSION,
        "python_build_tag": PYTHON_BUILD_TAG,
        "platforms": [
            {
                "target": t,
                "archive_ext": "zip" if "windows" in t else "tar.gz",
            }
            for t in UV_TARGETS
        ],
        "uv_artifacts": {},
        "python_artifacts": {"sha256": {}},
        "wheelhouse": {"sha256": ""},
    }

    # uv artifacts
    uv_dir = staging / "uv" / UV_VERSION
    for target in UV_TARGETS:
        ext = "zip" if "windows" in target else "tar.gz"
        artifact = f"uv-{target}.{ext}"
        artifact_path = uv_dir / artifact
        if not artifact_path.exists():
            print(f"error: missing {artifact_path}", file=sys.stderr)
            sys.exit(1)
        manifest["uv_artifacts"][target] = {
            "file": artifact,
            "sha256": sha256_file(artifact_path),
        }

    # python-build-standalone archives (all .tar.gz)
    pbs_dir = staging / "python"
    pbs_hashes: dict[str, str] = {}
    for target in UV_TARGETS:
        pbs_file = None
        for f in pbs_dir.iterdir():
            if target in f.name and f.name.endswith(".tar.gz"):
                pbs_file = f
                break
        if pbs_file is None:
            print(f"warning: no python archive found for {target}", file=sys.stderr)
            continue
        pbs_hashes[target] = sha256_file(pbs_file)
    manifest["python_artifacts"]["sha256"] = pbs_hashes

    # Wheelhouse: single hash over sorted wheel hashes
    pypi_dir = staging / "pypi"
    wheel_hashes: list[str] = []
    for whl in sorted(pypi_dir.glob("*.whl")):
        wheel_hashes.append(sha256_file(whl))
    if wheel_hashes:
        combined = hashlib.sha256("\n".join(wheel_hashes).encode()).hexdigest()
        manifest["wheelhouse"]["sha256"] = combined
    else:
        print("warning: no wheels found in pypi/", file=sys.stderr)

    out = staging / "manifest.json"
    with open(out, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    print(f"manifest written: {out}")
    print(f"  schema: {manifest['schema']}")
    print(f"  platforms: {len(manifest['platforms'])}")
    print(f"  uv artifacts: {len(manifest['uv_artifacts'])}")
    print(f"  python archives: {len(pbs_hashes)}")
    print(f"  wheels: {len(wheel_hashes)}")


if __name__ == "__main__":
    main()
