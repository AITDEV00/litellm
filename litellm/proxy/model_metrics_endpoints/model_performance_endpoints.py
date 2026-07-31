"""
Per-model-group performance endpoint (Tier 2).

Returns throughput, concurrent requests, and TTFT per model_group over
selectable time windows. DB-backed for 1h/24h/7d with sub-daily time
bucketing; Prometheus-backed for 5m/15m (real-time concurrent requests).
"""

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import fastapi
from fastapi import APIRouter, Depends, HTTPException, status

from litellm._logging import verbose_proxy_logger
from litellm.caching.in_memory_cache import InMemoryCache
from litellm.proxy._types import CommonProxyErrors, ProxyException, UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth

router = APIRouter()

_VALID_WINDOWS = ("5m", "15m", "1h", "24h", "7d")

_DB_CACHE: InMemoryCache = InMemoryCache(max_size_in_memory=64, default_ttl=300)

_DB_CACHE_TTL: dict[str, int] = {
    "1h": 0,
    "24h": 120,
    "7d": 300,
}

_WINDOW_DELTAS: dict[str, timedelta] = {
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
}

_WINDOW_BUCKET_INTERVALS: dict[str, str] = {
    "1h": "5 minutes",
    "24h": "1 hour",
    "7d": "6 hours",
}

_PROMETHEUS_WINDOWS = ("5m", "15m", "1h")


@router.get(
    "/model/performance",
    tags=["model management"],
    include_in_schema=False,
    dependencies=[Depends(user_api_key_auth)],
)
async def model_performance(
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
    window: str = fastapi.Query(
        default="1h",
        description="Time window: 5m, 15m, 1h, 24h, 7d",
    ),
    model_group: Optional[str] = fastapi.Query(
        default=None,
        description="Filter to a specific model_group",
    ),
):
    if window not in _VALID_WINDOWS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid window '{window}'. Must be one of: {', '.join(_VALID_WINDOWS)}",
        )

    if window in _PROMETHEUS_WINDOWS:
        prom_result = await _fetch_prometheus_performance(window=window, model_group=model_group)
        if prom_result is not None:
            return prom_result

    return await _fetch_db_performance(window=window, model_group=model_group)


async def _fetch_prometheus_performance(
    window: str,
    model_group: Optional[str],
) -> Optional[dict]:
    try:
        from litellm.integrations.prometheus_helpers.prometheus_api import (
            is_prometheus_connected,
            query_prometheus_range,
        )
    except ImportError:
        return None

    if not is_prometheus_connected():
        return None

    from litellm.integrations.prometheus_helpers.prometheus_api import (
        _WINDOW_CONFIG,
        _parse_range_result,
        _parse_window_to_timedelta,
        _quote_promql_string_literal,
    )

    range_str, step = _WINDOW_CONFIG.get(window, _WINDOW_CONFIG["1h"])
    end = datetime.now(timezone.utc)
    start = end - _parse_window_to_timedelta(range_str)

    label_filter = ""
    if model_group:
        quoted = _quote_promql_string_literal(model_group)
        label_filter = f"{{requested_model={quoted}}}"

    queries = {
        "concurrent_requests": (
            f"max by (requested_model) (max_over_time("
            f"litellm_deployment_in_progress_requests{label_filter}[{range_str}]))"
        ),
        "throughput_tokens_per_sec": (
            f"sum by (requested_model) (rate(litellm_output_tokens_metric_total{label_filter}[{range_str}]))"
        ),
        "ttft_seconds": (
            f"histogram_quantile(0.50, sum by (le, requested_model) (rate("
            f"litellm_deployment_latency_per_output_token_bucket{label_filter}[{range_str}])))"
        ),
    }

    models: dict[str, dict[str, Any]] = {}

    for series_name, promql in queries.items():
        try:
            raw = await query_prometheus_range(promql, start, end, step)
        except (ValueError, OSError, RuntimeError) as e:
            verbose_proxy_logger.debug(f"Prometheus performance query '{series_name}' failed: {e}")
            continue
        for entry in raw:
            labels = entry.get("metric", {})
            mg = labels.get("requested_model", "") or labels.get("model_id", "")
            if not mg:
                continue
            if mg not in models:
                models[mg] = _empty_model_dict(mg)
            models[mg]["time_series"][series_name] = _parse_range_result([entry])

    _compute_summaries(models)

    return {
        "window": window,
        "source": "prometheus",
        "step": step,
        "models": list(models.values()),
    }


async def _fetch_db_performance(
    window: str,
    model_group: Optional[str],
) -> dict:
    from litellm.proxy.proxy_server import prisma_client

    if prisma_client is None:
        raise ProxyException(
            message=CommonProxyErrors.db_not_connected_error.value,
            type="internal_error",
            param="None",
            code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    cache_ttl = _DB_CACHE_TTL.get(window, 0)
    cache_key = f"{window}:{model_group or ''}"
    if cache_ttl > 0:
        cached = _DB_CACHE.get_cache(cache_key)
        if cached is not None:
            return cached

    delta = _WINDOW_DELTAS[window]
    end_time = datetime.now(timezone.utc)
    start_time = end_time - delta
    bucket_interval = _WINDOW_BUCKET_INTERVALS.get(window, "1 hour")

    sql_query = """
        WITH bucketed AS (
            SELECT
                COALESCE(model_group, '') AS model_group,
                date_bin($3::interval, "startTime", timestamp '2000-01-01') AS bucket,
                COUNT(*) AS request_count,
                SUM(completion_tokens) AS total_completion_tokens,
                AVG(
                    CASE
                        WHEN request_duration_ms IS NOT NULL AND request_duration_ms > 0
                        THEN completion_tokens::float8 / request_duration_ms * 1000.0
                        ELSE NULL
                    END
                ) AS avg_throughput_tokens_per_sec,
                AVG(
                    CASE
                        WHEN "completionStartTime" IS NOT NULL
                             AND "completionStartTime" != "endTime"
                        THEN EXTRACT(epoch FROM ("completionStartTime" - "startTime"))
                        ELSE NULL
                    END
                ) AS avg_ttft_seconds,
                PERCENTILE_CONT(0.50) WITHIN GROUP (
                    ORDER BY
                        CASE
                            WHEN "completionStartTime" IS NOT NULL
                                 AND "completionStartTime" != "endTime"
                            THEN EXTRACT(epoch FROM ("completionStartTime" - "startTime"))
                            ELSE NULL
                        END
                ) AS p50_ttft_seconds,
                PERCENTILE_CONT(0.95) WITHIN GROUP (
                    ORDER BY
                        CASE
                            WHEN "completionStartTime" IS NOT NULL
                                 AND "completionStartTime" != "endTime"
                            THEN EXTRACT(epoch FROM ("completionStartTime" - "startTime"))
                            ELSE NULL
                        END
                ) AS p95_ttft_seconds
            FROM "LiteLLM_SpendLogs"
            WHERE
                "startTime" >= $1::timestamptz
                AND "startTime" < $2::timestamptz
                AND "cache_hit" != 'True'
                AND ($4::text IS NULL
                     OR COALESCE(model_group, '') = $4::text)
            GROUP BY
                COALESCE(model_group, ''),
                bucket
        )
        SELECT
            model_group,
            bucket,
            request_count,
            total_completion_tokens,
            avg_throughput_tokens_per_sec,
            avg_ttft_seconds,
            p50_ttft_seconds,
            p95_ttft_seconds
        FROM bucketed
        ORDER BY model_group, bucket
    """

    rows = await prisma_client.db.query_raw(
        sql_query,
        start_time,
        end_time,
        bucket_interval,
        model_group,
    )

    models: dict[str, dict[str, Any]] = {}

    if rows:
        for row in rows:
            mg = row.get("model_group", "") or ""
            if not mg:
                continue
            if mg not in models:
                models[mg] = _empty_model_dict(mg)

            ts_bucket = _format_bucket(row.get("bucket"))

            models[mg]["time_series"]["throughput_tokens_per_sec"].append(
                {"timestamp": ts_bucket, "value": _safe_float(row.get("avg_throughput_tokens_per_sec"))}
            )
            models[mg]["time_series"]["ttft_seconds"].append(
                {"timestamp": ts_bucket, "value": _safe_float(row.get("avg_ttft_seconds"))}
            )
            models[mg]["time_series"]["concurrent_requests"].append({"timestamp": ts_bucket, "value": 0.0})

            summary = models[mg]["summary"]
            summary["total_requests"] += int(row.get("request_count", 0))
            summary["total_tokens"] += int(row.get("total_completion_tokens", 0))

        for mg_data in models.values():
            _finalize_summary(mg_data)

    source = "db" if window not in _PROMETHEUS_WINDOWS else "db"

    result = {
        "window": window,
        "source": source,
        "step": bucket_interval,
        "models": list(models.values()),
    }

    if cache_ttl > 0:
        _DB_CACHE.set_cache(cache_key, result, ttl=cache_ttl)

    return result


def _empty_model_dict(model_group: str) -> dict:
    return {
        "model_group": model_group,
        "time_series": {
            "concurrent_requests": [],
            "throughput_tokens_per_sec": [],
            "ttft_seconds": [],
        },
        "summary": {
            "avg_concurrent": 0.0,
            "avg_throughput": 0.0,
            "p50_ttft": None,
            "p95_ttft": None,
            "total_requests": 0,
            "total_tokens": 0,
        },
    }


def _compute_summaries(models: dict[str, dict[str, Any]]) -> None:
    for mg_data in models.values():
        ts = mg_data["time_series"]
        mg_data["summary"]["avg_concurrent"] = _avg_values(ts.get("concurrent_requests", []))
        mg_data["summary"]["avg_throughput"] = _avg_values(ts.get("throughput_tokens_per_sec", []))
        ttft_vals = [p["value"] for p in ts.get("ttft_seconds", []) if p["value"] is not None]
        if ttft_vals:
            ttft_vals.sort()
            mg_data["summary"]["p50_ttft"] = ttft_vals[len(ttft_vals) // 2]
            mg_data["summary"]["p95_ttft"] = ttft_vals[
                min(len(ttft_vals) - 1, int(math.ceil(len(ttft_vals) * 0.95)) - 1)
            ]
        mg_data["summary"]["total_requests"] = 0
        mg_data["summary"]["total_tokens"] = 0


def _finalize_summary(mg_data: dict[str, Any]) -> None:
    ts = mg_data["time_series"]
    mg_data["summary"]["avg_throughput"] = _avg_values(ts.get("throughput_tokens_per_sec", []))
    ttft_vals = [p["value"] for p in ts.get("ttft_seconds", []) if p["value"] is not None]
    if ttft_vals:
        ttft_vals.sort()
        mg_data["summary"]["p50_ttft"] = ttft_vals[len(ttft_vals) // 2]
        mg_data["summary"]["p95_ttft"] = ttft_vals[min(len(ttft_vals) - 1, int(math.ceil(len(ttft_vals) * 0.95)) - 1)]
    else:
        mg_data["summary"]["p50_ttft"] = None
        mg_data["summary"]["p95_ttft"] = None
    mg_data["summary"]["avg_concurrent"] = 0.0


def _avg_values(points: list[dict]) -> float:
    vals = [p["value"] for p in points if p["value"] is not None]
    if not vals:
        return 0.0
    return sum(vals) / len(vals)


def _safe_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        f = float(val)
        if math.isnan(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _format_bucket(bucket: Any) -> str:
    if bucket is None:
        return ""
    if isinstance(bucket, datetime):
        return bucket.isoformat() + "+00:00"
    return str(bucket)
