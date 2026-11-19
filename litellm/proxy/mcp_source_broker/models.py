from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
from datetime import datetime
from typing import Final, Literal
from uuid import UUID, uuid5

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    field_validator,
    model_validator,
)

NAMESPACE: Final = UUID("67a55270-c485-45b4-8f1a-fc89c2e36078")
HOSTNAME: Final = TypeAdapter(str)
JSON_OBJECT: Final = TypeAdapter(dict[str, JsonValue])

# A source is either an OpenAPI-described HTTP service or a standard MCP server.
SourceKind = Literal["openapi", "mcp"]


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


# A Kubernetes service host is `name.namespace.svc` in the short form the client
# normalizes to. A pod may expose an origin off loopback, so the gateway must
# accept it too; it authorizes the exposure, not the client.
SERVICE_HOST: Final = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.svc$")


class Target(Frozen):
    """Where a source's origin listens, as the gateway stores it.

    The host is an IPv4 literal, a bracketed IPv6 literal, or a Kubernetes service
    name. It is never a URL, never carries userinfo or a path, and is not resolved
    here: the connector dials it from the source host, and the gateway only records
    and mirrors it.
    """

    host: str = Field(default="127.0.0.1", min_length=1, max_length=253)
    port: int = Field(18080, ge=1, le=65535)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        return canonical_worker_host(value)


def _is_ip_literal(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def canonical_worker_host(value: str) -> str:
    """Validate and normalize a worker target host to its canonical stored form.

    The worker row stores exactly what the reviewed source stores, so a service DNS
    name, a private IPv4, and an IPv6 literal must all survive insert, read, and
    projection. IPv6 is normalized through ``ipaddress`` so two spellings collapse to
    one, matching the client's ``target`` normalization; a service name is
    lowercased. This is the single validator the row DTO and the request ``Target``
    both reach, so the two boundaries cannot drift.
    """
    try:
        return ipaddress.ip_address(value).compressed
    except ValueError:
        if SERVICE_HOST.fullmatch(value):
            return value
        raise ValueError(
            "worker target host must be an IP address or a Kubernetes service name like name.namespace.svc"
        )


def canonical_hostport(host: str, port: int) -> str:
    """Render a host:port the way ``net.JoinHostPort`` does, bracketing IPv6.

    The connector parses this with ``net.SplitHostPort``, which requires a bracketed
    IPv6 address; emitting an unbracketed ``::1:9001`` would split on the wrong colon.
    """
    if _is_ip_literal(host) and ":" in host:
        return f"[{host}]:{port}"
    return f"{host}:{port}"


class SourceCreate(Frozen):
    """A self-service enrollment request.

    ``owner``, ``key`` and ``expires_at`` are server-derived: the connector
    must authenticate as its own gateway user without a team-membership
    precondition. Owner identity is verified from the gateway account, never
    decoded from an opaque key. These request fields remain accepted-but-ignored
    on this self-service path; see ``connector-owned-enrollment.md`` for the
    owner-facing bootstrap contract.

    A source is one of two kinds. ``openapi`` exposes an HTTP service described by
    an OpenAPI document, so ``spec`` is required and the gateway builds tools from
    it. ``mcp`` exposes a standard MCP server (streamable-http) reached at the
    origin's ``path``, so ``spec`` is empty and the gateway connects to it over the
    MCP protocol instead of loading a catalog. ``path`` is used only for ``mcp`` and
    defaults to ``/mcp``.
    """

    alias: str = Field(pattern=r"^[a-z][a-z0-9_.]{2,127}$")
    target: Target
    kind: SourceKind = "openapi"
    spec: str = Field(default="", max_length=600_000)
    path: str = Field(default="/mcp", pattern=r"^(/[A-Za-z0-9._~-]+)+$")
    owner: str = Field(default="", max_length=255)
    key: str = Field(default="", max_length=255)
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_kind(self) -> SourceCreate:
        if self.kind == "openapi" and self.spec == "":
            raise ValueError("an openapi source requires an OpenAPI spec")
        if self.kind == "mcp" and self.spec != "":
            raise ValueError("an mcp source does not take an OpenAPI spec")
        return self

    @field_validator("spec")
    @classmethod
    def validate_spec(cls, value: str) -> str:
        return canonical_spec(value) if value != "" else value

    @property
    def source_id(self) -> str:
        return self.source_id_for(self.owner, self.alias)

    @property
    def spec_path(self) -> str:
        return f"mcp-source-broker/{self.source_id}/openapi.json"

    def identity(self) -> str:
        # The claim token is gone; the gateway-auth ciphertext is keyed on the
        # owner and alias now, which are the only stable facts about a source.
        return hashlib.sha256(f"{self.owner}:{self.alias}".encode()).hexdigest()

    def fingerprint(self) -> str:
        # An openapi source is fingerprinted over its canonical catalog bytes. An
        # mcp source has no catalog, so its stable content is the kind plus the
        # endpoint path it serves; hashing that keeps re-enrollment idempotent the
        # same way without inventing a spec.
        if self.kind == "mcp":
            return hashlib.sha256(f"mcp:{self.path}".encode()).hexdigest()
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
    kind: SourceKind = "openapi"
    path: str = "/mcp"


TransportName = Literal["tunnel"]

# The only routes a connector's ordinary user key may call. This is the source
# self-service surface: enroll a source, register its worker, and read the
# connector identity it was issued. The private control plane
# (/v1/source-link/{peers,report,revoke}) is deliberately absent, and so is every
# admin route. A key minted for a connector carries exactly this list, so a
# leaked source-host credential cannot administer the gateway.
CONNECTOR_SELF_SERVICE_ROUTES: Final = (
    "/mcp-sources",
    "/mcp-source-link-sources/*",
)


class RouteSelection(Frozen):
    transport: TransportName = "tunnel"


class RemotePeer(Frozen):
    """A worker registration as the connector sends it.

    ``proof_key`` is optional and defaults to empty. The mandatory origin-proof
    signature it belonged to is removed from the supported local/OpenAPI flow: a
    connector that controls both the target response and the key proves nothing
    about who handled the request. A future independent origin integration can
    still populate it, and when present it is validated as an Ed25519 public key.
    """

    label: str = Field(pattern=r"^[a-z][a-z0-9-]{1,31}$")
    dataset_id: str = Field(min_length=1, max_length=255, pattern=r"^[^\r\n]+$")
    proof_key: str = ""
    target: Target

    @field_validator("proof_key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        if value == "":
            return value
        if len(base64.b64decode(value, validate=True)) != 32:
            raise ValueError("origin proof public key must be Ed25519")
        return value


class RemotePeerResponse(Frozen):
    """The connector runtime identity issued at worker creation.

    ``credential`` is the plaintext per-worker tunnel HMAC and ``generation`` is the
    fencing counter the tunnel presents. Both leave the gateway exactly once, over TLS,
    to the connector that registered the worker. Neither the pepper, the service token,
    the stored credential digest, nor any peer or digest projection is included.
    """

    worker_id: str
    source_id: str
    target: str
    generation: int
    credential: str
    expires_at: datetime
    label: str
    dataset_id: str


class SourceEnrolled(Frozen):
    """The response to ``POST /mcp-sources``.

    ``owner`` is the gateway-authenticated user id. The connector cannot derive it:
    a virtual key is opaque. ``source_id`` is the deterministic server-side uuid5.
    """

    source_id: str
    owner: str
    status: str


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
    kind: SourceKind = "openapi"


class RemoteHealthReport(Frozen):
    worker_id: str
    generation: int
    catalog_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class RemoteLifecycle(Frozen):
    worker_id: str
    source_id: str
    phase: Literal["active", "draining", "revoked"]
    generation: int


class LifecycleRequest(Frozen):
    generation: int = Field(ge=1)
