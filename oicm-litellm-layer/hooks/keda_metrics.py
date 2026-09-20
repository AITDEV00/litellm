"""
KEDA Metrics Callback — emits ml_model_concurrent_requests Prometheus gauge.

This is Component #4 of the OICM→LiteLLM integration layer.
It plugs into LiteLLM via `litellm_settings.callbacks` in config.yaml.

How it works:
1. On each incoming request (async_log_pre_api_call): increment gauge
2. On each completed request (async_log_success_event): decrement gauge
3. On each failed request (async_log_failure_event): decrement gauge
4. The gauge is exposed on LiteLLM's /metrics endpoint alongside other Prometheus metrics

KEDA integration:
  The existing KEDA ScaledObject for 0f8674c2 (MinerU) uses:
    sum(ml_model_concurrent_requests{model_id="<uuid>"})
  
  This callback emits the exact same metric name and label, so the existing
  KEDA ScaledObject continues to work without modification.

Model ID resolution:
  LiteLLM uses model_name (e.g., "Qwen/Qwen3-Next-80B-A3B-Instruct") internally,
  but KEDA uses the OICM UUID (e.g., "0f8674c2-5fb8-43c7-bf2d-a6e5db2c0ff4").
  We resolve model_name → oicm_uuid from the model_info metadata stored by
  the discovery controller.
"""

import logging
import os
from typing import Optional

from litellm.integrations.custom_logger import CustomLogger
from litellm.caching import DualCache
from litellm.proxy._types import UserAPIKeyAuth

logger = logging.getLogger("oicm-keda")

# ─── Prometheus gauge ───────────────────────────────────────────────────────

try:
    from prometheus_client import Gauge
    
    CONCURRENT_REQUESTS = Gauge(
        "ml_model_concurrent_requests",
        "Number of concurrent requests currently being processed",
        ["model_id"],
    )
    PROMETHEUS_AVAILABLE = True
except ImportError:
    logger.warning("prometheus_client not installed, KEDA metrics disabled")
    PROMETHEUS_AVAILABLE = False


# ─── Model name → OICM UUID resolution ─────────────────────────────────────

# Cache: model_name → oicm_uuid
# Populated from model_info metadata (set by discovery controller)
_uuid_cache: dict = {}


def _resolve_uuid(model_name: str, kwargs: dict) -> Optional[str]:
    """
    Resolve a LiteLLM model_name to an OICM UUID.
    
    Strategy:
    1. Check the kwargs metadata (litellm_params.model_info.oicm_uuid)
    2. Check the in-memory cache
    3. Return None if not found (gauge won't be emitted)
    """
    # Try from kwargs metadata
    metadata = kwargs.get("metadata", {})
    model_info = metadata.get("model_info", {})
    oicm_uuid = model_info.get("oicm_uuid")
    if oicm_uuid:
        _uuid_cache[model_name] = oicm_uuid
        return oicm_uuid
    
    # Try cache
    if model_name in _uuid_cache:
        return _uuid_cache[model_name]
    
    return None


def _get_model_id(kwargs: dict) -> Optional[str]:
    """Get the model identifier for KEDA labeling."""
    # The model field in kwargs could be the litellm model name or the model_name
    model = kwargs.get("model", "")
    
    # Strip the "hosted_vllm/" prefix if present
    if model.startswith("hosted_vllm/"):
        model = model[len("hosted_vllm/"):]
    
    # Try to resolve to OICM UUID
    uuid = _resolve_uuid(model, kwargs)
    if uuid:
        return uuid
    
    # Fallback: use the model name as-is
    return model


# ─── Callback ───────────────────────────────────────────────────────────────

class KEDAMetricsCallback(CustomLogger):
    """
    Custom callback that emits ml_model_concurrent_requests for KEDA.
    
    Register in config.yaml:
    
        litellm_settings:
          callbacks:
            - hooks.keda_metrics.KEDAMetricsCallback
    """

    async def async_log_pre_api_call(self, model, messages, kwargs):
        """Increment concurrent requests gauge before the API call."""
        if not PROMETHEUS_AVAILABLE:
            return
        
        model_id = _get_model_id(kwargs)
        if model_id:
            try:
                CONCURRENT_REQUESTS.labels(model_id=model_id).inc()
                logger.debug(f"KEDA inc: model_id={model_id}")
            except Exception as e:
                logger.error(f"KEDA inc error for {model_id}: {e}")

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        """Decrement concurrent requests gauge on success."""
        if not PROMETHEUS_AVAILABLE:
            return
        
        model_id = _get_model_id(kwargs)
        if model_id:
            try:
                CONCURRENT_REQUESTS.labels(model_id=model_id).dec()
                logger.debug(f"KEDA dec (success): model_id={model_id}")
            except Exception as e:
                logger.error(f"KEDA dec error for {model_id}: {e}")

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        """Decrement concurrent requests gauge on failure."""
        if not PROMETHEUS_AVAILABLE:
            return
        
        model_id = _get_model_id(kwargs)
        if model_id:
            try:
                CONCURRENT_REQUESTS.labels(model_id=model_id).dec()
                logger.debug(f"KEDA dec (failure): model_id={model_id}")
            except Exception as e:
                logger.error(f"KEDA dec error for {model_id}: {e}")

    async def async_log_stream_event(self, kwargs):
        """No-op for streaming events — we track at request level, not chunk level."""
        pass
