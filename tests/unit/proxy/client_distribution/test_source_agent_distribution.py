"""Tests for the source-agent signed-release distribution slice."""

from __future__ import annotations

import base64
import hashlib
import io
import json
from typing import Final

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

import litellm.proxy.client_distribution.source_agent_distribution as dist_mod
from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth, user_api_key_auth


class _FakeS3Client:
    """In-memory S3 stub matching the boto3 client surface the slice uses."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        if Key not in self._store:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "GetObject")
        data: bytes = self._store[Key]
        return {"Body": io.BytesIO(data), "ContentLength": len(data)}

    def put_object(self, Bucket: str, Key: str, Body: bytes, ContentType: str = "") -> None:
        self._store[Key] = Body


def _no_such_key(key: str) -> Exception:
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": "NoSuchKey", "Message": key}}, "GetObject")


def _sign_manifest(key: Ed25519PrivateKey, body: bytes) -> str:
    return base64.b64encode(key.sign(body)).decode()


def _manifest_body(version: str, sha256: str, os_name: str = "linux", arch: str = "amd64") -> bytes:
    return json.dumps(
        {"arch": arch, "os": os_name, "sha256": sha256, "version": version},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


@pytest.fixture()
def release_key(monkeypatch: pytest.MonkeyPatch) -> Ed25519PrivateKey:
    key: Final = Ed25519PrivateKey.generate()
    monkeypatch.setattr(dist_mod, "_TRUSTED_KEY", key.public_key().public_bytes_raw())
    return key


@pytest.fixture()
def app(release_key: Ed25519PrivateKey, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    from litellm.proxy.client_distribution.storage import RustFSConfig

    s3: Final = _FakeS3Client()
    config: Final = RustFSConfig(
        endpoint_url="http://rustfs-dev.adeo-litellm.svc.cluster.local:9000",
        access_key="test",
        secret_key="test",
        bucket="litellm-install",
    )
    monkeypatch.setattr(dist_mod, "_s3", lambda: (config, s3))
    monkeypatch.setenv("LITELLM_PUBLIC_BASE_URL", "https://gateway.example")
    application: Final = FastAPI()
    application.include_router(dist_mod.router)
    application.dependency_overrides[user_api_key_auth] = lambda: UserAPIKeyAuth(user_role="proxy_admin")
    return application


def _publish(
    client: TestClient, key: Ed25519PrivateKey, binary: bytes, version: str = "0.1.0"
) -> tuple[object, bytes, str]:
    sha256: Final = hashlib.sha256(binary).hexdigest()
    manifest_body: Final = _manifest_body(version, sha256)
    signature: Final = _sign_manifest(key, manifest_body)
    payload: Final = {
        "version": version,
        "sha256": sha256,
        "release_sig_b64": signature,
        "binary_b64": base64.b64encode(binary).decode(),
        "release_json_b64": base64.b64encode(manifest_body).decode(),
    }
    return (
        client.put("/install/admin/v1/source-agent/release", json=payload),
        manifest_body,
        signature,
    )


def test_admin_publish_rejects_bad_signature(app: FastAPI, release_key: Ed25519PrivateKey) -> None:
    client: Final = TestClient(app)
    wrong_key: Final = Ed25519PrivateKey.generate()
    response, _, _ = _publish(client, wrong_key, b"fake-binary")
    assert response.status_code == 409
    assert "signature" in response.json()["detail"]


def test_admin_publish_rejects_digest_mismatch(app: FastAPI, release_key: Ed25519PrivateKey) -> None:
    client: Final = TestClient(app)
    binary: Final = b"fake-binary"
    manifest_body: Final = _manifest_body("0.1.0", "0" * 64)
    payload: Final = {
        "version": "0.1.0",
        "sha256": "0" * 64,
        "release_sig_b64": _sign_manifest(release_key, manifest_body),
        "binary_b64": base64.b64encode(binary).decode(),
        "release_json_b64": base64.b64encode(manifest_body).decode(),
    }
    response: Final = client.put("/install/admin/v1/source-agent/release", json=payload)
    assert response.status_code == 409
    assert "digest" in response.json()["detail"]


def test_publish_then_install_script_and_assets(app: FastAPI, release_key: Ed25519PrivateKey) -> None:
    client: Final = TestClient(app)
    binary: Final = b"fake-source-agent-binary"
    response, manifest_body, signature = _publish(client, release_key, binary)
    assert response.status_code == 200
    assert response.json()["version"] == "0.1.0"

    script: Final = client.get("/install/source-agent")
    assert script.status_code == 200
    text: Final = script.text
    assert 'base="https://gateway.example"' in text
    assert '"$base/install/source-agent/$version/$asset"' in text
    assert hashlib.sha256(binary).hexdigest() in text
    assert signature in text
    assert "sha256sum" in text
    assert "checksum mismatch" in text

    asset: Final = client.get("/install/source-agent/0.1.0/source-agent")
    assert asset.status_code == 200 and asset.content == binary
    manifest: Final = client.get("/install/source-agent/0.1.0/release.json")
    assert manifest.status_code == 200 and manifest.content == manifest_body
    sig_asset: Final = client.get("/install/source-agent/0.1.0/release.sig.b64")
    assert sig_asset.status_code == 200 and sig_asset.text == signature


def test_install_script_404_when_no_release(app: FastAPI) -> None:
    client: Final = TestClient(app)
    response: Final = client.get("/install/source-agent")
    assert response.status_code == 404


def test_unknown_asset_rejected(app: FastAPI, release_key: Ed25519PrivateKey) -> None:
    client: Final = TestClient(app)
    _publish(client, release_key, b"bin")
    response: Final = client.get("/install/source-agent/0.1.0/unknown.asset")
    assert response.status_code == 404
    response: Final = client.get("/install/source-agent/0.1.0/release.json%2f..%2fetc")
    assert response.status_code in (400, 404)


def test_publish_requires_admin(app: FastAPI) -> None:
    app.dependency_overrides[user_api_key_auth] = lambda: UserAPIKeyAuth(user_id="mallory")
    client: Final = TestClient(app)
    payload: Final = {
        "version": "0.1.0",
        "sha256": "0" * 64,
        "release_sig_b64": "A" * 88,
        "binary_b64": "",
        "release_json_b64": "",
    }
    response: Final = client.put("/install/admin/v1/source-agent/release", json=payload)
    assert response.status_code == 403


def test_publish_rejects_path_traversal_version(app: FastAPI, release_key: Ed25519PrivateKey) -> None:
    client: Final = TestClient(app)
    binary: Final = b"bin"
    manifest_body: Final = _manifest_body("../evil", hashlib.sha256(binary).hexdigest())
    payload: Final = {
        "version": "../evil",
        "sha256": hashlib.sha256(binary).hexdigest(),
        "release_sig_b64": _sign_manifest(release_key, manifest_body),
        "binary_b64": base64.b64encode(binary).decode(),
        "release_json_b64": base64.b64encode(manifest_body).decode(),
    }
    response: Final = client.put("/install/admin/v1/source-agent/release", json=payload)
    assert response.status_code == 400
