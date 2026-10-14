#!/usr/bin/env python3
"""Build manifest.json (schema 4) from the staged mirror content.

Reads the staging directory, computes SHA-256 for every artifact and harness
preset, and writes a manifest.json conforming to
docs/client-distribution/mirror-content.md (schema 4).

Schema 4 pins each harness to a versioned preset:
  harnesses: { <app-id>: {version, path, sha256, mode} }
  - mode "switch":   field-mode harness (claude/codex/gemini), no preset file.
  - mode "additive": raw-config harness; path/sha256 point at the staged preset.

Usage: python3 build-manifest.py <staging-dir>
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

CC_SWITCH_VERSION = "5.11.1"

# cc-switch mirror targets -> archive extension
CC_SWITCH_TARGETS = {
    "linux-x64-musl": "tar.gz",
    "linux-arm64-musl": "tar.gz",
    "darwin-universal": "tar.gz",
    "windows-x64": "zip",
}

# Harness preset slices. version bumps when the preset shape changes; the
# manifest pins the new path so old installers keep working.
# mode "switch"   -> field-mode provider add (no preset file needed)
# mode "additive" -> --config-file provider add (preset file required)
HARNESSES = {
    "claude": {"version": 1, "mode": "switch"},
    "codex": {"version": 1, "mode": "switch"},
    "gemini": {"version": 1, "mode": "switch"},
    "open-code": {"version": 1, "mode": "additive"},
    "pi": {"version": 1, "mode": "additive"},
    "hermes": {"version": 1, "mode": "additive"},
    "open-claw": {"version": 1, "mode": "additive"},
}

PROVIDER = "oicm"


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
        "schema": 4,
        "cc_switch_version": CC_SWITCH_VERSION,
        "platforms": [
            {"target": t, "archive_ext": ext} for t, ext in CC_SWITCH_TARGETS.items()
        ],
        "cc_switch_artifacts": {},
        "harnesses": {},
    }

    # cc-switch binary artifacts
    ccs_dir = staging / "cc-switch" / CC_SWITCH_VERSION
    for target, ext in CC_SWITCH_TARGETS.items():
        artifact = f"cc-switch-cli-v{CC_SWITCH_VERSION}-{target}.{ext}"
        artifact_path = ccs_dir / artifact
        if not artifact_path.exists():
            print(f"error: missing {artifact_path}", file=sys.stderr)
            sys.exit(1)
        manifest["cc_switch_artifacts"][target] = {
            "file": artifact,
            "sha256": sha256_file(artifact_path),
        }

    # Harness presets
    for harness, meta in HARNESSES.items():
        if meta["mode"] == "switch":
            manifest["harnesses"][harness] = {
                "version": meta["version"],
                "mode": "switch",
            }
            continue
        version = meta["version"]
        preset_rel = f"providers/{PROVIDER}/{harness}/v{version}.json"
        preset_path = staging / preset_rel
        if not preset_path.exists():
            print(f"error: missing preset {preset_path}", file=sys.stderr)
            sys.exit(1)
        manifest["harnesses"][harness] = {
            "version": version,
            "path": preset_rel,
            "sha256": sha256_file(preset_path),
            "mode": "additive",
        }

    out = staging / "manifest.json"
    with open(out, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    print(f"manifest written: {out}")
    print(f"  schema: {manifest['schema']}")
    print(f"  cc-switch artifacts: {len(manifest['cc_switch_artifacts'])}")
    additive = sum(1 for h in manifest["harnesses"].values() if h["mode"] == "additive")
    print(f"  harnesses: {len(manifest['harnesses'])} ({additive} additive presets)")


if __name__ == "__main__":
    main()
