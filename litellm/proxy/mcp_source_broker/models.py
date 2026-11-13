from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Final, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid5

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    field_serializer,
    field_validator,
)

NAMESPACE: Final = UUID("67a55270-c485-45b4-8f1a-fc89c2e36078")
HOSTNAME: Final = TypeAdapter(str)
JSON_OBJECT: Final = TypeAdapter(dict[str, JsonValue])


def _validate_base_url(value: str, *, https_only: bool) -> str:
    parsed: Final = urlsplit(value)
    if parsed.scheme not in ({"https"} if https_only else {"https", "http"}):
        raise ValueError("URL scheme is not allowed")
    try:
        port: Final = parsed.port
    except ValueError as error:
        raise ValueError("URL port is invalid") from error
    hostname: Final = HOSTNAME.validate_python(parsed.hostname)
    invalid_scheme_host: Final = parsed.scheme == "http" and hostname not in {"127.0.0.1", "localhost"}
    if re.fullmatch(r"[a-zA-Z0-9.-]+", hostname) is None or invalid_scheme_host:
        raise ValueError("URL host is not allowed")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("URL port is invalid")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError("URL must not contain credentials, a query, or a fragment")
    path: Final = parsed.path.removesuffix("/")
    if not path:
        if https_only:
            raise ValueError("relay URL must end in /mcp-worker")
        return value.removesuffix("/")
    if any(segment in {"", ".", ".."} for segment in path.split("/")[1:]):
        raise ValueError("URL path must be canonical")
    if https_only and path != "/mcp-worker":
        raise ValueError("relay URL must end in /mcp-worker")
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def canonical_spec(document: str | bytes) -> str:
    parsed: Final = JSON_OBJECT.validate_json(document)
    version: Final = parsed.get("openapi")
    if not isinstance(version, str) or re.fullmatch(r"3\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version) is None:
        raise ValueError("spec must declare an OpenAPI 3 version")
    if not isinstance(parsed.get("paths"), dict):
        raise ValueError("spec must contain an OpenAPI paths object")  # noqa: TRY004  # public catalog contract
    canonical: Final = json.dumps(parsed, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(canonical) > 500_000:
        raise ValueError("canonical catalog exceeds 500000 bytes")
    return canonical.decode()


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class Target(Frozen):
    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: int = Field(18080, ge=1, le=65535)


class SourceCreate(Frozen):
    owner: str = Field(min_length=1, max_length=255)
    alias: str = Field(pattern=r"^[a-z][a-z0-9-]{2,127}$")
    key: str = Field(min_length=16, max_length=255)
    expires_at: AwareDatetime
    target: Target
    spec: str = Field(max_length=600_000)

    @field_validator("spec")
    @classmethod
    def validate_spec(cls, value: str) -> str:
        return canonical_spec(value)

    @property
    def source_id(self) -> str:
        return self.source_id_for(self.owner, self.alias)

    @property
    def spec_path(self) -> str:
        return f"mcp-source-broker/{self.source_id}/openapi.json"

    def identity(self) -> str:
        return hashlib.sha256(self.key.encode()).hexdigest()

    def fingerprint(self) -> str:
        return hashlib.sha256(self.spec.encode()).hexdigest()

    def with_owner(self, owner: str) -> SourceCreate:
        return self.model_copy(update={"owner": owner})

    @staticmethod
    def source_id_for(owner: str, alias: str) -> str:
        return str(uuid5(NAMESPACE, f"source:{owner}:{alias}"))


class SourceRecord(Frozen):
    source_id: str
    tenant: str
    key: str
    alias: str
    target: Target
    spec_path: str
    expected: str
    gateway_auth_ciphertext: str
    status: str
    revision: int


class EnrollmentRow(Frozen):
    id: str
    source_id: str
    token_hash: bytes
    expires_at: datetime
    redeemed_at: datetime | None


class Member(Frozen):
    connector_id: str
    source_id: str
    relay_host: str
    relay_port: int
    credential_digest: bytes
    healthy_at: datetime | None
    catalog_fingerprint: str | None
    status: str
    generation: int


class MemberView(Frozen):
    connector_id: str
    source_id: str
    relay_host: str
    relay_port: int
    healthy_at: datetime | None
    catalog_fingerprint: str | None
    status: str
    generation: int


class Claim(Frozen):
    source: SourceRecord
    member: MemberView
    username: str
    password: str


class EnrollResponse(Frozen):
    source_id: str
    enrollment_id: str
    token: str | None
    install_path: str


class RemotePeer(Frozen):
    label: str = Field(pattern=r"^[a-z][a-z0-9-]{1,31}$")
    dataset_id: str = Field(min_length=1, max_length=255, pattern=r"^[^\r\n]+$")
    proof_key: str = Field(pattern=r"^[A-Za-z0-9+/]{43}=?$")
    target: Target

    @field_validator("proof_key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        import base64

        if len(base64.b64decode(value, validate=True)) != 32:
            raise ValueError("origin proof public key must be Ed25519")
        return value


class RouteSelection(Frozen):
    transport: Literal["chisel", "remotedialer"] = "chisel"


class RemotePeerResponse(Frozen):
    worker_id: str
    source_id: str
    enrollment_token: str
    install_path: str
    expires_at: datetime
    label: str
    dataset_id: str


class RemoteRemoteInstall(Frozen):
    worker_id: str
    source_id: str
    target: Target
    dataset_id: str
    generation: int
    credential: str

    @field_serializer("target")
    def serialize_target(self, value: Target) -> str:
        return f"{value.host}:{value.port}"


class RemoteGoPeer(Frozen):
    worker_id: str
    source_id: str
    label: str
    target: str
    dataset_id: str
    proof_key: str
    generation: int
    phase: str
    credential_digest: str
    expected_catalog: str
    catalog_fingerprint: str | None
    healthy_at: datetime | None


class RemoteHealthReport(Frozen):
    worker_id: str
    generation: int
    catalog_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class RemoteLifecycle(Frozen):
    worker_id: str
    source_id: str
    phase: Literal["active", "draining", "revoked"]
    generation: int
    install_path: str | None = None
    enrollment_token: str | None = None


class LifecycleRequest(Frozen):
    generation: int = Field(ge=1)


class Health(Frozen):
    ok: bool


class RelayConfig(Frozen):
    relay_url: str
    public_url: str
    fingerprint: str = Field(pattern=r"^[0-9a-zA-Z+/=]+$")
    chisel_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("relay_url")
    @classmethod
    def validate_relay_url(cls, value: str) -> str:
        return _validate_base_url(value, https_only=True)

    @field_validator("public_url")
    @classmethod
    def validate_public_url(cls, value: str) -> str:
        return _validate_base_url(value, https_only=False)

    def catalog_url(self, path: str) -> str:
        return f"http://127.0.0.1:4000/install/{path}"


class ClaimRequest(Frozen):
    enrollment_id: str
    token: str


class ProbeRequest(Frozen):
    connector_id: str
    credential_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProbeResponse(Frozen):
    source_id: str
    status: str


class RelayUser(Frozen):
    connector_id: str
    source_id: str
    relay_host: str
    relay_port: int
    username: str
    password: str
    target_port: int
    status: str
    generation: int


class RelayMember(RelayUser):
    enrollment_id: str


class Enrollment(Frozen):
    source: SourceRecord
    enrollment_id: str
    token: str | None

    @classmethod
    def create(cls, owner: str, request: SourceCreate, enrollment_id: str, token: str) -> Enrollment:
        source: Final = SourceRecord(
            source_id=SourceCreate.source_id_for(owner, request.alias),
            tenant=owner,
            key=hashlib.sha256(request.key.encode()).hexdigest(),
            alias=request.alias,
            target=request.target,
            spec_path=f"mcp-source-broker/{SourceCreate.source_id_for(owner, request.alias)}/openapi.json",
            expected=hashlib.sha256(request.spec.encode()).hexdigest(),
            gateway_auth_ciphertext="",
            status="pending",
            revision=1,
        )
        return cls(source=source, enrollment_id=enrollment_id, token=token)
