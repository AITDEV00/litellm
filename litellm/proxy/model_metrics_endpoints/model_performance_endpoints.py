"""
Per-model-group performance endpoint (Tier 2).

Returns throughput, concurrent requests, and TTFT per model_group over
selectable time windows. DB-backed for 1h/24h/7d with sub-daily time
bucketing; Prometheus-backed for 5m/15m (real-time concurrent requests).
"""

import math
from dataclasses import dataclass
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


@dataclass(frozen=True)
class EntityScope:
    """Entity-scope filter applied to the DB-backed performance query.

    Mirrors the entity columns on ``LiteLLM_SpendLogs``. An empty scope (all
    fields ``None``) means "global".
    """

    team_id: Optional[str] = None
    organization_id: Optional[str] = None
    user_id: Optional[str] = None
    end_user_id: Optional[str] = None
    api_key: Optional[str] = None
    agent_id: Optional[str] = None

    @property
    def is_empty(self) -> bool:
        return all(
            v is None
            for v in (self.team_id, self.organization_id, self.user_id, self.end_user_id, self.api_key, self.agent_id)
        )

    def cache_key_suffix(self) -> str:
        parts = (
            f"t:{self.team_id or ''}",
            f"o:{self.organization_id or ''}",
            f"u:{self.user_id or ''}",
            f"e:{self.end_user_id or ''}",
            f"k:{self.api_key or ''}",
            f"a:{self.agent_id or ''}",
        )
        return ":".join(parts)

    def where_clause(self, start_idx: int) -> tuple[str, tuple[str, ...]]:
        """Return a SQL ``AND ...`` fragment and its bound params.

        ``start_idx`` is the 1-based index of the first placeholder for this
        fragment. Returns ``("", ())`` when the scope is empty.
        """
        clauses: list[str] = []
        params: list[str] = []
        idx = start_idx
        for column, value in (
            ("team_id", self.team_id),
            ("organization_id", self.organization_id),
            ("user_id", self.user_id),
            ("end_user", self.end_user_id),
            ("api_key", self.api_key),
            ("agent_id", self.agent_id),
        ):
            if value is None:
                continue
            clauses.append(f"COALESCE(\"{column}\", '') = ${idx}::text")
            params.append(value)
            idx += 1
        if not clauses:
            return "", ()
        return f" AND {' AND '.join(clauses)}", tuple(params)


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
    team_id: Optional[str] = fastapi.Query(
        default=None,
        description="Scope to a specific team",
    ),
    organization_id: Optional[str] = fastapi.Query(
        default=None,
        description="Scope to a specific organization",
    ),
    user_id: Optional[str] = fastapi.Query(
        default=None,
        description="Scope to a specific user",
    ),
    end_user_id: Optional[str] = fastapi.Query(
        default=None,
        description="Scope to a specific end user",
    ),
    api_key: Optional[str] = fastapi.Query(
        default=None,
        description="Scope to a specific virtual key (hashed)",
    ),
    agent_id: Optional[str] = fastapi.Query(
        default=None,
        description="Scope to a specific agent",
    ),
):
    if window not in _VALID_WINDOWS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid window '{window}'. Must be one of: {', '.join(_VALID_WINDOWS)}",
        )

    scope = EntityScope(
        team_id=team_id,
        organization_id=organization_id,
        user_id=user_id,
        end_user_id=end_user_id,
        api_key=api_key,
        agent_id=agent_id,
    )

    # Entity-scoped requests always use the DB path: the Prometheus metrics for
    # concurrent requests and TTFT do not carry the entity-scope labels, so we
    # cannot filter them without leaking cross-entity data. The DB path filters
    # on the SpendLogs entity columns directly.
    if scope.is_empty and window in _PROMETHEUS_WINDOWS:
        prom_result = await _fetch_prometheus_performance(window=window, model_group=model_group)
        if prom_result is not None:
            return prom_result

    return await _fetch_db_performance(window=window, model_group=model_group, scope=scope)


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
        # The deployment-scoped metrics (concurrent gauge, latency histogram)
        # carry litellm_model_name / model_id, not requested_model. Filter on
        # litellm_model_name for the model_group filter so it matches.
        label_filter = f"{{litellm_model_name={quoted}}}"

    # Group by model_id: it is present on all three metrics, whereas
    # requested_model only exists on the token counter. This mirrors the proven
    # query in prometheus_api.get_per_model_metrics.
    queries = {
        "concurrent_requests": (
            f"max by (model_id) (max_over_time(litellm_deployment_in_progress_requests{label_filter}[{range_str}]))"
        ),
        "throughput_tokens_per_sec": (
            f"sum by (model_id) (rate(litellm_output_tokens_metric_total{label_filter}[{range_str}]))"
        ),
        "ttft_seconds": (
            f"histogram_quantile(0.50, sum by (le, model_id) (rate("
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
            mg = labels.get("model_id", "") or labels.get("litellm_model_name", "")
            if not mg:
                continue
            if mg not in models:
                models[mg] = _empty_model_dict(mg)
            models[mg]["time_series"][series_name] = _parse_range_result([entry])

    for mg_data in models.values():
        _summarize_time_series(mg_data)

    return {
        "window": window,
        "source": "prometheus",
        "step": step,
        "models": list(models.values()),
    }


async def _fetch_db_performance(
    window: str,
    model_group: Optional[str],
    scope: EntityScope,
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
    cache_key = f"{window}:{model_group or ''}:{scope.cache_key_suffix()}"
    if cache_ttl > 0:
        cached = _DB_CACHE.get_cache(cache_key)
        if cached is not None:
            return cached

    delta = _WINDOW_DELTAS[window]
    end_time = datetime.now(timezone.utc)
    start_time = end_time - delta
    bucket_interval = _WINDOW_BUCKET_INTERVALS.get(window, "1 hour")

    scope_sql, scope_params = scope.where_clause(start_idx=5)

    sql_query = f"""
        WITH base AS (
            SELECT
                COALESCE(model_group, '') AS model_group,
                date_bin($3::interval, "startTime", timestamp '2000-01-01') AS bucket,
                "startTime",
                "endTime",
                completion_tokens,
                request_duration_ms,
                "completionStartTime",
                "endTime" AS "end_time"
            FROM "LiteLLM_SpendLogs"
            WHERE
                "startTime" >= $1::timestamptz
                AND "startTime" < $2::timestamptz
                AND "cache_hit" != 'True'
                AND ($4::text IS NULL
                     OR COALESCE(model_group, '') = $4::text)
                {scope_sql}
        ),
        bucketed AS (
            SELECT
                model_group,
                bucket,
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
                             AND "completionStartTime" != "end_time"
                        THEN EXTRACT(epoch FROM ("completionStartTime" - "startTime"))
                        ELSE NULL
                    END
                ) AS avg_ttft_seconds,
                PERCENTILE_CONT(0.50) WITHIN GROUP (
                    ORDER BY
                        CASE
                            WHEN "completionStartTime" IS NOT NULL
                                 AND "completionStartTime" != "end_time"
                            THEN EXTRACT(epoch FROM ("completionStartTime" - "startTime"))
                            ELSE NULL
                        END
                ) AS p50_ttft_seconds,
                PERCENTILE_CONT(0.95) WITHIN GROUP (
                    ORDER BY
                        CASE
                            WHEN "completionStartTime" IS NOT NULL
                                 AND "completionStartTime" != "end_time"
                            THEN EXTRACT(epoch FROM ("completionStartTime" - "startTime"))
                            ELSE NULL
                        END
                ) AS p95_ttft_seconds
            FROM base
            GROUP BY model_group, bucket
        ),
        concurrency AS (
            SELECT
                b.bucket AS bucket,
                b.model_group AS model_group,
                COUNT(o."startTime") AS concurrent_requests
            FROM bucketed b
            LEFT JOIN base o
                ON o.model_group = b.model_group
                AND o."startTime" <= (b.bucket + $3::interval / 2)
                AND (o."endTime" IS NULL OR o."endTime" > (b.bucket + $3::interval / 2))
            GROUP BY b.bucket, b.model_group
        )
        SELECT
            bk.model_group AS model_group,
            bk.bucket AS bucket,
            bk.request_count AS request_count,
            bk.total_completion_tokens AS total_completion_tokens,
            bk.avg_throughput_tokens_per_sec AS avg_throughput_tokens_per_sec,
            bk.avg_ttft_seconds AS avg_ttft_seconds,
            bk.p50_ttft_seconds AS p50_ttft_seconds,
            bk.p95_ttft_seconds AS p95_ttft_seconds,
            COALESCE(c.concurrent_requests, 0) AS concurrent_requests
        FROM bucketed bk
        LEFT JOIN concurrency c
            ON c.model_group = bk.model_group AND c.bucket = bk.bucket
        ORDER BY bk.model_group, bk.bucket
    """

    rows = await prisma_client.db.query_raw(
        sql_query,
        start_time,
        end_time,
        bucket_interval,
        model_group,
        *scope_params,
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
            models[mg]["time_series"]["concurrent_requests"].append(
                {"timestamp": ts_bucket, "value": _safe_float(row.get("concurrent_requests"))}
            )

            summary = models[mg]["summary"]
            summary["total_requests"] += int(row.get("request_count", 0))
            summary["total_tokens"] += int(row.get("total_completion_tokens", 0))

        for mg_data in models.values():
            _summarize_time_series(mg_data)

    source = "db"

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


def _summarize_time_series(mg_data: dict[str, Any]) -> None:
    """Fill the summary block of a single model entry from its time series.

    The DB path and the Prometheus path both build models via ``_empty_model_dict``
    and then fill ``summary`` from ``time_series``; sharing this one function
    keeps the two percentile/avg computations in sync.
    """
    ts = mg_data["time_series"]
    mg_data["summary"]["avg_concurrent"] = _avg_values(ts.get("concurrent_requests", []))
    mg_data["summary"]["avg_throughput"] = _avg_values(ts.get("throughput_tokens_per_sec", []))
    ttft_vals = [p["value"] for p in ts.get("ttft_seconds", []) if p["value"] is not None]
    if ttft_vals:
        ttft_vals.sort()
        mg_data["summary"]["p50_ttft"] = ttft_vals[len(ttft_vals) // 2]
        mg_data["summary"]["p95_ttft"] = ttft_vals[min(len(ttft_vals) - 1, int(math.ceil(len(ttft_vals) * 0.95)) - 1)]
    else:
        mg_data["summary"]["p50_ttft"] = None
        mg_data["summary"]["p95_ttft"] = None


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
