#!/usr/bin/env python3
"""Print a cache-key for the native Rust build inputs.

`litellm-src-build` caches `litellm-rust/target` across builds so a Python-only
edit does not recompile the whole Rust workspace. That cache is only safe when
the Rust inputs are byte-identical, because `podman build --timestamp` pins
source mtimes to a fixed epoch: a cached artifact then looks newer than an
edited `.rs` file, and cargo would skip recompiling it.

Hashing the Rust inputs and using the digest in the cache id prevents that. A
Rust edit gets a fresh cache directory, so the native module is rebuilt.

Usage:
    python3 scripts/rust_cache_key.py            # print the digest
    python3 scripts/rust_cache_key.py --path DIR # print the cache directory
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

INPUT_ROOTS = (
    "litellm-rust/Cargo.toml",
    "litellm-rust/Cargo.lock",
    "litellm-rust/crates",
    "rust-toolchain.toml",
    ".cargo/config.toml",
    "pyproject.toml",
)

INCLUDED_SUFFIXES = frozenset({".rs", ".toml", ".lock", ".json", ".proto", ".pyi"})
INCLUDED_NAMES = frozenset({"Cargo.lock", "build.rs"})


def _native_inputs(repo_root: Path) -> list[Path]:
    files: list[Path] = []
    for root in INPUT_ROOTS:
        path = repo_root / root
        if path.is_file():
            files.append(path)
            continue
        if not path.is_dir():
            continue
        files.extend(
            candidate
            for candidate in path.rglob("*")
            if candidate.is_file()
            and "target" not in candidate.parts
            and (candidate.suffix in INCLUDED_SUFFIXES or candidate.name in INCLUDED_NAMES)
        )
    return sorted(files, key=lambda p: p.relative_to(repo_root).as_posix())


def rust_cache_key(repo_root: Path = REPO_ROOT) -> str:
    digest = hashlib.sha256()
    for path in _native_inputs(repo_root):
        relative = path.relative_to(repo_root).as_posix()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def cache_directory(cache_root: Path, key: str) -> Path:
    return cache_root / key


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--path", type=Path, help="cache root; print the keyed directory")
    args = parser.parse_args()
    try:
        key = rust_cache_key(args.repo_root)
    except OSError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(cache_directory(args.path, key) if args.path else key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
