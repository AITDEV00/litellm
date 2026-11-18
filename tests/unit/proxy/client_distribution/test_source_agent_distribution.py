"""Tests for source-agent release distribution through the shared artifact channel.

These drive the real routes against a small in-memory store: the checked-in
template from disk is uploaded exactly as an operator would, the install route
renders it from a validated active release, and the binary is fetched back and its
checksum compared. Nothing here substitutes placeholders in a fixture, because the
bug under test was precisely that no production code performed that substitution.
"""

from __future__ import annotations

import hashlib
import io
import pathlib
from typing import Any, Callable, Final

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport

import litellm.proxy.client_distribution.source_agent_distribution as dist_mod
from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth, user_api_key_auth

_REPO_ROOT: Final = pathlib.Path(__file__).resolve().parents[4]
_TEMPLATE_PATH: Final = _REPO_ROOT / "oicm-litellm-layer" / "templates" / "source-agent-install.sh.tmpl"

_BINARY: Final = b"\x7fELF" + b"source-link-binary-bytes" * 64
_VERSION: Final = "0.5.0"


class _FakeS3Client:
    """In-memory store matching the boto3 surface this slice uses.

    ETags are content digests and conditional writes are enforced, because the
    activation race is only observable when the fake honours ``IfMatch`` and
    ``IfNoneMatch`` the way the real store does. A fake that accepted every put
    would let a read-then-write race pass.
    """

    def __init__(self) -> None:
        self.store: Final[dict[str, bytes]] = {}
        self.puts: Final[list[dict[str, str]]] = []
        self.before_put: Callable[[str], None] | None = None

    def _missing(self, key: str, operation: str) -> None:
        from botocore.exceptions import ClientError

        raise ClientError({"Error": {"Code": "NoSuchKey", "Message": f"missing {key}"}}, operation)

    def _etag(self, key: str) -> str:
        return '"%s"' % hashlib.sha256(self.store[key]).hexdigest()

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        if Key not in self.store:
            self._missing(Key, "GetObject")
        data: Final = self.store[Key]
        return {"Body": io.BytesIO(data), "ContentLength": len(data), "ETag": self._etag(Key)}

    def put_object(
        self,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str = "",
        IfMatch: str | None = None,
        IfNoneMatch: str | None = None,
    ) -> None:
        from botocore.exceptions import ClientError

        self.puts.append({"Key": Key, "IfMatch": IfMatch or "", "IfNoneMatch": IfNoneMatch or ""})
        if self.before_put is not None:
            self.before_put(Key)
        exists: Final = Key in self.store
        if IfNoneMatch == "*" and exists:
            raise ClientError({"Error": {"Code": "PreconditionFailed", "Message": "exists"}}, "PutObject")
        if IfMatch is not None and (not exists or self._etag(Key) != IfMatch):
            raise ClientError({"Error": {"Code": "PreconditionFailed", "Message": "stale"}}, "PutObject")
        self.store[Key] = Body


def _config() -> dist_mod.RustFSConfig:
    from litellm.proxy.client_distribution.storage import RustFSConfig

    return RustFSConfig(
        endpoint_url="http://rustfs-dev.adeo-litellm.svc.cluster.local:9000",
        access_key="test",
        secret_key="test",
        bucket="litellm-install",
    )


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> _FakeS3Client:
    client: Final = _FakeS3Client()
    config: Final = _config()
    monkeypatch.setattr(dist_mod, "_storage", lambda: (config, client))
    monkeypatch.setenv("LITELLM_PUBLIC_BASE_URL", "https://gateway.example")
    return client


@pytest.fixture()
def app(monkeypatch: pytest.MonkeyPatch, store: _FakeS3Client) -> FastAPI:
    application: Final = FastAPI()
    application.include_router(dist_mod.router)
    application.dependency_overrides[user_api_key_auth] = lambda: UserAPIKeyAuth(user_role="proxy_admin")
    return application


def _publish(store: _FakeS3Client, version: str = _VERSION, binary: bytes = _BINARY) -> str:
    """Upload one immutable release pair and the template, exactly as a publisher would."""
    sha256: Final = hashlib.sha256(binary).hexdigest()
    store.store[f"source-agent/{version}/source-link"] = binary
    store.store[f"source-agent/{version}/release.json"] = (
        b'{"version":"%s","sha256":"%s","size":%d}' % (version.encode(), sha256.encode(), len(binary))
    )
    store.store[dist_mod.TEMPLATE_KEY] = _TEMPLATE_PATH.read_bytes()
    return sha256


def _activate(store: _FakeS3Client, version: str = _VERSION, binary: bytes = _BINARY) -> None:
    sha256: Final = hashlib.sha256(binary).hexdigest()
    store.store[dist_mod.MANIFEST_KEY] = (
        b'{"version":"%s","sha256":"%s","size":%d}' % (version.encode(), sha256.encode(), len(binary))
    )


def test_installer_is_rendered_from_the_stored_template(app: FastAPI, store: _FakeS3Client) -> None:
    """The served installer has every placeholder filled by the gateway.

    This is the R1 regression: the old publisher uploaded the template verbatim, so
    a real install shipped ``__BASE__`` and friends. Serving real values, and
    failing on any leftover placeholder, is what a user can actually run.
    """
    sha256: Final = _publish(store)
    _activate(store)

    response: Final = TestClient(app).get("/install/source-agent")

    assert response.status_code == 200
    body: Final = response.text
    for placeholder in ("__BASE__", "__VERSION__", "__SHA__", "__STATE__"):
        assert placeholder not in body, f"{placeholder} leaked into the served installer"
    assert 'base="https://gateway.example"' in body
    assert f'version="{_VERSION}"' in body
    assert f'expected="{sha256}"' in body
    assert "ExecStart=${state}/source-link connector" in body


def test_installer_fails_loudly_when_pointer_disagrees_with_the_release(app: FastAPI, store: _FakeS3Client) -> None:
    """A corrupt pointer must not render an installer for bytes it does not describe."""
    _publish(store)
    store.store[dist_mod.MANIFEST_KEY] = (
        b'{"version":"0.5.0","sha256":"' + b"0" * 64 + b'","size":1}'
    )

    response: Final = TestClient(app).get("/install/source-agent")

    assert response.status_code == 409
    assert "disagrees" in response.json()["detail"]


def test_installer_requires_a_published_template(app: FastAPI, store: _FakeS3Client) -> None:
    """No template means a clear conflict naming what to upload, not a stale script."""
    _publish(store)
    del store.store[dist_mod.TEMPLATE_KEY]
    _activate(store)

    response: Final = TestClient(app).get("/install/source-agent")

    assert response.status_code == 409
    assert dist_mod.TEMPLATE_KEY in response.json()["detail"]


def test_binary_asset_downloads_bytes_that_match_the_advertised_checksum(app: FastAPI, store: _FakeS3Client) -> None:
    """The installer's checksum is meaningful only if the served binary matches it."""
    sha256: Final = _publish(store)
    _activate(store)
    client: Final = TestClient(app)

    installer: Final = client.get("/install/source-agent").text
    downloaded: Final = client.get(f"/install/source-agent/{_VERSION}/source-link")

    assert downloaded.status_code == 200
    assert hashlib.sha256(downloaded.content).hexdigest() == sha256
    assert sha256 in installer, "the served installer must pin the digest of the served binary"


def test_unknown_asset_is_rejected(app: FastAPI, store: _FakeS3Client) -> None:
    _publish(store)
    response: Final = TestClient(app).get(f"/install/source-agent/{_VERSION}/etc")

    assert response.status_code == 404


def test_activation_requires_the_binary_to_exist(app: FastAPI, store: _FakeS3Client) -> None:
    """A pointer may only move to a release whose immutable bytes are already stored."""
    store.store[dist_mod.TEMPLATE_KEY] = _TEMPLATE_PATH.read_bytes()
    response: Final = TestClient(app).put(
        "/install/admin/v1/source-agent/activate",
        json={"version": _VERSION, "sha256": hashlib.sha256(_BINARY).hexdigest()},
    )

    assert response.status_code == 409
    assert "not uploaded" in response.json()["detail"]


def test_activation_refuses_a_digest_that_the_store_does_not_hold(app: FastAPI, store: _FakeS3Client) -> None:
    """Activation validates the referenced bytes, so a pointer cannot claim a digest
    that no stored object has."""
    _publish(store)
    response: Final = TestClient(app).put(
        "/install/admin/v1/source-agent/activate",
        json={"version": _VERSION, "sha256": "f" * 64},
    )

    assert response.status_code == 409


def test_concurrent_activations_cannot_both_win(app: FastAPI, store: _FakeS3Client) -> None:
    """The checked pointer handoff is what stops two publishers from interleaving.

    Both read token A, both try to activate; the second must be refused because the
    pointer moved, rather than silently overwriting the first release.
    """
    first_sha: Final = _publish(store)
    second_binary: Final = _BINARY + b"-next"
    _publish(store, version="0.6.0", binary=second_binary)
    client: Final = TestClient(app)

    observed: Final = client.get("/install/admin/v1/source-agent/state").json()["token"]
    first: Final = client.put(
        "/install/admin/v1/source-agent/activate",
        json={"version": _VERSION, "sha256": first_sha, "expected_current": observed},
    )
    second: Final = client.put(
        "/install/admin/v1/source-agent/activate",
        json={
            "version": "0.6.0",
            "sha256": hashlib.sha256(second_binary).hexdigest(),
            "expected_current": observed,
        },
    )

    assert first.status_code == 200
    assert second.status_code == 409, "the stale-token publish must not clobber the first"
    assert store.store[dist_mod.MANIFEST_KEY] == b'{"version":"0.5.0","sha256":"%s","size":0}' % (
        first_sha.encode(),
    )


@pytest.mark.asyncio
async def test_activation_race_without_a_shared_token_is_caught_by_the_conditional_write(
    app: FastAPI, store: _FakeS3Client
) -> None:
    """Two publishers that both read the pointer before either writes must not both win.

    The token check alone is read-then-write: when neither caller passes
    ``expected_current``, two requests can both observe the same pointer and both
    proceed, which is the ``[200, 200]`` the review reproduced. The conditional put
    on the ETag read during validation is what makes the exchange atomic. This
    forces the interleaving by landing a competing activation between this
    request's read and its write, so the request fails unless its write is
    conditional.
    """
    first_sha: Final = _publish(store)
    second_binary: Final = _BINARY + b"-next"
    second_sha: Final = _publish(store, version="0.6.0", binary=second_binary)
    landed: Final = []

    def land_a_competing_activation(key: str) -> None:
        if key != dist_mod.MANIFEST_KEY or landed:
            return
        landed.append(True)
        store.store[dist_mod.MANIFEST_KEY] = b'{"version":"0.5.0","sha256":"%s","size":0}' % first_sha.encode()

    store.before_put = land_a_competing_activation
    transport: Final = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
        response = await client.put(
            "/install/admin/v1/source-agent/activate",
            json={"version": "0.6.0", "sha256": second_sha},
        )

    assert response.status_code == 409, "the interleaved publish must be refused, not overwrite the first release"
    assert b'"0.5.0"' in store.store[dist_mod.MANIFEST_KEY]


def test_rollback_reactivates_a_previous_complete_release(app: FastAPI, store: _FakeS3Client) -> None:
    """Rollback is another pointer move to an already-complete pair, not an edit of
    the old bytes."""
    first_sha: Final = _publish(store)
    second_binary: Final = _BINARY + b"-next"
    second_sha: Final = _publish(store, version="0.6.0", binary=second_binary)
    client: Final = TestClient(app)

    client.put("/install/admin/v1/source-agent/activate", json={"version": _VERSION, "sha256": first_sha})
    client.put("/install/admin/v1/source-agent/activate", json={"version": "0.6.0", "sha256": second_sha})
    rollback: Final = client.put("/install/admin/v1/source-agent/activate", json={"version": _VERSION, "sha256": first_sha})

    assert rollback.status_code == 200
    served: Final = client.get("/install/source-agent").text
    assert f'version="{_VERSION}"' in served and first_sha in served


def test_activation_requires_admin(app: FastAPI, store: _FakeS3Client) -> None:
    sha256: Final = _publish(store)
    app.dependency_overrides[user_api_key_auth] = lambda: UserAPIKeyAuth(user_role="internal_user")
    response: Final = TestClient(app).put(
        "/install/admin/v1/source-agent/activate", json={"version": _VERSION, "sha256": sha256}
    )

    assert response.status_code == 403
