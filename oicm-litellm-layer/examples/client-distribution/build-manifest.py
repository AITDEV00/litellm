#!/usr/bin/env python3
"""Build manifest.json (schema 5) from the staged mirror content.

Reads the staging directory, computes SHA-256 for every artifact and harness
preset, and writes a manifest.json conforming to
docs/client-distribution/mirror-content.md (schema 5).

Schema 5 pins each harness to a versioned preset:
  harnesses: { <app-id>: {version, path, sha256} }
Every harness carries one; the settings_config shape lives in the preset
(canonical per-harness skeletons with sentinels), not in the installer.
The optional `max_output_tokens` field is the default per-request output cap
the installer writes into harness configs with a real output knob
(claude env key, opencode limit.output).

Usage: python3 build-manifest.py <staging-dir>
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

# Fork release (github.com/AITDEV00/cc-switch-cli). Version strings must be
# lowercase alnum + dashes for the versioned mirror path; jya0- prefix marks a
# fork build carrying the --overwrite upsert. Keep in sync with sync-mirror.sh
CC_SWITCH_VERSION = "jya0-5.11.1"

# cc-switch mirror targets -> archive extension
CC_SWITCH_TARGETS = {
    "linux-x64-musl": "tar.gz",
    "linux-arm64-musl": "tar.gz",
    "darwin-universal": "tar.gz",
    "windows-x64": "zip",
}

# Harness preset versions. A version bumps when that harness's preset shape
# changes; the manifest pins the new path so old installers keep working.
# Every harness carries a preset file — the shape it encodes is the
# harness's own business, so there is no mode split. v2 adds per-model
# context windows and the default output cap where the harness has a real
# knob for them. v3 (codex only) adds the modelCatalog block that makes
# cc-switch emit Codex's model_catalog_json + generated catalog, and drops
# the dead disable_response_storage TOML key Codex 0.165.0 warns about.
HARNESSES = {
    "claude": 2,
    "codex": 3,
    "gemini": 2,
    "open-code": 2,
    "pi": 2,
    "hermes": 2,
    "open-claw": 2,
}

PROVIDER = "adeo"

# Default per-request output cap written into harness configs that support
# one. Change here (or edit the published manifest) and re-sync; the
# installer picks it up with no gateway rebuild.
MAX_OUTPUT_TOKENS = 65536


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
        "schema": 5,
        "cc_switch_version": CC_SWITCH_VERSION,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "platforms": [
            {"target": t, "archive_ext": ext} for t, ext in CC_SWITCH_TARGETS.items()
        ],
        "cc_switch_artifacts": {},
        "harnesses": {},
    }

    # cc-switch binary artifacts
    ccs_dir = staging / "cc-switch" / CC_SWITCH_VERSION
    for target, ext in CC_SWITCH_TARGETS.items():
        artifact = f"cc-switch-cli-{CC_SWITCH_VERSION}-{target}.{ext}"
        artifact_path = ccs_dir / artifact
        if not artifact_path.exists():
            print(f"error: missing {artifact_path}", file=sys.stderr)
            sys.exit(1)
        manifest["cc_switch_artifacts"][target] = {
            "file": artifact,
            "sha256": sha256_file(artifact_path),
        }

    # Harness presets
    for harness, version in HARNESSES.items():
        preset_rel = f"providers/{PROVIDER}/{harness}/v{version}.json"
        preset_path = staging / preset_rel
        if not preset_path.exists():
            print(f"error: missing preset {preset_path}", file=sys.stderr)
            sys.exit(1)
        manifest["harnesses"][harness] = {
            "version": version,
            "path": preset_rel,
            "sha256": sha256_file(preset_path),
        }

    out = staging / "manifest.json"
    with open(out, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    print(f"manifest written: {out}")
    print(f"  schema: {manifest['schema']}")
    print(f"  cc-switch artifacts: {len(manifest['cc_switch_artifacts'])}")
    print(f"  harness presets: {len(manifest['harnesses'])}")


if __name__ == "__main__":
    main()
