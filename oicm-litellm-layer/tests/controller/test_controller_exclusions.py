"""Tests for exclusion handling in the controller's two registration paths.

Exclusion is stronger than the `blocked` routing flag: an excluded deployment
must never be registered, and one that is already registered must be deleted, so
it is absent from /v1/models rather than merely unroutable. There are two ways a
model reaches the gateway (the k8s watch and the full sync), so both are pinned
here, along with the OICM placeholder that the full sync would otherwise keep.
"""

from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from controller.controller import DiscoveryController
from controller.models import OicmModel
from controller.status.snapshot import DeploymentStatus, OicmStatusSnapshot

EXCLUDED_ID = "Excluded-Model"
EXCLUDED_UUID = "uuid-excluded"


class _FakeSource:
    def __init__(self, models):
        self._models = models

    async def discover(self):
        return self._models

    async def aclose(self):
        return


def _model(uuid=EXCLUDED_UUID, model_id=EXCLUDED_ID, serving=True, ready=1):
    return OicmModel(
        uuid=uuid,
        model_id=model_id,
        model_name=model_id.replace("/", "--"),
        namespace="adeo",
        ready_replicas=ready,
        total_replicas=1,
        serving=serving,
    )


def _snapshot(workload_id, serving_available=True):
    return OicmStatusSnapshot(
        workspace_id="ws",
        workload_id=workload_id,
        source_name="alain",
        cluster="alain",
        source_status=DeploymentStatus.READY,
        desired_replicas=1,
        available_replicas=1,
        unavailable_replicas=0,
        serving_available=serving_available,
        error_msg=None,
        status_changed_at="2026-10-08T00:00:00+00:00",
        observed_at="2026-10-08T00:00:00+00:00",
    )


def _controller(models, *, exclusions, snapshots=None, registered=None):
    """A controller with discovery, pricing, and the gateway stubbed out.

    Dependencies are injected rather than monkeypatched, so the paths under test
    run their real logic.
    """
    source = _FakeSource(models)
    litellm = MagicMock()
    litellm.list_all_models_by_key = AsyncMock(return_value=registered or {})
    # `execute` zips register results back to the inputs by position, so the
    # batch result must carry one id per register or the strict zip raises.
    litellm.batch = AsyncMock(
        side_effect=lambda deletes, registers, patches: (
            0,
            [f"litellm-{i}" for i in range(len(registers))],
            0,
        )
    )
    litellm.register_model = AsyncMock(return_value="litellm-id")
    litellm.set_blocked = AsyncMock(return_value=True)

    poller = MagicMock()
    poller.snapshots = MappingProxyType(snapshots or {})
    poller.all_sources_ok = True

    controller = DiscoveryController(
        sources=[source], litellm=litellm, status_poller=poller, exclusions=exclusions
    )
    controller.pricing_resolver.resolve = AsyncMock(return_value=None)
    controller.fallback_reconciler.reconcile = AsyncMock()
    return controller


def _registered_entry(model_id, uuid):
    return {
        "model_id": model_id,
        "model_name": f"hosted_vllm/{EXCLUDED_ID}",
        "litellm_params": {"model": f"hosted_vllm/{EXCLUDED_ID}", "api_base": "http://x:8080/v1"},
        "model_info": {"id": model_id, "oicm_uuid": uuid},
    }


@pytest.mark.asyncio
async def test_full_sync_does_not_register_an_excluded_model():
    controller = _controller(
        {EXCLUDED_UUID: _model()}, exclusions=frozenset({EXCLUDED_ID})
    )

    await controller.full_sync()

    _, registers, _ = controller.litellm.batch.await_args.args
    assert registers == []
    assert EXCLUDED_UUID not in controller._state


@pytest.mark.asyncio
async def test_full_sync_registers_a_non_excluded_model():
    """The filter must not be a blanket drop: everything else still registers."""
    controller = _controller(
        {"keep-uuid": _model(uuid="keep-uuid", model_id="Keep-Model")},
        exclusions=frozenset({EXCLUDED_ID}),
    )

    await controller.full_sync()

    _, registers, _ = controller.litellm.batch.await_args.args
    assert [m.model_id for m, _ in registers] == ["Keep-Model"]


@pytest.mark.asyncio
async def test_full_sync_deletes_an_already_registered_excluded_model():
    """Exclusion must remove the row, not just stop re-registering it."""
    controller = _controller(
        {EXCLUDED_UUID: _model()},
        exclusions=frozenset({EXCLUDED_ID}),
        registered={EXCLUDED_UUID: [_registered_entry("litellm-id-1", EXCLUDED_UUID)]},
    )

    await controller.full_sync()

    deletes, registers, _ = controller.litellm.batch.await_args.args
    assert deletes == ["litellm-id-1"]
    assert registers == []


@pytest.mark.asyncio
async def test_full_sync_drops_the_oicm_placeholder_for_an_excluded_deployment():
    """A running deployment excluded by model id must not survive as a placeholder.

    The OICM half of `desired` carries only the uuid as a model id, so it has to
    be dropped by the k8s-derived key as well. Without that, the row survives as
    a non-serving placeholder instead of being deleted.
    """
    controller = _controller(
        {EXCLUDED_UUID: _model()},
        exclusions=frozenset({EXCLUDED_ID}),
        snapshots={EXCLUDED_UUID: _snapshot(EXCLUDED_UUID)},
        registered={EXCLUDED_UUID: [_registered_entry("litellm-id-1", EXCLUDED_UUID)]},
    )

    await controller.full_sync()

    deletes, registers, _ = controller.litellm.batch.await_args.args
    assert deletes == ["litellm-id-1"]
    assert registers == []
    _, _, blocks = controller.litellm.batch.await_args.args
    assert blocks == []


@pytest.mark.asyncio
async def test_watch_add_does_not_register_an_excluded_model():
    """The watch path registers directly, so it needs the same filter.

    Without it, a watch ADDED event re-registers a model that the full sync
    correctly removed.
    """
    model = _model()
    controller = _controller([model], exclusions=frozenset({EXCLUDED_ID}))
    controller._running = True
    controller.local_source.discover_for_deployment = AsyncMock(
        return_value={model.deployment_id: model}
    )

    await controller._handle_add(
        EXCLUDED_UUID, SimpleNamespace(status=SimpleNamespace(ready_replicas=1, replicas=1))
    )

    controller.litellm.register_model.assert_not_awaited()
    assert EXCLUDED_UUID not in controller._state


@pytest.mark.asyncio
async def test_watch_add_registers_a_non_excluded_model():
    model = _model(uuid="keep-uuid", model_id="Keep-Model")
    controller = _controller([model], exclusions=frozenset({EXCLUDED_ID}))
    controller._running = True
    controller.local_source.discover_for_deployment = AsyncMock(
        return_value={model.deployment_id: model}
    )

    await controller._handle_add(
        "keep-uuid", SimpleNamespace(status=SimpleNamespace(ready_replicas=1, replicas=1))
    )

    controller.litellm.register_model.assert_awaited_once()
    assert controller._state["keep-uuid"].model_id == "Keep-Model"


@pytest.mark.asyncio
async def test_no_exclusions_registers_everything():
    controller = _controller(
        {EXCLUDED_UUID: _model()}, exclusions=frozenset()
    )

    await controller.full_sync()

    _, registers, _ = controller.litellm.batch.await_args.args
    assert [m.model_id for m, _ in registers] == [EXCLUDED_ID]
