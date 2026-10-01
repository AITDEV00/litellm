"""Typed OICM REST wire payloads.

These models validate OICM REST responses at the client boundary, so the rest
of the controller works with typed objects instead of ``dict[str, Any]``. Field
names match the OICM wire format; ``extra="allow"`` keeps forward-compatible
fields OICM adds later without breaking parsing, and every field is optional so
a renamed/dropped key degrades to ``None`` instead of raising.

``status_detail`` is a discriminated union on ``kind``: each K8s object kind has
its own model with its own typed ``metadata``, so availability logic matches on
the typed variant rather than poking a loose dict with string keys.
"""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

KIND_DEPLOYMENT: Final = "Deployment"
KIND_POD: Final = "Pod"
KIND_LEADER_WORKER_SET: Final = "LeaderWorkerSet"

_EXTRA = ConfigDict(extra="allow", populate_by_name=True)


class _StatusDetailBase(BaseModel):
    model_config = _EXTRA

    apiVersion: Optional[str] = None
    name: Optional[str] = None
    node: Optional[str] = None
    status: Optional[str] = None
    status_msg: Optional[str] = None


class DeploymentStatusDetailMeta(BaseModel):
    model_config = _EXTRA

    available: Optional[bool] = None
    available_replicas: Optional[int] = None
    progressing: Optional[bool] = None


class DeploymentStatusDetail(_StatusDetailBase):
    kind: Literal["Deployment"] = "Deployment"
    metadata: Optional[DeploymentStatusDetailMeta] = None


class PodStatusDetailMeta(BaseModel):
    model_config = _EXTRA

    ready: Optional[bool] = None


class PodStatusDetail(_StatusDetailBase):
    kind: Literal["Pod"] = "Pod"
    metadata: Optional[PodStatusDetailMeta] = None


class LeaderWorkerSetStatusDetailMeta(BaseModel):
    model_config = _EXTRA

    available: Optional[bool] = None


class LeaderWorkerSetStatusDetail(_StatusDetailBase):
    kind: Literal["LeaderWorkerSet"] = "LeaderWorkerSet"
    metadata: Optional[LeaderWorkerSetStatusDetailMeta] = None


StatusDetail = Annotated[
    Union[
        DeploymentStatusDetail,
        PodStatusDetail,
        LeaderWorkerSetStatusDetail,
    ],
    Field(discriminator="kind"),
]

_KNOWN_KINDS: Final = frozenset({KIND_DEPLOYMENT, KIND_POD, KIND_LEADER_WORKER_SET})


class OicmDeployment(BaseModel):
    model_config = _EXTRA

    id: str
    workspace_id: Optional[str] = None
    name: Optional[str] = None
    status: Optional[str] = None
    error_msg: Optional[str] = None
    replicas: Optional[int] = None
    enable_auto_scaling: Optional[bool] = None
    version: Optional[int] = Field(default=None, alias="_version")
    updated_at: Optional[str] = Field(default=None, alias="_updated_at")


class OicmDeploymentHealth(BaseModel):
    model_config = _EXTRA

    is_health_check_supported: Optional[bool] = None
    is_ready: Optional[bool] = None
    message: Optional[str] = None


class OicmWorkloadRun(BaseModel):
    model_config = _EXTRA

    id: str
    workload_id: Optional[str] = None
    workspace_id: Optional[str] = None
    workload_status: Optional[str] = None
    status_detail: tuple[StatusDetail, ...] = ()

    @field_validator("status_detail", mode="before")
    @classmethod
    def _drop_unknown_kinds(cls, value: Any) -> Any:
        # Forward-compat: skip status_detail entries whose ``kind`` we have not
        # modeled, so a new OICM kind cannot crash the discriminated union. The
        # dropped entry counts as not-available, which is the safe default.
        if not isinstance(value, (list, tuple)):
            return value
        return [
            e
            for e in value
            if not isinstance(e, dict) or e.get("kind") in _KNOWN_KINDS
        ]

    @property
    def ready_pod_count(self) -> int:
        return sum(
            1
            for e in self.status_detail
            if isinstance(e, PodStatusDetail) and e.metadata is not None and e.metadata.ready
        )
