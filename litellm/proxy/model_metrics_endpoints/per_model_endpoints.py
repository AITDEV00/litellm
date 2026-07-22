"""
Per-model real-time and historical metrics endpoint (Tier 2).

Queries Prometheus for per-deployment time-series: concurrent requests,
request rate, output tokens/sec, and latency per token p50.

Falls back to an instant in-progress gauge value when Prometheus is not
connected.
"""

from typing import Optional

import fastapi
from fastapi import APIRouter, Depends

from litellm.integrations.prometheus_helpers.prometheus_api import (
    get_in_progress_requests_instant,
    is_prometheus_connected,
)
from litellm.integrations.prometheus_helpers.prometheus_api import (
    get_per_model_metrics as fetch_per_model_metrics,
)
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth

router = APIRouter()

_VALID_WINDOWS = ("1m", "15m", "1h", "24h", "7d")


@router.get(
    "/model/metrics/per_model",
    tags=["model management"],
    include_in_schema=False,
    dependencies=[Depends(user_api_key_auth)],
)
async def get_per_model_metrics(
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
    window: str = fastapi.Query(
        default="1h",
        description="Time window: 1m, 15m, 1h, 24h, 7d",
    ),
    model_id: Optional[str] = fastapi.Query(
        default=None,
        description="Filter to a specific deployment by model_id",
    ),
):
    if window not in _VALID_WINDOWS:
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid window '{window}'. Must be one of: {', '.join(_VALID_WINDOWS)}",
        )

    if is_prometheus_connected():
        return await fetch_per_model_metrics(window=window, model_id=model_id)

    instant = await get_in_progress_requests_instant()
    deployments = [
        {
            "model_id": d["model_id"],
            "litellm_model_name": d["litellm_model_name"],
            "api_base": d["api_base"],
            "api_provider": d["api_provider"],
            "rpm_limit": 0,
            "concurrent_requests": [{"timestamp": "", "value": d["value"]}],
            "request_rate": [],
            "output_tokens_per_sec": [],
            "latency_per_token_p50": [],
        }
        for d in instant
    ]
    return {
        "prometheus_connected": False,
        "window": window,
        "step": "",
        "deployments": deployments,
    }
