"""Tests for the watch handlers' handling of a deployment that stops serving.

A deployment OICM reports as Stopped has no k8s Deployment and no Service, so it
cannot be re-probed and its ClusterIP does not resolve. It must still be
registered and visible, and it must leave the routing pool, because leaving it
routable sends every request at a Service that no longer exists.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from controller.controller import DiscoveryController
from controller.models import OicmModel


def _model(uuid="uuid-1", serving=True, ready=1):
    return OicmModel(
        uuid=uuid,
        model_id="test-model",
        model_name="hosted_vllm/test-model",
        namespace="adeo",
        ready_replicas=ready,
        total_replicas=1,
        serving=serving,
    )


def _deployment(ready_replicas, replicas=1):
    return SimpleNamespace(
        status=SimpleNamespace(ready_replicas=ready_replicas, replicas=replicas)
    )


def _controller(models, serving=True):
    """A controller with discovery and the gateway stubbed out.

    Dependencies are injected rather than monkeypatched, so the handlers are
    exercised through their real logic.
    """
    controller = DiscoveryController(
        sources=[MagicMock()], litellm=MagicMock(), status_poller=MagicMock()
    )
    controller._running = True
    controller.local_source.discover_for_deployment = AsyncMock(
        return_value={m.composite_key: m for m in models}
    )
    controller.pricing_resolver.resolve = AsyncMock(return_value=None)
    controller.litellm.register_model = AsyncMock(return_value="litellm-id")
    controller.litellm.set_blocked = AsyncMock(return_value=True)
    controller.litellm.deregister_model = AsyncMock(return_value=True)
    controller.fallback_reconciler.reconcile = AsyncMock()
    return controller


@pytest.mark.asyncio
async def test_not_ready_deployment_is_registered_and_blocked():
    """A deployment seen before it is ready must not be dropped.

    It has to be registered so it is visible, then paused, because a model that
    is registered but not serving would otherwise take traffic.
    """
    model = _model(serving=True)
    controller = _controller([model])

    await controller._handle_add("uuid-1", _deployment(ready_replicas=0))

    controller.litellm.register_model.assert_awaited_once()
    registered = controller.litellm.register_model.await_args.args[0]
    assert registered.serving is False
    controller.litellm.set_blocked.assert_awaited_once_with("litellm-id", True)
    assert controller._state[model.composite_key].serving is False


@pytest.mark.asyncio
async def test_ready_deployment_is_registered_unblocked():
    model = _model()
    controller = _controller([model])

    await controller._handle_add("uuid-1", _deployment(ready_replicas=1))

    controller.litellm.register_model.assert_awaited_once()
    registered = controller.litellm.register_model.await_args.args[0]
    assert registered.serving is True
    controller.litellm.set_blocked.assert_not_awaited()


@pytest.mark.asyncio
async def test_deployment_losing_its_pods_is_blocked():
    """Replicas dropping to zero must pause routing, not remove the model."""
    model = _model()
    controller = _controller([])
    controller._state[model.composite_key] = model
    controller._litellm_id_map[model.composite_key] = "litellm-id"

    await controller._handle_modify("uuid-1", _deployment(ready_replicas=0))

    controller.litellm.set_blocked.assert_awaited_once_with("litellm-id", True)
    controller.litellm.deregister_model.assert_not_awaited()
    assert controller._state[model.composite_key].serving is False


@pytest.mark.asyncio
async def test_deployment_regaining_pods_is_unblocked():
    """A deployment that comes back must become routable again."""
    model = _model(serving=False, ready=0)
    controller = _controller([])
    controller._state[model.composite_key] = model
    controller._litellm_id_map[model.composite_key] = "litellm-id"

    await controller._handle_modify("uuid-1", _deployment(ready_replicas=1))

    controller.litellm.set_blocked.assert_awaited_once_with("litellm-id", False)
    assert controller._state[model.composite_key].serving is True


@pytest.mark.asyncio
async def test_unchanged_serving_state_does_not_write():
    """A replica-count change alone must not pause or resume routing."""
    model = _model()
    controller = _controller([])
    controller._state[model.composite_key] = model
    controller._litellm_id_map[model.composite_key] = "litellm-id"

    await controller._handle_modify("uuid-1", _deployment(ready_replicas=3, replicas=3))

    controller.litellm.set_blocked.assert_not_awaited()
    assert controller._state[model.composite_key].ready_replicas == 3


@pytest.mark.asyncio
async def test_delete_still_removes_the_model():
    """An actual k8s deletion is still a removal.

    This is the case that must keep working: the deployment is gone from k8s,
    and the OICM poller is what decides whether it is also gone from OICM.
    """
    model = _model()
    controller = _controller([])
    controller._state[model.composite_key] = model
    controller._litellm_id_map[model.composite_key] = "litellm-id"

    await controller._handle_delete("uuid-1")

    controller.litellm.deregister_model.assert_awaited_once_with("litellm-id")
    assert controller._state == {}
    assert controller._litellm_id_map == {}
