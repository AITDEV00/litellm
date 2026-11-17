from __future__ import annotations

import base64
import hashlib
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
)

NAMESPACE: Final = UUID("67a55270-c485-45b4-8f1a-fc89c2e36078")
HOSTNAME: Final = TypeAdapter(str)
JSON_OBJECT: Final = TypeAdapter(dict[str, JsonValue])


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


TransportName = Literal["reverse-h2", "tunnel"]


class RouteSelection(Frozen):
    transport: TransportName = "reverse-h2"


class RemotePeer(Frozen):
    label: str = Field(pattern=r"^[a-z][a-z0-9-]{1,31}$")
    dataset_id: str = Field(min_length=1, max_length=255, pattern=r"^[^\r\n]+$")
    proof_key: str = Field(pattern=r"^[A-Za-z0-9+/]{43}=?$")
    target: Target

    @field_validator("proof_key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        if len(base64.b64decode(value, validate=True)) != 32:
            raise ValueError("origin proof public key must be Ed25519")
        return value


class RemotePeerResponse(Frozen):
    worker_id: str
    source_id: str
    expires_at: datetime
    label: str
    dataset_id: str


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


class LifecycleRequest(Frozen):
    generation: int = Field(ge=1)
