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
        return_value={m.deployment_id: m for m in models}
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
    assert controller._state[model.deployment_id].serving is False


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
async def test_deployment_losing_its_pods_updates_state_without_writing_blocked():
    """Replicas dropping to zero must not remove the model.

    The watch records the transition but does not write `blocked`: OICM
    `serving_available` is the sole owner of that column, and a second writer
    would be a second opinion about the same fact.
    """
    model = _model()
    controller = _controller([])
    controller._state[model.deployment_id] = model
    controller._litellm_id_map[model.deployment_id] = "litellm-id"

    await controller._handle_modify("uuid-1", _deployment(ready_replicas=0))

    controller.litellm.set_blocked.assert_not_awaited()
    controller.litellm.deregister_model.assert_not_awaited()
    assert controller._state[model.deployment_id].serving is False


@pytest.mark.asyncio
async def test_deployment_regaining_pods_updates_state_without_writing_blocked():
    """A deployment that comes back must be recorded as serving again."""
    model = _model(serving=False, ready=0)
    controller = _controller([])
    controller._state[model.deployment_id] = model
    controller._litellm_id_map[model.deployment_id] = "litellm-id"

    await controller._handle_modify("uuid-1", _deployment(ready_replicas=1))

    controller.litellm.set_blocked.assert_not_awaited()
    assert controller._state[model.deployment_id].serving is True


@pytest.mark.asyncio
async def test_unchanged_serving_state_does_not_write():
    """A replica-count change alone must not pause or resume routing."""
    model = _model()
    controller = _controller([])
    controller._state[model.deployment_id] = model
    controller._litellm_id_map[model.deployment_id] = "litellm-id"

    await controller._handle_modify("uuid-1", _deployment(ready_replicas=3, replicas=3))

    controller.litellm.set_blocked.assert_not_awaited()
    assert controller._state[model.deployment_id].ready_replicas == 3


@pytest.mark.asyncio
async def test_delete_keeps_the_row_and_lets_oicm_decide_removal():
    """A k8s deletion must not remove the row; only OICM can say it is deleted.

    A k8s Deployment disappearing is either a stop or a real delete, and only
    OICM can tell them apart. The row is kept so a Stopped deployment stays
    distinguishable from a deleted one, and routing is left to the status poll.
    """
    model = _model()
    controller = _controller([])
    controller._state[model.deployment_id] = model
    controller._litellm_id_map[model.deployment_id] = "litellm-id"

    await controller._handle_delete("uuid-1")

    controller.litellm.deregister_model.assert_not_awaited()
    controller.litellm.set_blocked.assert_not_awaited()
    assert controller._state[model.deployment_id].serving is False
    # The id map must survive, or the next full sync could not resume it.
    assert controller._litellm_id_map[model.deployment_id] == "litellm-id"


@pytest.mark.asyncio
async def test_redeploy_reusing_the_uuid_replaces_the_placeholder():
    """A redeploy under the same uuid must reuse the row, not register twice.

    A Stopped deployment keeps its row. If the k8s object comes back with the
    same uuid, the real record has to replace the placeholder, otherwise the
    model stays stuck on the placeholder's config.
    """
    placeholder = _model(serving=False, ready=0)
    controller = _controller([_model()])
    controller._state[placeholder.deployment_id] = placeholder
    controller._litellm_id_map[placeholder.deployment_id] = "litellm-id"

    await controller._handle_add("uuid-1", _deployment(ready_replicas=1))

    controller.litellm.register_model.assert_not_awaited()
    controller.litellm.set_blocked.assert_not_awaited()
    assert controller._state[placeholder.deployment_id].serving is True
