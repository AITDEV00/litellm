"""Source connector signed-release distribution over RustFS.

Extends the client-distribution slice for the MCP source broker's reverse-h2
connector (the Go `source-link` binary): the broker operator publishes a
signed release triple (binary, release.json, detached Ed25519 signature)
through the admin API, and source hosts install through
``/install/source-agent`` with no secrets in any URL and no generated
per-host scripts. The installer is a fixed script that downloads the
versioned triple, verifies the SHA-256 checksum and the release signature
against the pinned public key before anything executes. The installed user
systemd unit runs `source-link connector` with the environment sourced from
`~/.config/source-agent/connector.env` (MCP_REMOTEDIALER_PUBLIC_URL,
MCP_REMOTEDIALER_CONNECTOR_CONFIG).

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

_TRUSTED_KEY_B64: Final = os.environ.get("SOURCE_AGENT_RELEASE_PUBLIC_KEY_B64", "")


def _load_trusted_key(raw_b64: str) -> bytes:
    if not raw_b64:
        return b""
    try:
        key: Final = base64.b64decode(raw_b64, validate=True)
        if len(key) != 32:
            raise ValueError("not 32 bytes")
    except Exception as exc:
        raise RuntimeError("SOURCE_AGENT_RELEASE_PUBLIC_KEY_B64 must be a base64 Ed25519 public key") from exc
    return key


_TRUSTED_KEY: Final = _load_trusted_key(_TRUSTED_KEY_B64)

router: Final = APIRouter(tags=["install"])

_AGENT_PREFIX: Final = "source-agent"
_MANIFEST_KEY: Final = f"{_AGENT_PREFIX}/manifest.json"
_MAX_RELEASE_BYTES: Final = 64 * 1024 * 1024


def _verify_release_signature(manifest_body: bytes, signature_b64: str) -> bool:
    if len(_TRUSTED_KEY) != 32:
        return False
    try:
        signature: Final = base64.b64decode(signature_b64, validate=True)
    except (ValueError, TypeError):
        return False
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        Ed25519PublicKey.from_public_bytes(_TRUSTED_KEY).verify(signature, manifest_body)
    except InvalidSignature:
        return False
    return True


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
    release_sig_b64: str
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

    @field_validator("release_sig_b64")
    @classmethod
    def signature(cls, value: str) -> str:
        if len(base64.b64decode(value, validate=True)) != 64:
            raise ValueError("release_sig_b64 must be an Ed25519 signature")
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
    """Publish a signed release: binary + release.json + detached signature.

    The client uploads the three files as one JSON body carrying base64
    fields; the gateway validates the signature against the pinned release
    key, verifies the binary digest matches release.json, then writes each
    immutable, versioned asset plus the current-release pointer, manifest last.
    """
    _require_admin(user_api_key_dict)
    if not _TRUSTED_KEY:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="source-agent release verification key is not configured",
        )
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
    if not _verify_release_signature(manifest_body, data.release_sig_b64):
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="release signature failed verification against the pinned key"
        )

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
        (f"{_AGENT_PREFIX}/{version}/release.sig.b64", data.release_sig_b64.encode(), "text/plain"),
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


def _render_source_agent_installer(base_url: str, version: str, sha256: str, public_key_b64: str) -> str:
    public_pem: Final = f"-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VwAyEA{public_key_b64}\n-----END PUBLIC KEY-----"
    return f"""#!/bin/sh
set -eu
umask 077
state="${{SOURCE_AGENT_STATE:-$HOME/.config/source-agent}}"
mkdir -p "$state" "$HOME/.config/systemd/user"
chmod 700 "$state" "$HOME/.config/systemd/user"
base="{base_url}"
version="{version}"
expected="{sha256}"
for asset in source-link release.json release.sig.b64; do
  curl --fail --location --silent --show-error \\
    --output "$state/$asset.new" "$base/install/source-agent/$version/$asset"
  mv -f "$state/$asset.new" "$state/$asset"
done
cat > "$state/release-public.pem" <<'KEY'
{public_pem}
KEY
base64 -d "$state/release.sig.b64" > "$state/release.sig"
openssl pkeyutl -verify -pubin -inkey "$state/release-public.pem" -rawin \\
  -in "$state/release.json" -sigfile "$state/release.sig" >/dev/null 2>&1 || {{
  echo 'source connector signature verification failed' >&2
  exit 1
}}
actual=$(sha256sum "$state/source-link" | cut -d ' ' -f 1)
[ "$actual" = "$expected" ] || {{ echo 'source connector checksum mismatch' >&2; exit 1; }}
chmod 500 "$state/source-link"
cat > "$HOME/.config/systemd/user/source-agent.service" <<'UNIT'
[Unit]
Description=ADEO MCP source connector (reverse-h2)

[Service]
Restart=always
RestartSec=2s
Environment=MCP_REMOTEDIALER_TRANSPORT=reverse-h2
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
  echo "source connector installed at $state/source-link (service starts after MCP_REMOTEDIALER_CONNECTOR_CONFIG is set in $state/connector.env)"
fi
"""


@router.get("/install/source-agent")
async def source_agent_install_script(request: Request) -> Response:
    """Serve the fixed installer script for the current source-agent release."""
    import asyncio

    if not _TRUSTED_KEY:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="source connector release verification key is not configured",
        )
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
        _TRUSTED_KEY_B64,
    )
    return Response(content=script, media_type="application/x-sh")


@router.get("/install/source-agent/{version}/{asset}")
async def source_agent_asset(version: str, asset: str) -> StreamingResponse:
    """Proxy one release asset read from RustFS. Versioned paths only."""
    import asyncio

    if asset not in {"source-link", "release.json", "release.sig.b64"} or "/" in version or version.startswith("."):
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
    content_type: Final = (
        "application/octet-stream"
        if asset == "source-link"
        else "application/json"
        if asset == "release.json"
        else "text/plain"
    )
    return StreamingResponse(
        content=response["Body"],  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]  # mypy_boto3_s3 stubs are absent locally
        media_type=content_type,
        headers={"Content-Length": str(response.get("ContentLength", 0))},  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]  # mypy_boto3_s3 stubs are absent locally
    )
