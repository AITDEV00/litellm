"""Source connector release distribution over RustFS, using the shared artifact channel.

The Go ``adma`` connector ships from the same bucket as the cc-switch client,
uploaded the same way: an administrator PUTs raw bytes to the gateway, the gateway
writes them to RustFS with its own store credentials, and clients download them back
through the same proxy. There is no second transport, no base64-in-JSON bundle, and
no store credential on an operator machine.

Layout (see ``docs/distribution/client-distribution/source-agent-releases.md``):

    templates/source-agent-install.sh.tmpl   operator-owned installer logic
    source-agent/<version>/adma             immutable binary bytes
    source-agent/<version>/release.json      version/sha256/compat metadata
    source-agent/manifest.json               active-release pointer

The install script is *rendered per request* from the stored template plus the
validated active release: base URL, version and checksum are filled by the gateway
from values it trusts. A template edit therefore needs only its own upload (no
version bump, no manifest activation, no image rebuild); a new binary needs an
immutable ``source-agent/<version>/`` pair and an activated pointer.

The binary and its ``release.json`` are uploaded through the generic
``PUT /install/admin/v1/artifact/{path}`` route, which is immutable for those paths:
re-uploading different bytes at an already-published version is a conflict rather
than a silent replacement of a released executable.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from typing import TYPE_CHECKING, Annotated, Final, Protocol, cast

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
    from litellm.proxy.client_distribution.storage import RustFSConfig

router: Final = APIRouter(tags=["install"])

AGENT_PREFIX: Final = "source-agent"
MANIFEST_KEY: Final = f"{AGENT_PREFIX}/manifest.json"
TEMPLATE_KEY: Final = "templates/source-agent-install.sh.tmpl"
MAX_RELEASE_BYTES: Final = 64 * 1024 * 1024

BASE_PLACEHOLDER: Final = "__BASE__"
VERSION_PLACEHOLDER: Final = "__VERSION__"
SHA_PLACEHOLDER: Final = "__SHA__"
STATE_PLACEHOLDER: Final = "__STATE__"


class SourceAgentManifest(BaseModel):
    """The active-release pointer.

    Its own schema, not the cc-switch ``schema 5`` manifest: that DTO carries
    harness/provider fields a source-agent release has none of, so validating this
    document against it would check fields that do not exist here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    sha256: str
    size: int = 0

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
            raise ValueError("sha256 must be a lowercase hex SHA-256 digest")
        return value


class ReleaseDocument(BaseModel):
    """The immutable ``release.json`` stored beside each binary."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    version: str
    sha256: str
    size: int = 0


class _Body(Protocol):
    def read(self) -> bytes: ...


class _StoredObject(Protocol):
    @property
    def Body(self) -> _Body: ...

    def __getitem__(self, key: str) -> _Body: ...

    def get(self, key: str, default: object = ...) -> object: ...


class _Store(Protocol):
    """The S3 operations this slice uses, typed narrowly.

    The real boto3 client is untyped here (its stubs are not installed in the
    verification venv), so narrowing at this one boundary keeps the rest of the
    module fully typed instead of carrying Unknown through every call.
    """

    def get_object(self, *, Bucket: str, Key: str) -> _StoredObject: ...

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str,
        IfMatch: str | None = None,
        IfNoneMatch: str | None = None,
    ) -> object: ...


def _storage() -> tuple[RustFSConfig, _Store]:
    config: Final = load_config_from_env()
    client: Final = cast(_Store, create_s3_client(config))
    return config, client


def _require_admin(user_api_key_dict: UserAPIKeyAuth) -> None:
    if user_api_key_dict.user_role != LitellmUserRoles.PROXY_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only proxy admins can publish source-agent releases",
        )


def _is_missing(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code", "") in {"NoSuchKey", "404", "NotFound"}


def _base_url(request: Request) -> str:
    """The canonical gateway URL, resolved from trusted config first.

    Mirrors ``routes._get_base_url``: an explicit ``LITELLM_PUBLIC_BASE_URL`` wins,
    otherwise the request's forwarded scheme and host are used. The installer must
    download from the environment the client actually reached.
    """
    configured: Final = os.environ.get("LITELLM_PUBLIC_BASE_URL")
    if configured:
        return configured.rstrip("/")
    scheme: Final = request.headers.get("x-forwarded-proto", "https")
    host: Final = request.headers.get("host", "localhost")
    return f"{scheme}://{host}"


async def _active_manifest(config: RustFSConfig, s3: _Store) -> SourceAgentManifest:
    try:
        response = await asyncio.to_thread(s3.get_object, Bucket=config.bucket, Key=MANIFEST_KEY)
        raw: Final = response["Body"].read()
    except ClientError as exc:
        if _is_missing(exc):
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="no source-agent release published") from exc
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="object store error") from exc
    try:
        return SourceAgentManifest.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=f"corrupt source-agent manifest: {exc}") from exc


async def _verified_release(config: RustFSConfig, s3: _Store, manifest: SourceAgentManifest) -> ReleaseDocument:
    """Load a release's own ``release.json`` and check it agrees with the pointer.

    The pointer alone is not trusted: a mismatched version or digest between the
    pointer and the versioned document means the publication is inconsistent and must
    not be rendered into an installer.
    """
    try:
        response = await asyncio.to_thread(
            s3.get_object, Bucket=config.bucket, Key=f"{AGENT_PREFIX}/{manifest.version}/release.json"
        )
        raw: Final = response["Body"].read()
    except ClientError as exc:
        if _is_missing(exc):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=f"active version {manifest.version} has no release.json; republish it",
            ) from exc
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="object store error") from exc
    try:
        document: Final = ReleaseDocument.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=f"corrupt release.json: {exc}") from exc
    if document.version != manifest.version or document.sha256 != manifest.sha256:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="manifest pointer disagrees with the version it names; republish before serving",
        )
    return document


async def _render_installer(request: Request, manifest: SourceAgentManifest) -> str:
    """Load the stored template and fill its trusted values.

    The template is read at request time, so editing it needs no version bump. Every
    placeholder is replaced here, from the pointer the gateway just validated and its
    own canonical base URL; an unresolved placeholder is a failure, not an output.
    """
    config, s3 = _storage()
    try:
        response = await asyncio.to_thread(s3.get_object, Bucket=config.bucket, Key=TEMPLATE_KEY)
        template: Final = response["Body"].read().decode("utf-8")
    except ClientError as exc:
        if _is_missing(exc):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail=f"installer template {TEMPLATE_KEY} is not published; upload it before serving installs",
            ) from exc
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="object store error") from exc
    if not template.strip():
        raise HTTPException(status.HTTP_409_CONFLICT, detail="installer template is empty; republish it")

    rendered: Final = (
        template.replace(BASE_PLACEHOLDER, _base_url(request))
        .replace(VERSION_PLACEHOLDER, manifest.version)
        .replace(SHA_PLACEHOLDER, manifest.sha256)
        .replace(STATE_PLACEHOLDER, "${state}")
    )
    unresolved: Final = [
        name for name in (BASE_PLACEHOLDER, VERSION_PLACEHOLDER, SHA_PLACEHOLDER, STATE_PLACEHOLDER) if name in rendered
    ]
    if unresolved:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"installer template left placeholders unresolved: {unresolved}",
        )
    return rendered


@router.get("/install/source-agent")
async def source_agent_install_script(request: Request) -> Response:
    """Serve the installer rendered from the stored template and active release."""
    config, s3 = _storage()
    manifest: Final = await _active_manifest(config, s3)
    await _verified_release(config, s3, manifest)
    script: Final = await _render_installer(request, manifest)
    return Response(content=script, media_type="application/x-sh")


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


@router.get("/install/source-agent/{version}/{asset}")
async def source_agent_asset(version: str, asset: str) -> StreamingResponse:
    """Proxy one immutable release asset. Versioned paths only."""
    if asset not in {"adma", "release.json"} or "/" in version or version.startswith(".") or ".." in version:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="unknown release asset")
    config, s3 = _storage()
    key: Final = f"{AGENT_PREFIX}/{version}/{asset}"
    try:
        response = await asyncio.to_thread(s3.get_object, Bucket=config.bucket, Key=key)
    except ClientError as exc:
        if _is_missing(exc):
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="release asset not found") from exc
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="object store error") from exc
    content_type: Final = "application/json" if asset == "release.json" else "application/octet-stream"
    body: Final = response["Body"].read()
    return StreamingResponse(
        content=iter((body,)),
        media_type=content_type,
        headers={"Content-Length": str(len(body))},
    )


async def _read_optional(config: RustFSConfig, s3: _Store) -> tuple[bytes, str]:
    """Current pointer bytes plus a stable concurrency token.

    RustFS ETags can be opaque, so the token is the digest of the bytes we read.
    A caller that saw version A passes that digest back; if the pointer has moved
    to B meanwhile the digests differ and the write is refused, which is what makes
    two concurrent publishers fail instead of last-write-wins dropping one release.
    """
    try:
        response = await asyncio.to_thread(s3.get_object, Bucket=config.bucket, Key=MANIFEST_KEY)
        raw: Final = response["Body"].read()
        return raw, hashlib.sha256(raw).hexdigest()
    except ClientError as exc:
        if _is_missing(exc):
            return b"", ""
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="object store error") from exc


async def _read_pointer_state(config: RustFSConfig, s3: _Store) -> tuple[bytes, str, str | None]:
    """Pointer bytes, its content digest, and the object's ETag (``None`` if absent).

    The digest is the caller-facing token. The ETag is what a conditional write
    needs: the token check alone is read-then-write, so two publishers holding the
    same token both pass it. Passing this ETag as ``IfMatch`` on the write turns
    the exchange into one atomic conditional put instead of a check with a window.
    """
    try:
        response = await asyncio.to_thread(s3.get_object, Bucket=config.bucket, Key=MANIFEST_KEY)
        raw: Final = response["Body"].read()
        etag: Final = cast("str | None", response.get("ETag"))
        return raw, hashlib.sha256(raw).hexdigest(), etag
    except ClientError as exc:
        if _is_missing(exc):
            return b"", "", None
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="object store error") from exc


async def _head_digest(config: RustFSConfig, s3: _Store, key: str) -> str | None:
    """The digest of an already-stored object, or None when it is absent."""
    try:
        response = await asyncio.to_thread(s3.get_object, Bucket=config.bucket, Key=key)
        raw: Final = response["Body"].read()
        return hashlib.sha256(raw).hexdigest()
    except ClientError as exc:
        if _is_missing(exc):
            return None
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="object store error") from exc


class ActivateRequest(BaseModel):
    """Activate an already-uploaded, complete release.

    ``expected_current`` is the pointer digest the caller last observed, or the
    literal ``"absent"`` when it expects no pointer yet. Omitting it skips the
    compare, which is only appropriate for a deliberate first publication.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    sha256: str
    expected_current: str | None = None


@router.put(
    "/install/admin/v1/source-agent/activate",
    dependencies=[Depends(user_api_key_auth)],
)
async def admin_activate_release(
    request: Request,
    data: ActivateRequest,
    user_api_key_dict: Annotated[UserAPIKeyAuth, Depends(user_api_key_auth)],
) -> dict[str, object]:
    """Point the active manifest at one complete, already-uploaded release.

    Activation is separate from upload on purpose: the operator uploads the
    immutable binary and release.json first, and only then moves the pointer, so an
    interrupted publication leaves the previous installer and version serving. This
    route validates the referenced bytes, refuses an incomplete pair, and applies a
    compare-and-set on the pointer token so concurrent publishers cannot both win.
    """
    del request
    _require_admin(user_api_key_dict)
    config, s3 = _storage()

    binary_key: Final = f"{AGENT_PREFIX}/{data.version}/adma"
    binary_digest: Final = await _head_digest(config, s3, binary_key)
    if binary_digest is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"release {data.version} is not uploaded ({binary_key} is absent); upload before activating",
        )
    if binary_digest != data.sha256:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"{binary_key} holds {binary_digest}, not the requested {data.sha256}",
        )
    release_digest: Final = await _head_digest(config, s3, f"{AGENT_PREFIX}/{data.version}/release.json")
    if release_digest is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"release {data.version} has no release.json; upload the complete pair before activating",
        )

    _, current_token, current_etag = await _read_pointer_state(config, s3)
    if data.expected_current is not None:
        expected: Final = "" if data.expected_current == "absent" else data.expected_current
        if current_token != expected:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="active release moved since it was read; re-read and retry the activation",
            )

    pointer: Final = SourceAgentManifest(version=data.version, sha256=data.sha256)
    body: Final = pointer.model_dump_json().encode()
    try:
        await asyncio.to_thread(
            s3.put_object,
            Bucket=config.bucket,
            Key=MANIFEST_KEY,
            Body=body,
            ContentType="application/json",
            **({"IfMatch": current_etag} if current_etag else {"IfNoneMatch": "*"}),
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code", "") in {"PreconditionFailed", "412", "ConditionalRequestConflict"}:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="active release changed between read and write; re-read and retry the activation",
            ) from exc
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="object store error") from exc
    return {"ok": True, "version": pointer.version, "sha256": pointer.sha256, "previous": current_token or "absent"}


@router.get("/install/admin/v1/source-agent/state", dependencies=[Depends(user_api_key_auth)])
async def admin_release_state(
    user_api_key_dict: Annotated[UserAPIKeyAuth, Depends(user_api_key_auth)],
) -> dict[str, object]:
    """The current pointer bytes and its concurrency token, for a checked update."""
    _require_admin(user_api_key_dict)
    config, s3 = _storage()
    raw, token = await _read_optional(config, s3)
    return {"active": raw.decode("utf-8") if raw else None, "token": token or "absent"}
