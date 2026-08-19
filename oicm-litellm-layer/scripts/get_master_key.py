"""Print the LiteLLM master key from the single source of truth.

The authoritative value lives in the inline `litellm-master-key` Secret in
`deploy/prod/litellm-proxy.yaml`. This script lets local tooling (the Makefile, env
templates, benchmarks) derive the key from that one manifest instead of
hardcoding a second copy, so a rotation in the manifest propagates everywhere.

Stdlib-only (no third-party deps) so it runs under any `python3`, including in
`.env` files sourced by `make` targets and in benchmark scripts.

Usage:
    python3 scripts/get_master_key.py           # print the value
    python3 scripts/get_master_key.py --export  # print `export LITELLM_MASTER_KEY=<value>`
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

MANIFEST = Path(__file__).resolve().parent.parent / "deploy" / "litellm-proxy.yaml"
SECRET_NAME = "litellm-master-key"
SECRET_KEY = "master-key"


def master_key_from_manifest(manifest: Path = MANIFEST) -> str:
    text = manifest.read_text(encoding="utf-8")
    # Split on `---` YAML document separators and find the named Secret's block,
    # so a `master-key:` value elsewhere in the file cannot be mistaken for it.
    for block in re.split(r"^---\s*$", text, flags=re.MULTILINE):
        if f"name: {SECRET_NAME}" not in block:
            continue
        # Restrict to the Secret's own stringData scope: find the value only
        # after the last `stringData:` line within this block.
        string_data_index = block.rfind("stringData:")
        if string_data_index == -1:
            continue
        scope = block[string_data_index:]
        match = re.search(
            rf"^\s*{re.escape(SECRET_KEY)}:\s*(\S.*?)\s*$",
            scope,
            re.MULTILINE,
        )
        if match:
            return match.group(1).strip()
    raise RuntimeError(
        f"Master key {SECRET_KEY!r} not found in Secret {SECRET_NAME!r} in {manifest}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", action="store_true", help="Emit an `export LITELLM_MASTER_KEY=` line")
    args = parser.parse_args()

    try:
        value = master_key_from_manifest()
    except (OSError, RuntimeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.export:
        print(f"export LITELLM_MASTER_KEY={value}")
    else:
        print(value)
    return 0


if __name__ == "__main__":
    sys.exit(main())