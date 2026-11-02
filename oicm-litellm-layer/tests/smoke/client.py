"""HTTP client and resource cleanup for the smoke suite.

Thin typed layer over httpx, borrowing the error-as-value shape from
``tests/e2e/e2e_http.py`` and the ``ResourceManager`` teardown contract from
``tests/e2e/lifecycle.py``. Smaller because this suite only talks to one
deployed gateway and never spins up its own stack.

Every request returns a ``Result`` (``Success | HttpError``), so callers match
on outcome instead of catching exceptions. An ``HttpError`` always carries the
status code and the body, so an auth/validation assertion can read both.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal

import httpx

DEFAULT_BASE: Final = "http://localhost:14001"
DEFAULT_TIMEOUT: Final = 60.0


@dataclass(frozen=True, slots=True)
class Success:
    kind: Literal["success"] = "success"
    status: int = 200
    body: Any = None


@dataclass(frozen=True, slots=True)
class HttpError:
    kind: Literal["error"] = "error"
    status: int = 0
    body: Any = None


Result = Success | HttpError


@dataclass(frozen=True, slots=True)
class Gateway:
    """One deployed gateway plus the credentials to call it."""

    base: str
    master_key: str | None
    scoped_key: str | None

    def url(self, path: str) -> str:
        return f"{self.base.rstrip('/')}/{path.lstrip('/')}"

    def _client(self, key: str | None) -> httpx.Client:
        headers: dict[str, str] = {}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return httpx.Client(
            base_url=self.base.rstrip("/"),
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
            verify=False,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        key: str | None = None,
        json: Any = None,
        files: Any = None,
        data: Any = None,
        params: Mapping[str, Any] | None = None,
    ) -> Result:
        """One request, error-as-value. ``key`` overrides the master key."""
        resolved_key = self.master_key if key is None else key
        try:
            with self._client(resolved_key) as c:
                resp = c.request(method, path, json=json, files=files, data=data, params=params)
        except httpx.HTTPError as exc:
            return HttpError(status=0, body=f"{type(exc).__name__}: {exc}")
        body: Any
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        if resp.status_code < 400:
            return Success(status=resp.status_code, body=body)
        return HttpError(status=resp.status_code, body=body)

    def get(self, path: str, *, key: str | None = None, params: Mapping[str, Any] | None = None) -> Result:
        return self.request("GET", path, key=key, params=params)

    def post(self, path: str, *, key: str | None = None, json: Any = None, files: Any = None, data: Any = None) -> Result:
        return self.request("POST", path, key=key, json=json, files=files, data=data)

    def patch(self, path: str, *, key: str | None = None, json: Any = None) -> Result:
        return self.request("PATCH", path, key=key, json=json)

    def delete(self, path: str, *, key: str | None = None, json: Any = None) -> Result:
        return self.request("DELETE", path, key=key, json=json)


@dataclass
class Resources:
    """LIFO best-effort teardown for state created on the gateway.

    Register ``resources.defer(lambda: gw.delete(f"/key/delete\", json=...))``
    right after creating something. Teardown runs every registered cleanup in
    reverse, swallowing individual failures so one bad delete never blocks the
    rest, exactly like ``tests/e2e/lifecycle.ResourceManager``.
    """

    _cleanups: list[Callable[[], object]] = field(default_factory=list)

    def defer(self, cleanup: Callable[[], object]) -> None:
        self._cleanups.append(cleanup)

    def teardown(self) -> None:
        for cleanup in reversed(self._cleanups):
            try:
                cleanup()
            except Exception:
                pass


def _master_key_from_manifest() -> str | None:
    """Resolve the master key from the deploy manifest, falling back to env.

    Order: explicit ``MASTER_KEY`` env wins, then the manifest. Never
    hardcodes; a gateway with a rotated key just needs the env var set.
    """
    env = os.environ.get("MASTER_KEY") or os.environ.get("LITELLM_MASTER_KEY")
    if env:
        return env
    manifest = (
        os.path.dirname(os.path.abspath(__file__))
        + "/../../deploy/base/gateway/secrets/litellm-master-key.yaml"
    )
    try:
        with open(manifest, encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped.startswith("master-key:"):
                    return stripped.split(":", 1)[1].strip()
    except OSError:
        return None
    return None


def build_gateway() -> Gateway:
    base = os.environ.get("BASE", DEFAULT_BASE)
    return Gateway(
        base=base,
        master_key=_master_key_from_manifest(),
        scoped_key=os.environ.get("KEY"),
    )


def is_prod(gateway: Gateway) -> bool:
    return "litellm.ecouncil.ae" in gateway.base


def error_detail(result: Result) -> str:
    """Compact body text for an assertion message."""
    if isinstance(result.body, (dict, list)):
        import json

        return json.dumps(result.body)[:300]
    return str(result.body)[:300]
