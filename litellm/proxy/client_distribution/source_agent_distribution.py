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
import os
from typing import TYPE_CHECKING, Annotated, Final

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
    for key, body, content_type in (
        (binary_key, binary, "application/octet-stream"),
        (f"{_AGENT_PREFIX}/{version}/release.json", manifest_body, "application/json"),
        (_MANIFEST_KEY, manifest_body, "application/json"),
    ):
        await asyncio.to_thread(
            s3.put_object,  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]  # mypy_boto3_s3 stubs are absent locally
            Bucket=config.bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
        )
    return {"ok": True, "version": version, "sha256": data.sha256, "size": len(binary)}


def _render_source_agent_installer(base_url: str, version: str, sha256: str) -> str:
    return f"""#!/bin/sh
set -eu
umask 077
state="${{SOURCE_AGENT_STATE:-$HOME/.config/source-agent}}"
mkdir -p "$state" "$HOME/.config/systemd/user"
chmod 700 "$state" "$HOME/.config/systemd/user"
base="{base_url}"
version="{version}"
expected="{sha256}"
for asset in source-link release.json; do
  curl --fail --location --silent --show-error \\
    --output "$state/$asset.new" "$base/install/source-agent/$version/$asset"
  mv -f "$state/$asset.new" "$state/$asset"
done
actual=$(sha256sum "$state/source-link" | cut -d ' ' -f 1)
[ "$actual" = "$expected" ] || {{ echo 'source connector checksum mismatch' >&2; exit 1; }}
chmod 500 "$state/source-link"
cat > "$HOME/.config/systemd/user/source-agent.service" <<'UNIT'
[Unit]
Description=ADEO MCP source connector (tunnel)

[Service]
Restart=always
RestartSec=2s
EnvironmentFile=-$HOME/.config/source-agent/connector.env
ExecStart=$state/source-link connector
NoNewPrivileges=yes

[Install]
WantedBy=default.target
UNIT
if [ "${{SOURCE_AGENT_START:-1}}" = "1" ] && [ -f "$HOME/.config/source-agent/connector.env" ]; then
  systemctl --user daemon-reload
  systemctl --user enable --now source-agent.service
else
  echo "source connector installed at $state/source-link (service starts after MCP_SOURCE_LINK_CONNECTOR_CONFIG is set in $state/connector.env)"
fi
"""


@router.get("/install/source-agent")
async def source_agent_install_script(request: Request) -> Response:
    """Serve the fixed installer script for the current source-agent release."""
    import asyncio

    config, s3 = _s3()  # pyright: ignore[reportUnknownVariableType]  # mypy_boto3_s3 stubs are absent locally
    try:
        response = await asyncio.to_thread(s3.get_object, Bucket=config.bucket, Key=_MANIFEST_KEY)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType, reportUnknownVariableType]  # mypy_boto3_s3 stubs are absent locally
        manifest: Final = _StoredManifest.model_validate_json(response["Body"].read())  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]  # mypy_boto3_s3 stubs are absent locally
    except ClientError as exc:
        code: Final = exc.response.get("Error", {}).get("Code", "")
        if code in {"NoSuchKey", "404", "NotFound"}:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="no source-agent release published") from exc
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="object store error") from exc
    except ValidationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=f"corrupt release manifest: {exc}") from exc
    version: Final = manifest.version
    sha256: Final = manifest.sha256
    base_url: Final = os.environ.get("LITELLM_PUBLIC_BASE_URL") or (
        f"{request.headers.get('x-forwarded-proto', 'https')}://{request.headers.get('host', 'localhost')}"
    )  # pyright: ignore[reportUnknownArgumentType]  # header get returns Unknown in this stubless venv
    script: Final = _render_source_agent_installer(
        base_url,
        version,
        sha256,
    )
    return Response(content=script, media_type="application/x-sh")


@router.get("/install/source-agent/{version}/{asset}")
async def source_agent_asset(version: str, asset: str) -> StreamingResponse:
    """Proxy one release asset read from RustFS. Versioned paths only."""
    import asyncio

    if asset not in {"source-link", "release.json"} or "/" in version or version.startswith("."):
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
    content_type: Final = "application/octet-stream" if asset == "source-link" else "application/json"
    return StreamingResponse(
        content=response["Body"],  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]  # mypy_boto3_s3 stubs are absent locally
        media_type=content_type,
        headers={"Content-Length": str(response.get("ContentLength", 0))},  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]  # mypy_boto3_s3 stubs are absent locally
    )
