"""Source connector release distribution over RustFS.

Extends the client-distribution slice for the MCP source broker's connector
(the Go `source-link` binary): the broker operator publishes a
release pair (binary, release.json) through the admin API, and source hosts
install through ``/install/source-agent`` with no secrets in any URL and no
generated per-host scripts. The installer is a fixed script that downloads the
versioned pair and verifies the SHA-256 checksum of the binary before anything
executes. The installed user systemd unit runs `source-link connector` with the
environment sourced from `~/.config/source-agent/connector.env`
(MCP_SOURCE_LINK_PUBLIC_URL, MCP_SOURCE_LINK_CONNECTOR_CONFIG). The connector
has a single transport, so the unit sets no transport variable.

Credential and registration behavior follow ``connector-owned-enrollment.md``.
One admin account creates ordinary user accounts. An ordinary user key with no
team is enough: ``POST /mcp-sources`` accepts it and returns the gateway-derived
owner, and the connector registers itself with that key and persists the
server-issued worker identity at mode 0600 (see ``internal/bootstrap`` and
``connector.LoadIdentity`` in the Go module). An ordinary user key must never be
confused with the sidecar/service token or the pepper, which never reach a
source host. What is still blocked is the approved live read on dev, because dev
has no public host serving both the broker REST routes and the tunnel; that is a
deployment gap, not this installer. See
``oicm-litellm-layer/docs/mcp-source-broker/decisions/approaches/connector-owned-enrollment.md``.

Reuse rules: connection parameters, bucket handling and key normalization all
come from :mod:`litellm.proxy.client_distribution.storage`. Only the
source-agent-specific manifest, upload validation and script rendering live
here.
"""

from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING, Annotated, Final, cast

from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from litellm.proxy._types import LitellmUserRoles
from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth, user_api_key_auth
from litellm.proxy.client_distribution.storage import (
    create_s3_client,  # pyright: ignore[reportUnknownVariableType]  # optional boto3 stubs
    load_config_from_env,
)

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client  # pyright: ignore[reportUnknownVariableType]  # stubs ship outside this venv

    from litellm.proxy.client_distribution.storage import RustFSConfig

router: Final = APIRouter(tags=["install"])

_AGENT_PREFIX: Final = "source-agent"
_MANIFEST_KEY: Final = f"{_AGENT_PREFIX}/manifest.json"
_MAX_RELEASE_BYTES: Final = 64 * 1024 * 1024


def _require_admin(user_api_key_dict: UserAPIKeyAuth) -> None:
    if user_api_key_dict.user_role != LitellmUserRoles.PROXY_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only proxy admins can publish source-agent releases",
        )


class _ReleasePayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: str
    sha256: str
    binary_b64: str
    release_json_b64: str
    # The installer is a stored release asset, so updating it needs no gateway
    # rebuild. It is optional only so an older client can republish a binary; a
    # release without it cannot be installed from, and the install route says so.
    install_script_b64: str = ""

    @field_validator("version")
    @classmethod
    def plain_version(cls, value: str) -> str:
        if not value or len(value) > 64 or value.startswith(".") or "/" in value or ".." in value:
            raise ValueError("version must be a plain path segment")
        return value

    @field_validator("sha256")
    @classmethod
    def hex_digest(cls, value: str) -> str:
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("sha256 must be a hex SHA-256 digest")
        return value


class _StoredManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: str
    sha256: str


def _s3() -> tuple[RustFSConfig, S3Client]:  # pyright: ignore[reportUnknownParameterType, reportUnknownVariableType, reportGeneralTypeIssues]  # mypy_boto3_s3 stubs are absent in the venv used for local checks
    config: Final = load_config_from_env()
    client: Final = create_s3_client(config)  # pyright: ignore[reportUnknownVariableType]  # mypy_boto3_s3 stubs are absent locally
    return config, client  # pyright: ignore[reportUnknownVariableType]  # tuple element carries the stubless-S3 Unknown


def _validated_install_script(encoded: str) -> bytes:
    """Decode and check the stored install script, or return empty bytes.

    A release may omit the script (an older client republishing a binary); the
    install route then reports that the release cannot be installed from. A
    present script must be a real installer that drives `init`.
    """
    if not encoded:
        return b""
    import binascii

    try:
        script: Final = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="install script is not valid base64") from exc
    if len(script) > _MAX_RELEASE_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="install script too large")
    if b" init" not in script:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="install script must invoke 'source-link init' so a new user is prompted",
        )
    return script


async def _load_manifest(config: RustFSConfig, s3: S3Client) -> _StoredManifest:  # pyright: ignore[reportUnknownParameterType, reportUnknownVariableType]  # mypy_boto3_s3 stubs are absent locally
    import asyncio

    try:
        response = await asyncio.to_thread(s3.get_object, Bucket=config.bucket, Key=_MANIFEST_KEY)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType, reportUnknownVariableType]  # mypy_boto3_s3 stubs are absent locally
        return _StoredManifest.model_validate_json(response["Body"].read())  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]  # mypy_boto3_s3 stubs are absent locally
    except ClientError as exc:
        code: Final = exc.response.get("Error", {}).get("Code", "")
        if code in {"NoSuchKey", "404", "NotFound"}:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="no source-agent release published") from exc
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="object store error") from exc
    except ValidationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=f"corrupt release manifest: {exc}") from exc


@router.put(
    "/install/admin/v1/source-agent/release",
    dependencies=[Depends(user_api_key_auth)],
)
async def admin_publish_release(
    request: Request,
    user_api_key_dict: Annotated[UserAPIKeyAuth, Depends(user_api_key_auth)],
) -> dict[str, object]:
    """Publish a release: binary + release.json.

    The client uploads the two files as one JSON body carrying base64 fields;
    the gateway verifies the binary digest matches release.json, then writes
    each immutable, versioned asset plus the current-release pointer, manifest
    last. The route is admin-only; there is no separate signature on top of
    that, because the checksum the installer enforces is derived from what the
    gateway itself published.
    """
    _require_admin(user_api_key_dict)
    raw: Final = await request.body()
    if len(raw) > _MAX_RELEASE_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="release payload too large")
    try:
        data: Final = _ReleasePayload.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=exc.errors()[0]["msg"]) from exc
    import binascii

    try:
        binary: Final = base64.b64decode(data.binary_b64, validate=True)
        manifest_body: Final = base64.b64decode(data.release_json_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="base64 field is not valid base64") from exc
    if len(binary) > _MAX_RELEASE_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="binary too large")
    version: Final = data.version
    try:
        manifest: Final = _StoredManifest.model_validate_json(manifest_body)
    except ValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"release.json is not a valid manifest: {exc}") from exc
    if manifest.version != version or manifest.sha256 != data.sha256:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="release.json does not agree with the payload version or digest"
        )
    if hashlib.sha256(binary).hexdigest() != data.sha256:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="binary digest does not match release.json sha256")

    import asyncio

    config, s3 = _s3()  # pyright: ignore[reportUnknownVariableType]  # mypy_boto3_s3 stubs are absent locally
    binary_key: Final = f"{_AGENT_PREFIX}/{version}/source-link"
    try:
        await asyncio.to_thread(
            s3.head_object,  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]  # mypy_boto3_s3 stubs are absent locally
            Bucket=config.bucket,
            Key=binary_key,
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code", "") not in {"NoSuchKey", "404", "NotFound"}:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="object store error") from exc
    else:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="release version is already published and immutable")
    install_script: Final = _validated_install_script(data.install_script_b64)
    assets: Final = [
        (binary_key, binary, "application/octet-stream"),
        (f"{_AGENT_PREFIX}/{version}/release.json", manifest_body, "application/json"),
    ]
    if install_script:
        assets.append((f"{_AGENT_PREFIX}/{version}/install.sh", install_script, "application/x-sh"))
    for key, body, content_type in assets:
        await asyncio.to_thread(
            s3.put_object,  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]  # mypy_boto3_s3 stubs are absent locally
            Bucket=config.bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
        )
    # The pointer moves last, after every referenced asset is durable, so an
    # interrupted publish never activates a half-written release.
    await asyncio.to_thread(
        s3.put_object,  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]  # mypy_boto3_s3 stubs are absent locally
        Bucket=config.bucket,
        Key=_MANIFEST_KEY,
        Body=manifest_body,
        ContentType="application/json",
    )
    return {"ok": True, "version": version, "sha256": data.sha256, "size": len(binary)}


@router.get("/install/source-agent")
async def source_agent_install_script(request: Request) -> Response:
    """Serve the current source-agent release's install script.

    The script is a stored, validated release object, not Python-rendered per
    request, so changing it needs only a new upload: the gateway image and pod
    stay untouched. An older release that predates stored scripts has no
    ``install.sh`` object; that case is a clear 409 telling the operator to
    republish with a script, never a silent fallback to generated content.
    """
    import asyncio

    config, s3 = _s3()  # pyright: ignore[reportUnknownVariableType]  # mypy_boto3_s3 stubs are absent locally
    manifest = await _load_manifest(config, s3)  # pyright: ignore[reportUnknownArgumentType]  # mypy_boto3_s3 stubs are absent locally
    script_key: Final = f"{_AGENT_PREFIX}/{manifest.version}/install.sh"
    try:
        response = await asyncio.to_thread(s3.get_object, Bucket=config.bucket, Key=script_key)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType, reportUnknownVariableType]  # mypy_boto3_s3 stubs are absent locally
        script_bytes: Final = cast(bytes, response["Body"].read())  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]  # mypy_boto3_s3 stubs are absent locally
    except ClientError as exc:
        code: Final = exc.response.get("Error", {}).get("Code", "")
        if code in {"NoSuchKey", "404", "NotFound"}:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="the active release has no stored install script; republish it with install.sh",
            ) from exc
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="object store error") from exc
    return Response(content=script_bytes, media_type="application/x-sh")


@router.get("/install/source-agent/{version}/{asset}")
async def source_agent_asset(version: str, asset: str) -> StreamingResponse:
    """Proxy one release asset read from RustFS. Versioned paths only."""
    import asyncio

    if asset not in {"source-link", "release.json", "install.sh"} or "/" in version or version.startswith("."):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown release asset")
    config, s3 = _s3()  # pyright: ignore[reportUnknownVariableType]  # mypy_boto3_s3 stubs are absent locally
    key: Final = f"{_AGENT_PREFIX}/{version}/{asset}"
    try:
        response = await asyncio.to_thread(s3.get_object, Bucket=config.bucket, Key=key)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType, reportUnknownVariableType]  # mypy_boto3_s3 stubs are absent locally
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404", "NotFound"):
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="release asset not found") from exc
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="object store error") from exc
    content_type: Final = {
        "source-link": "application/octet-stream",
        "install.sh": "application/x-sh",
    }.get(asset, "application/json")
    return StreamingResponse(
        content=response["Body"],  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]  # mypy_boto3_s3 stubs are absent locally
        media_type=content_type,
        headers={"Content-Length": str(response.get("ContentLength", 0))},  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]  # mypy_boto3_s3 stubs are absent locally
    )
