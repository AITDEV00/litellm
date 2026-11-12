"""The native Rust cache must be keyed by content, not by mtime.

`litellm-src-build` passes `--timestamp` to podman so unchanged layers keep the
same digest. That pins source mtimes to a fixed epoch, which means a warm
`litellm-rust/target` cache would look newer than an edited `.rs` file and cargo
would skip recompiling it. The cache key therefore has to change when any Rust
input changes.

Run with the controller's test extra:

    cd oicm-litellm-layer && uv run --extra test --python 3.13 \
        python -m pytest tests/controller/test_rust_cache_key.py
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from scripts.rust_cache_key import rust_cache_key

_REPO_ROOT: Final = Path(__file__).resolve().parents[3]


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _minimal_repo(root: Path) -> None:
    _write(root, "litellm-rust/Cargo.toml", "[workspace]\n")
    _write(root, "litellm-rust/crates/bridge/Cargo.toml", "[package]\n")
    _write(root, "litellm-rust/crates/bridge/src/lib.rs", "pub fn value() -> u8 { 1 }\n")
    _write(root, "pyproject.toml", "[project]\nname = 'litellm'\n")


def test_key_is_stable_when_content_is_unchanged(tmp_path: Path) -> None:
    _minimal_repo(tmp_path)

    assert rust_cache_key(tmp_path) == rust_cache_key(tmp_path)


def test_editing_a_rust_source_changes_the_key(tmp_path: Path) -> None:
    _minimal_repo(tmp_path)
    before = rust_cache_key(tmp_path)

    _write(tmp_path, "litellm-rust/crates/bridge/src/lib.rs", "pub fn value() -> u8 { 2 }\n")

    assert rust_cache_key(tmp_path) != before


def test_editing_a_cargo_manifest_changes_the_key(tmp_path: Path) -> None:
    _minimal_repo(tmp_path)
    before = rust_cache_key(tmp_path)

    _write(tmp_path, "litellm-rust/crates/bridge/Cargo.toml", "[package]\nname = 'bridge'\n")

    assert rust_cache_key(tmp_path) != before


def test_build_artifacts_do_not_change_the_key(tmp_path: Path) -> None:
    _minimal_repo(tmp_path)
    before = rust_cache_key(tmp_path)

    _write(tmp_path, "litellm-rust/target/release/output.so", "compiled output")
    _write(tmp_path, "litellm/rust_bridge/_native.abi3.so", "compiled extension")

    assert rust_cache_key(tmp_path) == before


def test_moving_the_workspace_changes_the_key(tmp_path: Path) -> None:
    _minimal_repo(tmp_path)
    before = rust_cache_key(tmp_path)

    (tmp_path / "litellm-rust/crates/bridge/src/lib.rs").rename(tmp_path / "litellm-rust/crates/bridge/src/other.rs")

    assert rust_cache_key(tmp_path) != before
