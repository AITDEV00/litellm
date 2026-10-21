"""Same-origin client distribution routes (/install).

Co-located vertical slice, following the ``oicm_routes.py`` / ``voice_routes.py``
pattern: a standalone ``APIRouter`` that lazily imports ``proxy_server`` globals
inside handlers. Mounted in the fork-appended include block at the bottom of
``proxy_server.py``.

Serves the install script, proxies artifact reads from RustFS, and exposes
the admin write surface under ``/install/admin/v1/*``.

Route registration order matters: specific admin routes must come before the
catch-all ``GET /install/{path:path}`` so FastAPI matches them first.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
from typing import TYPE_CHECKING, Final

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse

from litellm._logging import verbose_proxy_logger
from litellm.proxy._types import LitellmUserRoles
from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth, user_api_key_auth
from litellm.proxy.client_distribution.generator import (
    MirrorManifest,
    get_baked_preset,
    manifest_from_json,
    render_install_script,
)
from litellm.proxy.client_distribution.generator import get_template
from litellm.proxy.client_distribution.storage import (
    RustFSConfig,
    create_s3_client,
    load_config_from_env,
    normalize_key,
)

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client


def _is_not_found(exc: ClientError) -> bool:
    """RustFS returns a generic ClientError with a 404 code, not boto3's typed
    NoSuchKey subclass, so the typed catch alone misses it."""
    code: str = exc.response.get("Error", {}).get("Code", "")
    return code in ("NoSuchKey", "404", "NotFound")

router = APIRouter(tags=["install"])

# Max artifact upload size (cc-switch archives are ~20 MB).
_MAX_ARTIFACT_BYTES: Final = 200 * 1024 * 1024
_ARTIFACT_READ_CHUNK: Final = 1024 * 1024

_CONTENT_TYPES: Final = {
    ".json": "application/json",
    ".tar.gz": "application/gzip",
    ".zip": "application/zip",
}

_DEFAULT_CONTENT_TYPE: Final = "application/octet-stream"


def _content_type_for(key: str) -> str:
    for suffix, ct in _CONTENT_TYPES.items():
        if key.endswith(suffix):
            return ct
    return _DEFAULT_CONTENT_TYPE


def _require_admin(user_api_key_dict: UserAPIKeyAuth) -> None:
    if user_api_key_dict.user_role != LitellmUserRoles.PROXY_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only proxy admins can manage the client mirror",
        )


def _get_storage() -> tuple[RustFSConfig, S3Client]:
    """Build the S3 client from env. Returns (config, client).

    Raises 503 if the store is not configured.
    """
    try:
        config: RustFSConfig = load_config_from_env()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    return config, create_s3_client(config)


def _get_base_url(request: Request) -> str:
    """Resolve the gateway's public base URL.

    Prefers the ``LITELLM_PUBLIC_BASE_URL`` env var; falls back to the
    request's Host header with the forwarded protocol.
    """
    env_url: str | None = os.environ.get("LITELLM_PUBLIC_BASE_URL")
    if env_url:
        return env_url.rstrip("/")
    forwarded_proto: str = request.headers.get("x-forwarded-proto", "https")
    host: str = request.headers.get("host", "localhost")
    return f"{forwarded_proto}://{host}"


def _fetch_manifest(config: RustFSConfig, s3: S3Client) -> MirrorManifest:
    """Fetch and parse the manifest from the store."""
    try:
        response = s3.get_object(Bucket=config.bucket, Key="manifest.json")
        raw: bytes = response["Body"].read()
        return manifest_from_json(raw.decode("utf-8"))
    except ClientError as exc:
        if not _is_not_found(exc):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="object store error",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="manifest.json not found in the store",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"corrupt manifest in store: {exc}",
        ) from exc
    except Exception as exc:
        verbose_proxy_logger.warning("client_distribution: manifest fetch failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="object store unreachable",
        ) from exc


# The install template is served from the store when present (admin-synced via
# the PROXY_ADMIN-gated write API, so only an admin can change it), falling back
# to the baked-in copy when the store copy is missing or unreadable. This keeps
# the template iterable without an image rebuild; the store write path is the
# same admin channel as every other artifact. See security.md "What lives where".
_TEMPLATE_KEY: Final = "templates/install.sh.tmpl"


def _fetch_template(config: RustFSConfig, s3: S3Client) -> str:
    """Fetch the install-script template from the store.

    Falls back to the baked-in template if the store copy is missing or
    unreadable, so a broken store never breaks /install.
    """
    try:
        response = s3.get_object(Bucket=config.bucket, Key=_TEMPLATE_KEY)
        template: str = response["Body"].read().decode("utf-8")
        if not template.strip():
            raise ValueError("template is empty")
        verbose_proxy_logger.info("client_distribution: using template from store (%s)", _TEMPLATE_KEY)
        return template
    except Exception:
        verbose_proxy_logger.info("client_distribution: using baked-in template (store copy not found)")
        return get_template()


async def _validate_store_references(
    config: RustFSConfig,
    s3: S3Client,
    data: dict[str, object],
) -> None:
    """Check that every object the manifest references exists in the store.

    Raises 409 listing every missing key.
    """
    cc_artifacts: object = data.get("cc_switch_artifacts", {})
    cc_version: object = data.get("cc_switch_version", "")
    if isinstance(cc_artifacts, dict):
        artifact_keys: tuple[str, ...] = tuple(
            f"cc-switch/{cc_version}/{artifact['file']}"
            for artifact in cc_artifacts.values()
            if isinstance(artifact, dict) and isinstance(artifact.get("file"), str)
        )
        missing: list[str] = []
        for key in artifact_keys:
            if not await _key_exists(s3, config.bucket, key):
                missing.append(key)
        if missing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"manifest references missing objects: {missing}",
            )

    harnesses: object = data.get("harnesses", {})
    if isinstance(harnesses, dict):
        preset_paths: tuple[str, ...] = tuple(
            preset["path"]
            for preset in harnesses.values()
            if isinstance(preset, dict)
            and isinstance(preset.get("path"), str)
            and preset["path"]
        )
        missing_presets: list[str] = []
        for path in preset_paths:
            if not await _key_exists(s3, config.bucket, path):
                missing_presets.append(path)
        if missing_presets:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"manifest references missing preset objects: {missing_presets}",
            )


async def _key_exists(s3: S3Client, bucket: str, key: str) -> bool:
    """Return True if the key exists in the bucket."""
    try:
        await asyncio.to_thread(s3.head_object, Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        if _is_not_found(exc):
            return False
        raise


# ---------------------------------------------------------------------------
# Admin routes (PROXY_ADMIN only). Must be registered BEFORE the catch-all
# GET /install/{path:path} so FastAPI matches them first.
# ---------------------------------------------------------------------------


@router.get(
    "/install/admin/v1/manifest",
    dependencies=[Depends(user_api_key_auth)],
)
async def admin_get_manifest(
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> Response:
    """Read the current manifest (admin view)."""
    _require_admin(user_api_key_dict)
    return await get_manifest()


@router.put(
    "/install/admin/v1/manifest",
    dependencies=[Depends(user_api_key_auth)],
)
async def admin_put_manifest(
    request: Request,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> dict[str, object]:
    """Upsert the manifest. Validates that every referenced object exists."""
    _require_admin(user_api_key_dict)
    config, s3 = _get_storage()

    body: bytes = await request.body()
    try:
        data: dict[str, object] = json.loads(body)
    except (json.JSONDecodeError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid JSON: {exc}",
        ) from exc

    schema_version: object = data.get("schema")
    if schema_version != 5:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unsupported schema version: {schema_version} (expected 5)",
        )

    # Validate the optional output cap early: an invalid value would surface
    # later as a 502 render failure on every /install request.
    max_output_tokens: object = data.get("max_output_tokens")
    if max_output_tokens is not None and (
        isinstance(max_output_tokens, bool)
        or not isinstance(max_output_tokens, int)
        or max_output_tokens <= 0
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'max_output_tokens' must be a positive integer",
        )

    await _validate_store_references(config, s3, data)

    await asyncio.to_thread(
        s3.put_object,
        Bucket=config.bucket,
        Key="manifest.json",
        Body=body,
        ContentType="application/json",
    )
    return {"ok": True, "size": len(body)}


@router.put(
    "/install/admin/v1/artifact/{path:path}",
    dependencies=[Depends(user_api_key_auth)],
)
async def admin_put_artifact(
    path: str,
    request: Request,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> dict[str, object]:
    """Upsert one artifact. Streams to RustFS with sha256 on the fly."""
    _require_admin(user_api_key_dict)
    config, s3 = _get_storage()

    try:
        key: str = normalize_key(path)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # The cap is enforced on the bytes actually read, not on the
    # Content-Length header: a chunked upload sends no Content-Length, and a
    # mismatched one lies. Accumulating per chunk keeps peak memory at
    # _MAX_ARTIFACT_BYTES rather than at whatever the client sends.
    chunks: list[bytes] = []  # rebind-ok: grows while streaming, then joined once
    total: int = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > _MAX_ARTIFACT_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"artifact too large: over {_MAX_ARTIFACT_BYTES} bytes",
            )
        chunks.append(chunk)
    body: bytes = b"".join(chunks)
    sha256: str = hashlib.sha256(body).hexdigest()

    # Check if the object already exists with the same hash
    unchanged: bool = False
    try:
        head = await asyncio.to_thread(
            s3.head_object, Bucket=config.bucket, Key=key
        )
        existing_etag: str = head.get("ETag", "").strip('"')
        if existing_etag == sha256:
            unchanged = True
    except ClientError as exc:
        if not _is_not_found(exc):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="object store error",
            ) from exc
        pass  # Object doesn't exist yet; proceed to upload

    if not unchanged:
        await asyncio.to_thread(
            s3.put_object,
            Bucket=config.bucket,
            Key=key,
            Body=io.BytesIO(body),
            ContentType=_content_type_for(key),
        )

    return {
        "path": key,
        "sha256": sha256,
        "size": len(body),
        "unchanged": unchanged,
    }


@router.delete(
    "/install/admin/v1/artifact/{path:path}",
    dependencies=[Depends(user_api_key_auth)],
)
async def admin_delete_artifact(
    path: str,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> dict[str, object]:
    """Delete one artifact. Rejects if the manifest still references it."""
    _require_admin(user_api_key_dict)
    config, s3 = _get_storage()

    try:
        key: str = normalize_key(path)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # Check manifest references
    try:
        response = await asyncio.to_thread(
            s3.get_object, Bucket=config.bucket, Key="manifest.json"
        )
        manifest_data: dict[str, object] = json.loads(response["Body"].read())
        cc_artifacts: object = manifest_data.get("cc_switch_artifacts", {})
        if isinstance(cc_artifacts, dict):
            cc_version: object = manifest_data.get("cc_switch_version", "")
            for artifact in cc_artifacts.values():
                if isinstance(artifact, dict):
                    filename: object = artifact.get("file")
                    if isinstance(filename, str) and f"cc-switch/{cc_version}/{filename}" == key:
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail=f"object is still referenced by the manifest: {key}",
                        )
    except HTTPException:
        raise
    except ClientError as exc:
        if not _is_not_found(exc):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="object store error",
            ) from exc
        pass  # No manifest yet; nothing references the object

    await asyncio.to_thread(s3.delete_object, Bucket=config.bucket, Key=key)
    return {"ok": True, "path": key}


@router.get(
    "/install/admin/v1/inventory",
    dependencies=[Depends(user_api_key_auth)],
)
async def admin_inventory(
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> dict[str, object]:
    """List stored objects with sizes and hashes."""
    _require_admin(user_api_key_dict)
    config, s3 = _get_storage()

    objects: list[dict[str, object]] = []
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=config.bucket):
            for obj in page.get("Contents", []):
                objects.append(
                    {
                        "key": obj["Key"],
                        "size": obj["Size"],
                        "etag": obj.get("ETag", "").strip('"'),
                        "last_modified": obj["LastModified"].isoformat(),
                    }
                )
    except Exception as exc:
        verbose_proxy_logger.warning("client_distribution: inventory failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="object store unreachable",
        ) from exc

    return {"bucket": config.bucket, "objects": objects, "count": len(objects)}


# ---------------------------------------------------------------------------
# Public routes (unauthenticated)
# ---------------------------------------------------------------------------


@router.get("/install")
async def install_script(request: Request) -> Response:
    """Serve the generated install script."""
    config, s3 = _get_storage()
    base_url: str = _get_base_url(request)
    try:
        manifest: MirrorManifest = await asyncio.to_thread(_fetch_manifest, config, s3)
        template: str = await asyncio.to_thread(_fetch_template, config, s3)
    except HTTPException:
        raise
    try:
        script: str = render_install_script(manifest, base_url, template=template)
    except Exception as exc:
        # A store-pushed template that is malformed Jinja or references an
        # undefined variable must fail loudly here, not ship a broken script.
        verbose_proxy_logger.warning("client_distribution: template render failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"install template failed to render: {exc}",
        ) from exc
    return Response(content=script, media_type="application/x-sh")


@router.get("/install/manifest.json")
async def get_manifest() -> Response:
    """Serve the mirror manifest."""
    config, s3 = _get_storage()
    try:
        response = await asyncio.to_thread(
            s3.get_object, Bucket=config.bucket, Key="manifest.json"
        )
        raw: bytes = response["Body"].read()
        return Response(content=raw, media_type="application/json")
    except ClientError as exc:
        if not _is_not_found(exc):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="object store error",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="manifest not found"
        ) from exc
    except Exception as exc:
        verbose_proxy_logger.warning("client_distribution: manifest read failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="object store unreachable",
        ) from exc


@router.get("/install/providers/{provider}/{harness}/v{version}.json")
async def get_harness_preset(provider: str, harness: str, version: int) -> Response:
    """Serve a versioned harness preset.

    Store-backed with a baked-in fallback: the manifest pins
    ``providers/adeo/<harness>/v<N>.json``, and this route serves the store
    copy when present, else the baked skeleton for that harness. Registered
    before the catch-all so it matches first. Runtime values (base URL, API
    key, models) are NOT filled here — the installer composes them with jq,
    so this response is identical for every caller.
    """
    config, s3 = _get_storage()
    key: str = f"providers/{provider}/{harness}/v{version}.json"
    try:
        response = await asyncio.to_thread(
            s3.get_object, Bucket=config.bucket, Key=key
        )
        raw: bytes = response["Body"].read()
        return Response(content=raw, media_type="application/json")
    except ClientError as exc:
        if not _is_not_found(exc):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="object store error",
            ) from exc
        baked: str | None = get_baked_preset(harness)
        if baked is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no preset for harness '{harness}'",
            ) from exc
        return Response(content=baked, media_type="application/json")
    except Exception as exc:
        verbose_proxy_logger.warning("client_distribution: preset read failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="object store unreachable",
        ) from exc


@router.get("/install/{path:path}")
async def get_artifact(path: str) -> StreamingResponse:
    """Proxy an artifact read from RustFS."""
    config, s3 = _get_storage()
    try:
        key: str = normalize_key(path)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    try:
        response = await asyncio.to_thread(
            s3.get_object, Bucket=config.bucket, Key=key
        )
        body = response["Body"]
        content_length: int = response.get("ContentLength", 0)
        content_type: str = _content_type_for(key)

        async def _stream() -> object:
            try:
                while True:
                    chunk: bytes = await asyncio.to_thread(body.read, 65536)
                    if not chunk:
                        break
                    yield chunk
            finally:
                body.close()

        headers: dict[str, str] = {}
        if content_length:
            headers["content-length"] = str(content_length)
        return StreamingResponse(
            _stream(),
            media_type=content_type,
            headers=headers,
        )
    except ClientError as exc:
        if not _is_not_found(exc):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="object store error",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"object not found: {key}",
        ) from exc
    except Exception as exc:
        verbose_proxy_logger.warning("client_distribution: artifact read failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="object store unreachable",
        ) from exc
