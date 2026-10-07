"""Gateway-observed availability for one deployment.

The OICM controller is the authority on whether a deployment can serve. It
records that verdict twice: the lifecycle and serving facts in
``model_info.oicm`` (per model), and a native health row (per deployment, keyed
by the deployment id). This module turns those facts into one
``gateway_status`` object, and owns the single place where OICM's lifecycle
vocabulary becomes a gateway availability verdict.

It also owns the mapping onto OpenRouter's numeric ``PublicEndpoint.status``.
That enum is documented nowhere: OpenRouter's spec lists six bare integers with
no description, no docs page and no sibling schema, and every published example
uses only ``0``. The mapping below is therefore our own reading, kept in one
function so a correction is a single edit. See
``docs/oicm-status/FEASIBILITY-ANSWERS.md`` for the decision record.

Freshness is judged per source, not per model: model health is written hourly
or on change, so its ``observed_at`` is routinely older than the source that
produced it. Only the source heartbeat says whether anyone is still watching.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict

from litellm.proxy.openrouter_compat.openrouter_schema.endpoints import EndpointStatus

Availability: TypeAlias = Literal["online", "degraded", "offline", "unknown"]

# Matches the controller's own STATUS_STALE_AFTER default. The controller
# heartbeats at a third of this, so a single missed cycle does not read stale.
STATUS_STALE_AFTER_SECONDS: Final = 90

_HEALTHY: Final = "healthy"

_SERVING_STATUSES: Final[frozenset[str]] = frozenset({"Ready", "Available"})
_DEPLOYING_STATUSES: Final[frozenset[str]] = frozenset({"Deploying", "Pending"})
_STOPPED_STATUSES: Final[frozenset[str]] = frozenset({"Stopped", "Undeploying"})

_AVAILABILITY_BY_SERVING: Final[Mapping[bool | None, Availability]] = {
    True: "online",
    False: "degraded",
    None: "unknown",
}

# OpenRouter ``PublicEndpoint.status`` values. Unverified: see the module
# docstring. ``-1`` is deliberately unassigned; it is reserved for a load-based
# signal that needs SGLang server-side telemetry we do not collect yet.
_ENDPOINT_STATUS_OK: Final[EndpointStatus] = 0
_ENDPOINT_STATUS_ATTENTION: Final[EndpointStatus] = -2
_ENDPOINT_STATUS_DEPLOYING: Final[EndpointStatus] = -3
_ENDPOINT_STATUS_FAILED: Final[EndpointStatus] = -5
_ENDPOINT_STATUS_STOPPED: Final[EndpointStatus] = -10

# Lifecycle wins over availability: a stopped or failed deployment is that, even
# if a stale-sourced availability said otherwise. No lifecycle maps to 0, so a
# lookup miss is unambiguous. ``-1`` is deliberately absent (see above).
_ENDPOINT_STATUS_BY_OICM_STATUS: Final[Mapping[str, EndpointStatus]] = {
    "Failed": _ENDPOINT_STATUS_FAILED,
    "Stopped": _ENDPOINT_STATUS_STOPPED,
    "Undeploying": _ENDPOINT_STATUS_STOPPED,
    "Deploying": _ENDPOINT_STATUS_DEPLOYING,
    "Pending": _ENDPOINT_STATUS_DEPLOYING,
}
_ENDPOINT_STATUS_BY_AVAILABILITY: Final[Mapping[Availability, EndpointStatus]] = {
    "degraded": _ENDPOINT_STATUS_ATTENTION,
    "online": _ENDPOINT_STATUS_OK,
}


class ReplicaCounts(BaseModel):
    model_config = ConfigDict(frozen=True)

    desired: int | None = None
    available: int | None = None


class GatewayStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    # ``oicm_status`` is the controller's own string, passed through verbatim so
    # a consumer sees exactly what OICM reported. ``availability`` is the one
    # derived verdict, because two things cannot be expressed in OICM's
    # vocabulary: a source that stopped reporting (``stale``), and a deployment
    # OICM still calls Ready whose pods serve nothing.
    oicm_status: str | None = None
    availability: Availability
    stale: bool
    source: str | None = None
    healthy: bool | None = None
    replicas: ReplicaCounts
    observed_at: str | None = None
    checked_at: str | None = None

    def endpoint_status(self) -> EndpointStatus | None:
        """This deployment's OpenRouter numeric status, or None if unassigned.

        ``None`` omits the field rather than emitting a guess, which is what
        happens for an unmanaged deployment and for a stale observation.
        """
        if self.stale:
            return None
        by_availability = _ENDPOINT_STATUS_BY_AVAILABILITY.get(self.availability)
        if by_availability is not None:
            return by_availability
        oicm_status = self.oicm_status
        if oicm_status is None:
            return None
        return _ENDPOINT_STATUS_BY_OICM_STATUS.get(oicm_status, _ENDPOINT_STATUS_ATTENTION)


@dataclass(frozen=True, slots=True)
class GatewayStatusInputs:
    """The raw facts one deployment's status is derived from."""

    oicm_status: str | None
    serving_available: bool | None
    replicas_desired: int | None
    replicas_available: int | None
    observed_at: str | None
    cluster: str | None
    health_status: str | None
    source_checked_at: datetime | None


class GatewayStateResolver:
    """Map OICM lifecycle facts onto a gateway availability verdict."""

    def __init__(
        self,
        *,
        stale_after_seconds: int = STATUS_STALE_AFTER_SECONDS,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._stale_after_seconds = stale_after_seconds
        self._now = now or (lambda: datetime.now(timezone.utc))

    def resolve(self, inputs: GatewayStatusInputs) -> GatewayStatus:
        checked_at = inputs.source_checked_at
        stale = self._is_stale(checked_at)
        availability = "unknown" if stale else self._availability(inputs.oicm_status, inputs.serving_available)
        return GatewayStatus(
            oicm_status=inputs.oicm_status,
            availability=availability,
            stale=stale,
            source=inputs.cluster,
            healthy=None if inputs.health_status is None else inputs.health_status == _HEALTHY,
            replicas=ReplicaCounts(desired=inputs.replicas_desired, available=inputs.replicas_available),
            observed_at=inputs.observed_at,
            checked_at=checked_at.isoformat() if checked_at else None,
        )

    def _is_stale(self, checked_at: datetime | None) -> bool:
        if checked_at is None:
            return True
        age = (self._now() - self._as_utc(checked_at)).total_seconds()
        return age > self._stale_after_seconds

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value

    @staticmethod
    def _availability(status: str | None, serving_available: bool | None) -> Availability:
        if status in _STOPPED_STATUSES or status == "Failed" or status in _DEPLOYING_STATUSES:
            return "offline"
        if status in _SERVING_STATUSES:
            return _AVAILABILITY_BY_SERVING[serving_available]
        return "unknown"
