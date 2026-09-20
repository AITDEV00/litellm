"""Runtime information and fact provenance. Canonical domain: no runtime imports."""

from pydantic import BaseModel, Field


class RuntimeInfo(BaseModel):
    kind: str
    version: str | None = None
    deployment_id: str
    api_base_fingerprint: str | None = None


class FactSource(BaseModel):
    probe: str
    field: str | None = None


class ModelProvenance(BaseModel):
    facts: dict[str, FactSource] = Field(default_factory=dict)