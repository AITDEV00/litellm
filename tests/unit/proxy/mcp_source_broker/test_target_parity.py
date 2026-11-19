"""Cross-language parity for the MCP source target grammar.

The gateway (Python, ``canonical_worker_host``) and the connector (Go,
``target.Parse``) must agree on every target spelling: each side's accepted value
must canonicalize to the exact host:port the other side would produce, and each
side's rejection must be shared, or a row written by one boundary becomes an
unreadable or duplicated identity at the other.

This drives the REAL Go parser: it compiles a tiny program that imports the
connector's target package and prints the canonical form for each input, then
asserts Python's validator agrees case by case. It skips when the Go toolchain or
the connector module is unavailable so the suite still runs on a Python-only box.

Run: .venv/bin/python -m pytest -q tests/unit/proxy/mcp_source_broker/test_target_parity.py
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Final

import pytest

from litellm.proxy.mcp_source_broker.models import Target, canonical_hostport


def _go_binary(tmp: Path) -> Path | None:
    """Build the connector's targetparity command, or None if the toolchain is unusable.

    The helper lives in the Go module (cmd/targetparity) because only code inside
    the module may import its internal target package.
    """
    go: Final = _find_go()
    if go is None:
        return None
    module: Final = _find_go_module()
    if module is None:
        return None
    command_dir: Final = module / "cmd" / "targetparity"
    if not (command_dir / "main.go").exists():
        return None
    binary: Final = tmp / "targetparity"
    env: dict[str, str] = {
        "PATH": "/usr/bin:/bin",
        "GOFLAGS": "-mod=mod",
        "GO111MODULE": "on",
        "HOME": str(tmp),
    }
    cache: Final = Path("/tmp/rd-gopath/pkg/mod")
    if cache.exists():
        env["GOMODCACHE"] = str(cache)
        env["GOPATH"] = str(cache.parent)
        env["GOPROXY"] = "off"
    build: Final = subprocess.run(
        [go, "build", "-o", str(binary), "./cmd/targetparity"],
        cwd=module,
        env=env,
        capture_output=True,
        text=True,
    )
    if build.returncode != 0:
        return None
    return binary


def _find_go() -> str | None:
    return next(
        (candidate for candidate in ("/tmp/sa-build/bin/go", "/usr/local/go/bin/go") if Path(candidate).exists()), None
    )


def _find_go_module() -> Path | None:
    """Walk up from this test file to the repo root and locate the Go module."""
    current: Final = Path(__file__).resolve()
    module: Final = next(
        (
            candidate
            for parent in current.parents
            if (candidate := parent / "oicm-litellm-layer" / "mcp-source-link" / "go.mod").exists()
        ),
        None,
    )
    return module.parent if module is not None else None


# One shared grammar, one case per row: (input host:port, expected canonical).
# Every Go-accepted spelling must persist one identity in Python, and every
# Python-accepted value must be exactly what Go parses.
PARITY_CASES: Final = (
    # IPv4 literals, mapped forms, and private addresses.
    ("127.0.0.1:18080", "127.0.0.1:18080"),
    ("10.34.104.99:4000", "10.34.104.99:4000"),
    # IPv4-mapped IPv6 collapses to IPv4 on both sides.
    ("[::ffff:127.0.0.1]:8080", "127.0.0.1:8080"),
    ("[::ffff:7f00:1]:8080", "127.0.0.1:8080"),
    # IPv6 compressed and expanded spellings collapse to one identity.
    ("[::1]:8080", "[::1]:8080"),
    ("[0:0:0:0:0:0:0:1]:8080", "[::1]:8080"),
    ("[fd00::1]:4000", "[fd00::1]:4000"),
    # Service spellings: short, full cluster, local, uppercase, terminal dot.
    ("litellm-dev.adeo-litellm.svc:4000", "litellm-dev.adeo-litellm.svc:4000"),
    ("litellm-dev.adeo-litellm.svc.cluster.local:4000", "litellm-dev.adeo-litellm.svc:4000"),
    ("litellm-dev.adeo-litellm.svc.local:4000", "litellm-dev.adeo-litellm.svc:4000"),
    ("LITELLM-DEV.ADEO-LITELLM.SVC:4000", "litellm-dev.adeo-litellm.svc:4000"),
    ("litellm-dev.adeo-litellm.svc.:4000", "litellm-dev.adeo-litellm.svc:4000"),
)

# Rejections that must be shared, so neither side stores what the other refuses.
SHARED_REJECTIONS: Final = (
    # Unspecified, broadcast, multicast, link-local.
    "0.0.0.0:8080",
    "[::]:8080",
    "255.255.255.255:8080",
    "224.0.0.1:8080",
    "[ff02::1]:8080",
    "[fe80::1]:8080",
    "169.254.1.1:8080",
    # Scoped IPv6 zones: Python accepted these while Go refused them.
    "[::1%eth0]:8080",
    "[fd00::1%eth0]:8080",
    # Public hostnames are not service targets on either side.
    "example.com:8080",
)


def _split(hostport: str) -> tuple[str, int]:
    host, _, port = hostport.rpartition(":")
    host = host.removeprefix("[").removesuffix("]")
    return host, int(port)


def _go_canonical(binary: Path, values: tuple[str, ...]) -> list[str]:
    run: Final = subprocess.run(
        [str(binary)],
        input="\n".join(values),
        capture_output=True,
        text=True,
    )
    if run.returncode != 0:
        pytest.fail(f"go parity helper failed: {run.stderr}")
    return run.stdout.splitlines()


def test_go_and_python_agree_on_every_target_spelling(tmp_path: Path) -> None:
    binary: Path | None = _go_binary(tmp_path)
    if binary is None:
        pytest.skip("Go toolchain or connector module unavailable; parity not run")
    inputs: Final = PARITY_CASES and tuple(value for value, _ in PARITY_CASES)
    go_results: Final = _go_canonical(binary, inputs)
    for (value, expected), got in zip(PARITY_CASES, go_results, strict=True):
        host, port = _split(value)
        python_target: Target = Target(host=host, port=port)
        python_canonical: str = (
            f"[{python_target.host}]:{python_target.port}"
            if ":" in python_target.host
            else f"{python_target.host}:{python_target.port}"
        )
        assert got == expected, f"Go parsed {value!r} as {got!r}, want {expected!r}"
        assert python_canonical == expected, (
            f"Python canonicalized {value!r} as {python_canonical!r}, want {expected!r}"
        )


def test_go_and_python_share_rejections(tmp_path: Path) -> None:
    binary: Path | None = _go_binary(tmp_path)
    if binary is None:
        pytest.skip("Go toolchain or connector module unavailable; parity not run")
    go_results: Final = _go_canonical(binary, SHARED_REJECTIONS)
    for value, got in zip(SHARED_REJECTIONS, go_results, strict=True):
        assert got == "REJECT", f"Go accepted {value!r}; the shared grammar says reject"
        host, port = _split(value)
        with pytest.raises(ValueError):
            Target(host=host, port=port)


def test_canonical_hostport_round_trips_through_go(tmp_path: Path) -> None:
    """The persisted hostport string must re-parse in Go to the same identity."""
    binary: Path | None = _go_binary(tmp_path)
    if binary is None:
        pytest.skip("Go toolchain or connector module unavailable; parity not run")
    rendered: Final = tuple(canonical_hostport(host, port) for host, port in (_split(v) for v, _ in PARITY_CASES))
    go_results: Final = _go_canonical(binary, rendered)
    for (value, expected), rendered_value, got in zip(PARITY_CASES, rendered, go_results, strict=True):
        assert got == expected, (
            f"Go re-parsed the persisted form {rendered_value!r} of {value!r} as {got!r}, want {expected!r}"
        )
