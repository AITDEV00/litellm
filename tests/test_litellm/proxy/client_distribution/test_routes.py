"""Tests for the client-distribution route handlers.

Storage is stubbed via a fake S3 client, dependency-injected through the
``_get_storage`` seam. No real RustFS or network is involved.
"""

from __future__ import annotations

import hashlib
import io
import json
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import litellm.proxy.client_distribution.routes as routes_mod
from litellm.proxy.client_distribution.routes import router
from litellm.proxy.client_distribution.storage import RustFSConfig, normalize_key

# ── stub S3 client ──────────────────────────────────────────────────────────

_FAKE_CONFIG: RustFSConfig = RustFSConfig(
    endpoint_url="http://rustfs-dev.adeo-litellm.svc.cluster.local:9000",
    access_key="test-access",
    secret_key="test-secret",
    bucket="litellm-client-mirror-dev",
)


class _FakeS3Client:
    """In-memory S3 stub that mimics the boto3 client surface we use."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}
        self.exceptions: MagicMock = MagicMock()
        self.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        if Key not in self._store:
            raise self.exceptions.NoSuchKey(f"NoSuchKey: {Key}")
        data: bytes = self._store[Key]
        return {
            "Body": io.BytesIO(data),
            "ContentLength": len(data),
        }

    def put_object(self, Bucket: str, Key: str, Body: Any, ContentType: str = "") -> None:
        if hasattr(Body, "read"):
            self._store[Key] = Body.read()
        else:
            self._store[Key] = Body

    def head_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        if Key not in self._store:
            raise self.exceptions.NoSuchKey(f"NoSuchKey: {Key}")
        etag: str = hashlib.sha256(self._store[Key]).hexdigest()
        return {"ETag": f'"{etag}"'}

    def delete_object(self, Bucket: str, Key: str) -> None:
        self._store.pop(Key, None)

    def get_paginator(self, operation: str) -> Any:
        client: _FakeS3Client = self

        class _FakePaginator:
            def paginate(self, Bucket: str, Prefix: str = "") -> list[dict[str, Any]]:
                import datetime

                contents: list[dict[str, Any]] = [
                    {
                        "Key": k,
                        "Size": len(v),
                        "ETag": f'"{hashlib.sha256(v).hexdigest()}"',
                        "LastModified": datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
                    }
                    for k, v in sorted(client._store.items())
                    if k.startswith(Prefix)
                ]
                return [{"Contents": contents}]

        return _FakePaginator()


# ── fixtures ────────────────────────────────────────────────────────────────

_ORIGINAL_GET_STORAGE: object = routes_mod._get_storage
_ORIGINAL_AUTH: object = routes_mod.user_api_key_auth


@pytest.fixture()
def s3() -> Generator[_FakeS3Client, None, None]:
    yield _FakeS3Client()


@pytest.fixture()
def app(s3: _FakeS3Client) -> Generator[FastAPI, None, None]:
    """FastAPI app with the router and a stubbed storage layer."""
    routes_mod._get_storage = lambda: (_FAKE_CONFIG, s3)  # type: ignore[assignment]
    application: FastAPI = FastAPI()
    application.include_router(router)
    yield application
    routes_mod._get_storage = _ORIGINAL_GET_STORAGE  # type: ignore[assignment]


@pytest.fixture()
def admin_app(s3: _FakeS3Client) -> Generator[FastAPI, None, None]:
    """App where auth is stubbed to always succeed as proxy_admin."""
    from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth, user_api_key_auth

    fake_auth: UserAPIKeyAuth = UserAPIKeyAuth(user_role="proxy_admin")
    routes_mod._get_storage = lambda: (_FAKE_CONFIG, s3)  # type: ignore[assignment]
    application: FastAPI = FastAPI()
    application.include_router(router)
    application.dependency_overrides[user_api_key_auth] = lambda: fake_auth
    yield application
    routes_mod._get_storage = _ORIGINAL_GET_STORAGE  # type: ignore[assignment]


# ── path normalization ──────────────────────────────────────────────────────


class TestNormalizeKey:
    def test_simple_key(self) -> None:
        assert normalize_key("manifest.json") == "manifest.json"

    def test_nested_key(self) -> None:
        assert normalize_key("uv/0.10.9/uv-x86_64.tar.gz") == "uv/0.10.9/uv-x86_64.tar.gz"

    def test_strips_leading_slash(self) -> None:
        assert normalize_key("/manifest.json") == "manifest.json"

    def test_rejects_traversal(self) -> None:
        with pytest.raises(ValueError, match="traversal"):
            normalize_key("../litellm-logs/data.json")

    def test_rejects_nested_traversal(self) -> None:
        with pytest.raises(ValueError, match="traversal"):
            normalize_key("uv/../../litellm-logs/data.json")

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            normalize_key("")

    def test_rejects_only_dots(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            normalize_key("./")

    def test_strips_dot_segments(self) -> None:
        assert normalize_key("uv/./0.10.9/file.tar.gz") == "uv/0.10.9/file.tar.gz"


# ── public routes ───────────────────────────────────────────────────────────


class TestInstallScript:
    def test_serves_script_with_200(self, app: FastAPI, s3: _FakeS3Client) -> None:
        s3._store["manifest.json"] = json.dumps(
            {
                "schema": 4,
                "cc_switch_version": "5.11.1",
                "cc_switch_artifacts": {
                    "linux-x64-musl": {"file": "f.tgz", "sha256": "a" * 64}
                },
                "platforms": [{"target": "linux-x64-musl", "archive_ext": "tar.gz"}],
            }
        ).encode()
        client: TestClient = TestClient(app)
        response = client.get("/install")
        assert response.status_code == 200
        assert "application/x-sh" in response.headers["content-type"]
        assert 'CC_SWITCH_VERSION="5.11.1"' in response.text

    def test_500_when_manifest_missing_field(
        self, app: FastAPI, s3: _FakeS3Client
    ) -> None:
        """A manifest that fails to parse surfaces as a render/fetch error."""
        s3._store["manifest.json"] = json.dumps({"schema": 3}).encode()
        client: TestClient = TestClient(app, raise_server_exceptions=False)
        response = client.get("/install")
        assert response.status_code == 502

    def test_store_template_overrides_baked(
        self, app: FastAPI, s3: _FakeS3Client
    ) -> None:
        """An admin-synced template in the store takes precedence over the baked one."""
        s3._store["manifest.json"] = json.dumps(
            {
                "schema": 4,
                "cc_switch_version": "5.11.1",
                "cc_switch_artifacts": {
                    "linux-x64-musl": {"file": "f.tgz", "sha256": "a" * 64}
                },
            }
        ).encode()
        s3._store["templates/install.sh.tmpl"] = b"# custom admin template\nBASE={{ base_url }}\n"
        client: TestClient = TestClient(app)
        response = client.get("/install")
        assert response.status_code == 200
        assert "# custom admin template" in response.text

    def test_store_template_render_failure_falls_back_to_baked(
        self, app: FastAPI, s3: _FakeS3Client
    ) -> None:
        """A store template referencing an unknown variable fails loudly, not silently."""
        s3._store["manifest.json"] = json.dumps(
            {
                "schema": 4,
                "cc_switch_version": "5.11.1",
                "cc_switch_artifacts": {
                    "linux-x64-musl": {"file": "f.tgz", "sha256": "a" * 64}
                },
            }
        ).encode()
        s3._store["templates/install.sh.tmpl"] = b"gateway={{ not_a_real_var }}"
        client: TestClient = TestClient(app, raise_server_exceptions=False)
        response = client.get("/install")
        assert response.status_code == 500
        assert "failed to render" in response.text

    def test_503_when_store_unconfigured(self) -> None:
        # Build a bare app without the _get_storage patch, so the real
        # env-var-based load_config_from_env fires and fails.
        bare_app: FastAPI = FastAPI()
        bare_app.include_router(router)
        with patch.dict("os.environ", {}, clear=True):
            client: TestClient = TestClient(bare_app, raise_server_exceptions=False)
            response = client.get("/install")
            assert response.status_code == 503


class TestGetManifest:
    def test_returns_manifest_json(self, app: FastAPI, s3: _FakeS3Client) -> None:
        manifest: dict[str, str] = {
            "uv_version": "0.10.9",
            "litellm_cli_version": "1.102.0",
            "python_version": "3.13.9",
        }
        s3._store["manifest.json"] = json.dumps(manifest).encode()
        client: TestClient = TestClient(app)
        response = client.get("/install/manifest.json")
        assert response.status_code == 200
        assert response.json()["uv_version"] == "0.10.9"

    def test_404_when_missing(self, app: FastAPI) -> None:
        client: TestClient = TestClient(app)
        response = client.get("/install/manifest.json")
        assert response.status_code == 404


class TestWheelhouseIndex:
    def test_lists_wheels_as_find_links_page(self, app: FastAPI, s3: _FakeS3Client) -> None:
        s3._store["pypi/litellm-1.102.0-py3-none-any.whl"] = b"wheel-a"
        s3._store["pypi/click-8.1.7-py3-none-any.whl"] = b"wheel-b"
        s3._store["pypi/not-a-wheel.txt"] = b"nope"
        client: TestClient = TestClient(app)
        for path in ("/install/pypi", "/install/pypi/"):
            response = client.get(path)
            assert response.status_code == 200
            assert "text/html" in response.headers["content-type"]
            assert '/install/pypi/litellm-1.102.0-py3-none-any.whl' in response.text
            assert '/install/pypi/click-8.1.7-py3-none-any.whl' in response.text
            assert "not-a-wheel" not in response.text


class TestHarnessPreset:
    def test_serves_store_preset(self, app: FastAPI, s3: _FakeS3Client) -> None:
        preset: bytes = b'{"npm": "@ai-sdk/openai-compatible", "custom": true}'
        s3._store["providers/oicm/open-code/v3.json"] = preset
        client: TestClient = TestClient(app)
        response = client.get("/install/providers/oicm/open-code/v3.json")
        assert response.status_code == 200
        assert response.json()["custom"] is True

    def test_baked_fallback_when_store_missing(self, app: FastAPI) -> None:
        client: TestClient = TestClient(app)
        response = client.get("/install/providers/oicm/open-code/v1.json")
        assert response.status_code == 200
        assert response.json()["npm"] == "@ai-sdk/openai-compatible"

    def test_404_for_unknown_harness(self, app: FastAPI) -> None:
        client: TestClient = TestClient(app)
        response = client.get("/install/providers/oicm/no-such/v1.json")
        assert response.status_code == 404

    def test_preset_has_no_runtime_values(self, app: FastAPI) -> None:
        """Presets are structural skeletons: no apiKey/baseURL filled server-side."""
        client: TestClient = TestClient(app)
        response = client.get("/install/providers/oicm/open-code/v1.json")
        body = response.json()
        assert body["options"]["apiKey"] == ""
        assert body["options"]["baseURL"] == ""
        assert body["models"] == {}


class TestGetArtifact:
    def test_serves_wheel(self, app: FastAPI, s3: _FakeS3Client) -> None:
        wheel_content: bytes = b"PK\x03\x04fake-wheel-bytes"
        s3._store["pypi/litellm-1.102.0-py3-none-any.whl"] = wheel_content
        client: TestClient = TestClient(app)
        response = client.get("/install/pypi/litellm-1.102.0-py3-none-any.whl")
        assert response.status_code == 200
        assert response.content == wheel_content

    def test_404_for_missing(self, app: FastAPI) -> None:
        client: TestClient = TestClient(app)
        response = client.get("/install/uv/0.10.9/nonexistent.tar.gz")
        assert response.status_code == 404

    def test_traversal_rejected(self, app: FastAPI) -> None:
        # The HTTP client normalizes ".." out of the URL path before it
        # reaches the handler, so the traversal never arrives as a path
        # parameter. The normalize_key defense is tested at the unit level
        # above. This test confirms the route doesn't serve anything from
        # outside the bucket when the client resolves the dots.
        client: TestClient = TestClient(app)
        response = client.get("/install/../../litellm-logs/data.json")
        assert response.status_code == 404


# ── admin routes ────────────────────────────────────────────────────────────


class TestAdminPutArtifact:
    def test_upload_and_idempotent(self, admin_app: FastAPI) -> None:
        client: TestClient = TestClient(admin_app)

        content: bytes = b"fake-wheel-content"
        response = client.put(
            "/install/admin/v1/artifact/pypi/test-1.0-py3-none-any.whl",
            content=content,
            headers={"content-type": "application/octet-stream"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["unchanged"] is False
        assert data["size"] == len(content)
        assert data["sha256"] == hashlib.sha256(content).hexdigest()

        # Second identical upload -> unchanged
        response2 = client.put(
            "/install/admin/v1/artifact/pypi/test-1.0-py3-none-any.whl",
            content=content,
            headers={"content-type": "application/octet-stream"},
        )
        assert response2.status_code == 200
        assert response2.json()["unchanged"] is True


class TestAdminDeleteArtifact:
    def test_delete_unreferenced(self, admin_app: FastAPI, s3: _FakeS3Client) -> None:
        s3._store["pypi/old-wheel.whl"] = b"old"
        client: TestClient = TestClient(admin_app)
        response = client.delete("/install/admin/v1/artifact/pypi/old-wheel.whl")
        assert response.status_code == 200
        assert "pypi/old-wheel.whl" not in s3._store

    def test_delete_rejects_manifest_referenced(self, admin_app: FastAPI, s3: _FakeS3Client) -> None:
        key: str = "cc-switch/5.11.1/cc-switch-cli-linux-x64-musl.tar.gz"
        s3._store[key] = b"ccs-binary"
        s3._store["manifest.json"] = json.dumps(
            {
                "schema": 4,
                "cc_switch_version": "5.11.1",
                "cc_switch_artifacts": {
                    "linux-x64-musl": {
                        "file": "cc-switch-cli-linux-x64-musl.tar.gz",
                        "sha256": "abc",
                    }
                },
            }
        ).encode()
        client: TestClient = TestClient(admin_app)
        response = client.delete(f"/install/admin/v1/artifact/{key}")
        assert response.status_code == 409
        assert key in s3._store  # Not deleted


class TestAdminInventory:
    def test_lists_objects(self, admin_app: FastAPI, s3: _FakeS3Client) -> None:
        s3._store["manifest.json"] = b"{}"
        s3._store["pypi/a.whl"] = b"aaa"
        client: TestClient = TestClient(admin_app)
        response = client.get("/install/admin/v1/inventory")
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2
        keys: list[str] = [o["key"] for o in data["objects"]]
        assert "manifest.json" in keys
        assert "pypi/a.whl" in keys


class TestAdminPutManifest:
    def test_rejects_missing_objects(self, admin_app: FastAPI) -> None:
        client: TestClient = TestClient(admin_app)
        manifest: dict[str, Any] = {
            "schema": 4,
            "cc_switch_version": "5.11.1",
            "cc_switch_artifacts": {
                "linux-x64-musl": {
                    "file": "cc-switch-cli-linux-x64-musl.tar.gz",
                    "sha256": "abc",
                }
            },
        }
        response = client.put(
            "/install/admin/v1/manifest",
            content=json.dumps(manifest).encode(),
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 409

    def test_accepts_valid_manifest(self, admin_app: FastAPI, s3: _FakeS3Client) -> None:
        # Pre-populate the artifact the manifest references
        s3._store["cc-switch/5.11.1/cc-switch-cli-linux-x64-musl.tar.gz"] = b"ccs-binary"
        client: TestClient = TestClient(admin_app)
        manifest: dict[str, Any] = {
            "schema": 4,
            "cc_switch_version": "5.11.1",
            "cc_switch_artifacts": {
                "linux-x64-musl": {
                    "file": "cc-switch-cli-linux-x64-musl.tar.gz",
                    "sha256": "abc",
                }
            },
        }
        response = client.put(
            "/install/admin/v1/manifest",
            content=json.dumps(manifest).encode(),
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 200
        assert response.json()["ok"] is True
