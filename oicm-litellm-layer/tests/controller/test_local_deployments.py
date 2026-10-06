"""Tests for LocalDeploymentSource multi-model fan-out."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from controller.sources.base import probe_v1_models
from controller.sources.local_deployments import LocalDeploymentSource


def _make_deployment(uuid, ready=1, replicas=1):
    dep = MagicMock()
    dep.metadata.labels = {"oip/workload-id": uuid}
    dep.status.ready_replicas = ready
    dep.status.replicas = replicas
    return dep


@pytest.mark.asyncio
async def test_single_model_deployment_is_keyed_by_uuid():
    source = LocalDeploymentSource.__new__(LocalDeploymentSource)
    source._get_configmap_field = AsyncMock(return_value=None)
    source._probe_openapi_paths = AsyncMock(return_value=frozenset({"/v1/chat/completions"}))
    source._discover_model_ids = AsyncMock(return_value=(["llama-3-8b"], "openai"))

    dep = _make_deployment("uuid-1")
    models = await source.discover_for_deployment(dep)

    assert list(models.keys()) == ["uuid-1"]
    model = models["uuid-1"]
    assert model.model_id == "llama-3-8b"
    assert model.mode == "chat"
    assert model.provider == "hosted_vllm"


@pytest.mark.asyncio
async def test_deployment_advertising_multiple_ids_registers_only_the_first():
    """One deployment is one model, so a multi-id advertisement is truncated.

    Registering the extra ids would create rows the reconciler cannot match to a
    deployment (they share one uuid), so the first id wins and the rest are
    logged. A deployment that genuinely serves several models is out of scope.
    """
    source = LocalDeploymentSource.__new__(LocalDeploymentSource)
    source._get_configmap_field = AsyncMock(return_value=None)
    source._probe_openapi_paths = AsyncMock(
        return_value=frozenset({"/v1/convert/source", "/health"})
    )
    source._discover_model_ids = AsyncMock(
        return_value=(["PP-DocLayoutV3", "PP-StructureV3"], "")
    )

    dep = _make_deployment("doc-uuid")
    models = await source.discover_for_deployment(dep)

    assert set(models.keys()) == {"doc-uuid"}
    model = models["doc-uuid"]
    assert model.model_id == "PP-DocLayoutV3"
    # /v1/convert/* no longer implies a docling provider; falls back to
    # the default hosted_vllm classification.
    assert model.provider == "hosted_vllm"
    assert model.api_base == "http://s-doc-uuid.adeo.svc.cluster.local:8080/v1"


@pytest.mark.asyncio
async def test_ocr_path_deployment_registers_as_paddlex_ocr():
    source = LocalDeploymentSource.__new__(LocalDeploymentSource)
    source._get_configmap_field = AsyncMock(return_value=None)
    source._probe_openapi_paths = AsyncMock(
        return_value=frozenset({"/v1/ocr", "/health"})
    )
    source._discover_model_ids = AsyncMock(
        return_value=(["PP-DocLayoutV3"], "")
    )

    dep = _make_deployment("ocr-uuid")
    models = await source.discover_for_deployment(dep)

    assert set(models.keys()) == {"ocr-uuid"}
    model = models["ocr-uuid"]
    # A /v1/ocr-only model registers as a paddlex OCR model.
    assert model.mode == "ocr"
    assert model.provider == "paddlex"
    assert model.api_base == "http://s-ocr-uuid.adeo.svc.cluster.local:8080/v1"


@pytest.mark.asyncio
async def test_configmap_model_id_wins_and_stays_single():
    source = LocalDeploymentSource.__new__(LocalDeploymentSource)
    source._get_configmap_field = AsyncMock(return_value=None)
    source._probe_openapi_paths = AsyncMock(return_value=frozenset({"/v1/convert/source"}))
    # _discover_model_ids returns a single id when the ConfigMap MODEL_ID wins
    source._discover_model_ids = AsyncMock(return_value=(["PP-DocLayoutV3"], ""))

    dep = _make_deployment("doc-uuid")
    models = await source.discover_for_deployment(dep)

    # A single resolved model id yields a single uuid-keyed record
    assert list(models.keys()) == ["doc-uuid"]
    assert models["doc-uuid"].model_id == "PP-DocLayoutV3"


@pytest.mark.asyncio
async def test_discover_model_ids_configmap_takes_precedence():
    source = LocalDeploymentSource.__new__(LocalDeploymentSource)
    source._get_configmap_field = AsyncMock(return_value="PP-DocLayoutV3")
    source._query_v1_models = AsyncMock(return_value=(["PP-DocLayoutV3", "PP-StructureV3"], ""))

    model_ids, _ = await source._discover_model_ids("doc-uuid")

    # ConfigMap MODEL_ID overrides the /v1/models fan-out (backward compat)
    assert model_ids == ["PP-DocLayoutV3"]
    source._query_v1_models.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_model_id_falls_back_to_uuid():
    source = LocalDeploymentSource.__new__(LocalDeploymentSource)
    source._get_configmap_field = AsyncMock(return_value=None)
    source._probe_openapi_paths = AsyncMock(return_value=frozenset())
    source._discover_model_ids = AsyncMock(return_value=([], None))

    dep = _make_deployment("uuid-fallback")
    models = await source.discover_for_deployment(dep)

    assert list(models.keys()) == ["uuid-fallback"]
    assert models["uuid-fallback"].model_id == "uuid-fallback"

class TestProbeV1Models:
    """The shared /v1/models probe, used by both discovery sources."""

    @pytest.mark.asyncio
    async def test_openai_shape_returns_the_ids(self):
        class _Client:
            async def get(self, url):
                return httpx.Response(
                    200,
                    json={"object": "list", "data": [{"id": "org/model"}]},
                    request=httpx.Request("GET", url),
                )

        assert await probe_v1_models(_Client(), "http://x/v1/models") == ["org/model"]

    @pytest.mark.asyncio
    async def test_405_means_no_openai_surface_not_a_failure(self):
        """A native-surface pod answers 405, which is a normal result."""

        class _Client:
            async def get(self, url):
                return httpx.Response(
                    405, request=httpx.Request("GET", url)
                )

        assert await probe_v1_models(_Client(), "http://x/v1/models") == []

    @pytest.mark.asyncio
    async def test_server_error_raises_so_the_caller_decides(self):
        """A 500 is a real failure; each source handles it its own way."""

        class _Client:
            async def get(self, url):
                return httpx.Response(
                    500, request=httpx.Request("GET", url)
                )

        with pytest.raises(httpx.HTTPStatusError):
            await probe_v1_models(_Client(), "http://x/v1/models")
