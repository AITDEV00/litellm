"""
Per-model-group performance endpoint (Tier 2).

Returns throughput, concurrent requests, and TTFT per model_group over
selectable time windows. DB-backed for 1h/24h/7d with sub-daily time
bucketing; Prometheus-backed for 5m/15m (real-time concurrent requests).
"""

import asyncio
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, cast

import fastapi
from fastapi import APIRouter, Depends, HTTPException, status

import litellm
from litellm._logging import verbose_proxy_logger
from litellm.caching.in_memory_cache import InMemoryCache
from litellm.proxy._types import CommonProxyErrors, ProxyException, UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy.db.db_transaction_queue.model_performance_rollup_update_queue import (
    add_histogram_counts,
    histogram_percentile,
)

router = APIRouter()

_VALID_WINDOWS = ("1m", "5m", "15m", "30m", "1h", "24h", "7d")

# Prisma's default HTTP engine timeout is 30s (prisma/http_abstract.py). A
# large custom time range (e.g. 30 days over millions of spend-log rows) can
# exceed that and time out server-side, surfacing as a 500. This module-level
# lazy client is created with a raised timeout and is used for DB queries whose
# window suggests they may be heavy. It is connected on first use and reused.
_heavy_query_prisma_client: Optional[Any] = None
_heavy_query_prisma_client_lock: asyncio.Lock = asyncio.Lock()


async def _get_heavy_query_prisma_client() -> Any:
    """Return a PrismaClient with a raised HTTP timeout, connected lazily.

    Mirrors the dedicated long-timeout client used to refresh the
    ``MonthlyGlobalSpend`` materialized view (spend_management_endpoints.py)
    so the 30s default engine timeout does not kill large-range queries.
    """
    global _heavy_query_prisma_client
    if _heavy_query_prisma_client is not None:
        return _heavy_query_prisma_client

    async with _heavy_query_prisma_client_lock:
        if _heavy_query_prisma_client is not None:
            return _heavy_query_prisma_client
        from litellm.proxy.proxy_server import proxy_logging_obj
        from litellm.proxy.utils import PrismaClient

        db_url = os.getenv("DATABASE_URL")
        if db_url is None:
            raise ProxyException(
                message=CommonProxyErrors.db_not_connected_error.value,
                type="internal_error",
                param="None",
                code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        client = PrismaClient(
            database_url=db_url,
            proxy_logging_obj=proxy_logging_obj,
            http_client={
                "timeout": 600,
            },
        )
        await client.connect()
        _heavy_query_prisma_client = client
        return client


_DB_CACHE: InMemoryCache = InMemoryCache(max_size_in_memory=64, default_ttl=300)

_DB_CACHE_TTL: dict[str, int] = {
    "1m": 0,
    "5m": 0,
    "15m": 0,
    "30m": 0,
    "1h": 0,
    "24h": 120,
    "7d": 300,
}

_WINDOW_DELTAS: dict[str, timedelta] = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
}

_WINDOW_BUCKET_INTERVALS: dict[str, str] = {
    "1m": "5 seconds",
    "5m": "15 seconds",
    "15m": "30 seconds",
    "30m": "1 minute",
    "1h": "5 minutes",
    "24h": "1 hour",
    "7d": "6 hours",
}

_PROMETHEUS_WINDOWS = ("1m", "5m", "15m", "30m", "1h")

# Ranges spanning at least this many seconds (14 days) are routed through the
# dedicated long-timeout Prisma client to avoid the 30s default HTTP timeout.
_HEAVY_RANGE_THRESHOLD_SECONDS = 14 * 24 * 60 * 60


def _bucket_interval_for_window(window: str, start_time: datetime, end_time: datetime) -> str:
    """Choose a bucket interval for a requested window/range.

    For the predefined windows we keep the tuned defaults. For a custom time
    range we scale the bucket so the range renders a reasonable number of
    points (target ~288 buckets, matching a 24h-at-5-min cadence): the larger
    the range, the coarser the bucket.
    """
    default = _WINDOW_BUCKET_INTERVALS.get(window)
    if default is not None and start_time is not None and end_time is not None:
        duration = end_time - start_time
        approx = _WINDOW_DELTAS[window]
        # If the custom range is close to the window default, keep the default.
        if abs((duration - approx).total_seconds()) < approx.total_seconds() * 0.5:
            return default
    duration = (end_time - start_time).total_seconds()
    if duration <= 0:
        return "5 minutes"
    per_bucket = max(duration / 240, 60)  # at least 1 minute buckets
    # Round up to a "nice" interval among common choices.
    for interval in (60, 300, 900, 1800, 3600, 7200, 21600, 43200, 86400):
        if per_bucket <= interval:
            return _format_interval_seconds(interval)
    return "7 days"


def _format_interval_seconds(seconds: int) -> str:
    """Render a bucket interval in seconds as a Postgres interval string."""
    for unit_s, unit_name in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds % unit_s == 0 and seconds // unit_s >= 1:
            count = seconds // unit_s
            return f"{count} {unit_name}{'s' if count != 1 else ''}"
    return f"{seconds} seconds"


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
    step: Optional[str] = fastapi.Query(
        default=None,
        description="Bucket/granularity for the time series (e.g. '1 hour', '6 hours'). Overrides the window default so the UI can zoom in/out the x-axis.",
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
    start_time: Optional[datetime] = fastapi.Query(
        default=None,
        description="Explicit start time (ISO-8601). Overrides the window-relative start when provided.",
    ),
    end_time: Optional[datetime] = fastapi.Query(
        default=None,
        description="Explicit end time (ISO-8601). Overrides the window-relative end when provided.",
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
    # A custom time range also forces the DB path: the Prometheus short-window
    # metrics cannot be queried for an arbitrary past interval.
    has_custom_range = start_time is not None or end_time is not None
    if scope.is_empty and not has_custom_range and window in _PROMETHEUS_WINDOWS:
        prom_result = await _fetch_prometheus_performance(window=window, model_group=model_group)
        if prom_result is not None:
            return prom_result

    return await _fetch_db_performance(
        window=window,
        model_group=model_group,
        scope=scope,
        bucket_interval=step,
        start_time=start_time,
        end_time=end_time,
    )


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

    model_filter = model_group

    range_str, step = _WINDOW_CONFIG.get(window, _WINDOW_CONFIG["1h"])
    end = datetime.now(timezone.utc)
    start = end - _parse_window_to_timedelta(range_str)

    label_filter = ""
    # The concurrency gauge carries litellm_model_name; the throughput counter
    # carries requested_model; the TTFT histogram carries ``model`` (not
    # litellm_model_name). Each metric is filtered on the label it actually
    # exposes so a model_group filter does not silently drop a metric to empty.
    if model_filter:
        quoted = _quote_promql_string_literal(model_filter)
        label_filter = f"{{litellm_model_name={quoted}}}"

    ttft_label_filter = ""
    if model_filter:
        quoted = _quote_promql_string_literal(model_filter)
        ttft_label_filter = f"{{model={quoted}}}"

    # Group by model_id: it is present on all three metrics, whereas
    # requested_model only exists on the token counter. This mirrors the
    # proven query in prometheus_api.get_per_model_metrics.
    # Group by model_id: it is present on all three metrics, whereas
    # requested_model only exists on the token counter. We ALSO carry the
    # readable model name through the aggregation so the name can be recovered
    # from each range series' own labels (see below): the concurrency gauge and
    # latency histogram expose ``litellm_model_name``, while the throughput
    # counter exposes ``requested_model``. Without the name in the grouping,
    # `sum by (model_id)` drops every other label and the name is unrecoverable
    # from the range result.
    queries = {
        # Concurrent requests is a live gauge. Report the peak concurrency
        # observed within each step interval, not the value at exactly the
        # step instant. Prometheus scrapes the gauge on its own cadence
        # (scrape_interval, typically 15s), so a plain instant query can miss a
        # short-lived burst that falls between scrape points. max_over_time
        # takes the max of every scrape sample inside the window, so each
        # plotted point is the true maximum concurrency seen in that interval.
        "concurrent_requests": (
            f"sum by (model_id, litellm_model_name) (max_over_time(litellm_deployment_in_progress_requests{label_filter}[{step}]))"
        ),
        "throughput_tokens_per_sec": (
            f"sum by (model_id, requested_model) (rate(litellm_output_tokens_metric_total{label_filter}[{range_str}]))"
        ),
        # TTFT is the time to first token for streaming requests, captured by
        # litellm_llm_api_time_to_first_token_metric (a histogram). It must not
        # be conflated with litellm_deployment_latency_per_output_token, which
        # measures total latency divided by completion tokens (i.e. per-token
        # throughput) and is a different quantity. This query keys on ``model``
        # because that is the label the TTFT histogram exposes (it has no
        # litellm_model_name label).
        "ttft_seconds": (
            f"histogram_quantile(0.5, sum by (le, model_id, model) (rate("
            f"litellm_llm_api_time_to_first_token_metric_bucket{ttft_label_filter}[{range_str}])))"
        ),
    }

    # Raw per-model_id time series, keyed by model_id, with the readable model
    # name recovered from each series' own labels.
    #
    # We intentionally recover the name from the RANGE series' labels rather
    # than a separate instant metadata query: the deployment metrics carry
    # ``litellm_model_name`` (concurrent gauge, latency histogram) or
    # ``requested_model`` (throughput counter) alongside ``model_id``. An
    # instant label-metadata query only sees series that are live *right now*;
    # a deployment that is idle, scaled to zero, or already removed by the
    # discovery controller no longer emits the gauge, so the instant query
    # misses its ``model_id`` and the model would surface as a raw UUID.
    series_by_id: dict[str, dict[str, list[dict]]] = {}
    model_id_to_name: dict[str, str] = {}

    for series_name, promql in queries.items():
        try:
            raw = await query_prometheus_range(promql, start, end, step)
        except (ValueError, OSError, RuntimeError) as e:
            verbose_proxy_logger.debug(f"Prometheus performance query '{series_name}' failed: {e}")
            continue
        for entry in raw:
            labels = cast(dict[str, str], entry.get("metric", {}))
            mid = labels.get("model_id", "") or labels.get("litellm_model_name", "")
            if not mid:
                continue
            series_by_id.setdefault(mid, {})[series_name] = _parse_range_result([entry])
            name = (
                labels.get("litellm_model_name", "")
                or labels.get("requested_model", "")
                or labels.get("model", "")
            )
            if name and mid not in model_id_to_name:
                model_id_to_name[mid] = name

    # Merge every model_id that shares the same readable model name into one
    # model group. Throughput/concurrency sum across deployments serving the
    # same model; TTFT uses the max latency per bucket (worst-case first token).
    models: dict[str, dict[str, Any]] = {}

    for mid, series in series_by_id.items():
        name = _strip_provider_prefix(model_id_to_name.get(mid) or mid)
        if name not in models:
            models[name] = _empty_model_dict(name)
        _merge_series_into(models[name], series, name)

    for mg_data in models.values():
        _summarize_time_series(mg_data)

    return {
        "window": window,
        "source": "prometheus",
        "step": step,
        "models": list(models.values()),
    }


def _merge_series_into(target: dict[str, Any], incoming: dict[str, list[dict]], model_group: str) -> None:
    """Accumulate ``incoming`` per-model_id series into ``target`` model group.

    For throughput and concurrent requests the bucket values are summed (the
    model may be served by several deployments); for TTFT we keep the max value
    at each timestamp (the slowest deployment to first token).
    """
    for series_name, points in incoming.items():
        if not points:
            continue
        if series_name == "ttft_seconds":
            _merge_points_max(target["time_series"][series_name], points, model_group)
        else:
            _merge_points_sum(target["time_series"][series_name], points, model_group)


def _merge_points_sum(target: list[dict], points: list[dict], model_group: str) -> None:
    # Seed with the values already accumulated in ``target`` (from a previous
    # deployment sharing the same model name) so merging sums rather than
    # replacing them.
    by_ts: dict[str, float] = {p["timestamp"]: p["value"] for p in target if p["value"] is not None}
    for p in points:
        val = p.get("value")
        if val is None:
            continue
        # Throughput and concurrent-requests are non-negative quantities. The
        # in-progress gauge can drift negative if inc/dec desync across proxy
        # restarts (a request spanning a restart leaves a permanent offset), so
        # clamp to 0 rather than surfacing impossible negative concurrency.
        by_ts[p["timestamp"]] = max(0.0, by_ts.get(p["timestamp"], 0.0) + float(val))
    _write_points(target, by_ts, model_group)


def _merge_points_max(target: list[dict], points: list[dict], model_group: str) -> None:
    by_ts: dict[str, float] = {p["timestamp"]: p["value"] for p in target if p["value"] is not None}
    for p in points:
        val = p.get("value")
        if val is None:
            continue
        fval = float(val)
        prev = by_ts.get(p["timestamp"])
        if prev is None or fval > prev:
            by_ts[p["timestamp"]] = fval
    _write_points(target, by_ts, model_group)


def _write_points(target: list[dict], by_ts: dict[str, float], model_group: str) -> None:
    """Overwrite ``target`` with merged points, keeping timestamp order."""
    target.clear()
    for ts in sorted(by_ts):
        target.append({"timestamp": ts, "value": by_ts[ts]})


async def _fetch_db_performance(
    window: str,
    model_group: Optional[str],
    scope: EntityScope,
    bucket_interval: Optional[str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
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
    cache_key = (
        f"{window}:{model_group or ''}:{bucket_interval or ''}:{scope.cache_key_suffix()}:"
        f"{start_time.isoformat() if start_time else ''}:{end_time.isoformat() if end_time else ''}"
    )
    if cache_ttl > 0:
        cached = _DB_CACHE.get_cache(cache_key)
        if cached is not None:
            return cached

    # The global (no entity-filter) view is served from the 1-minute rollup
    # table instead of the raw spend log, which is what keeps the endpoint fast
    # over large windows. Entity-scoped requests fall through to the raw scan
    # below, which filters on the entity columns directly.
    if scope.is_empty:
        return await _fetch_db_performance_from_rollup(
            window=window,
            model_group=model_group,
            bucket_interval=bucket_interval,
            start_time=start_time,
            end_time=end_time,
        )

    # A custom time range overrides the window-relative computation. The
    # default bucket interval is derived from the range duration so the UI's
    # "Select Time Range" picker renders a sensible number of points.
    if start_time is not None and end_time is not None:
        computed_end = end_time
        computed_start = start_time
    else:
        delta = _WINDOW_DELTAS[window]
        computed_end = datetime.now(timezone.utc)
        computed_start = computed_end - delta

    effective_bucket = bucket_interval or _bucket_interval_for_window(window, computed_start, computed_end)
    end_time = computed_end
    start_time = computed_start

    scope_sql, scope_params = scope.where_clause(start_idx=5)

    # The time-range filter, cache_hit exclusion and scope/model_group filters
    # are intentionally repeated in the aggregation CTEs below instead of
    # being factored out into a shared ``base`` CTE. A shared CTE is
    # materialized once and then re-scanned once per reference, so with three
    # references Postgres re-reads the full row set up to three times.
    # Aggregating straight from the table means the index range scan runs once
    # per CTE instead of a materialize + N re-scans.
    #
    # Concurrent requests is the PEAK concurrency observed within each bucket,
    # not the value at the bucket's left edge. Each request contributes a +1 at
    # its start and a -1 at its end; a running SUM of those changes over time is
    # the number of simultaneously active requests at any instant, so the max
    # of that running sum inside a bucket is the peak concurrency for that
    # bucket.
    sql_query = f"""
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
                {scope_sql}
            GROUP BY model_group, bucket
        ),
        concurrency AS (
            SELECT
                model_group,
                bucket,
                GREATEST(MAX(cumulative), 0) AS concurrent_requests
            FROM (
                SELECT
                    model_group,
                    date_bin($3::interval, ev_time, timestamp '2000-01-01') AS bucket,
                    cumulative
                FROM (
                    SELECT
                        model_group,
                        ev_time,
                        SUM(change) OVER (
                            PARTITION BY model_group
                            ORDER BY ev_time, change DESC
                        ) AS cumulative
                    FROM (
                        SELECT
                            COALESCE(model_group, '') AS model_group,
                            "startTime" AS ev_time,
                            1 AS change
                        FROM "LiteLLM_SpendLogs"
                        WHERE
                            "startTime" >= $1::timestamptz
                            AND "startTime" < $2::timestamptz
                            AND "cache_hit" != 'True'
                            AND ($4::text IS NULL
                                 OR COALESCE(model_group, '') = $4::text)
                            {scope_sql}
                        UNION ALL
                        SELECT
                            COALESCE(model_group, '') AS model_group,
                            LEAST("endTime", $2::timestamptz) AS ev_time,
                            -1 AS change
                        FROM "LiteLLM_SpendLogs"
                        WHERE
                            "startTime" >= $1::timestamptz
                            AND "startTime" < $2::timestamptz
                            AND "cache_hit" != 'True'
                            AND "endTime" IS NOT NULL
                            AND "endTime" >= $1::timestamptz
                            AND ($4::text IS NULL
                                 OR COALESCE(model_group, '') = $4::text)
                            {scope_sql}
                    ) ev
                ) run
            ) binned
            GROUP BY model_group, bucket
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

    # Ranges spanning at least ~2 weeks can exceed Prisma's 30s default HTTP
    # timeout over millions of rows, so route them through the dedicated
    # long-timeout client. The default 24h/7d presets are well under the 30s
    # limit and keep using the shared proxy client.
    range_duration = (end_time - start_time).total_seconds()
    query_client = (
        await _get_heavy_query_prisma_client() if range_duration >= _HEAVY_RANGE_THRESHOLD_SECONDS else prisma_client
    )
    rows = await query_client.db.query_raw(
        sql_query,
        start_time,
        end_time,
        effective_bucket,
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
        "step": effective_bucket,
        "models": list(models.values()),
    }

    if cache_ttl > 0:
        _DB_CACHE.set_cache(cache_key, result, ttl=cache_ttl)

    return result


async def _fetch_db_performance_from_rollup(
    window: str,
    model_group: Optional[str],
    bucket_interval: Optional[str] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
) -> dict:
    """DB-backed global performance read from ``LiteLLM_ModelPerformanceRollup``.

    Serves the global (no entity-filter) view, which is the common slow path.
    The rollup stores 1-minute per-model_group buckets, so instead of scanning
    the raw (multi-GB) spend log the endpoint reads a tiny time-window slice of
    the rollup and aggregates 1-minute rows up to the effective (coarser) bucket.

    Concurrency is recomputed at read time: each 1-minute row carries
    ``starts`` / ``ends`` counters (``starts`` in the request's start minute,
    ``ends`` in its end minute), and the running-sum peak is derived from the
    chronological sequence of 1-minute buckets.
    """
    from litellm.proxy.proxy_server import prisma_client

    if prisma_client is None:
        raise ProxyException(
            message=CommonProxyErrors.db_not_connected_error.value,
            type="internal_error",
            param="None",
            code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    cache_ttl = _DB_CACHE_TTL.get(window, 0)
    cache_key = (
        f"rollup:{window}:{model_group or ''}:{bucket_interval or ''}:"
        f"{start_time.isoformat() if start_time else ''}:{end_time.isoformat() if end_time else ''}"
    )
    if cache_ttl > 0:
        cached = _DB_CACHE.get_cache(cache_key)
        if cached is not None:
            return cached

    if start_time is not None and end_time is not None:
        computed_end = end_time
        computed_start = start_time
    else:
        delta = _WINDOW_DELTAS[window]
        computed_end = datetime.now(timezone.utc)
        computed_start = computed_end - delta

    effective_bucket = bucket_interval or _bucket_interval_for_window(window, computed_start, computed_end)
    end_time = computed_end
    start_time = computed_start

    # Aggregate the 1-minute rollup rows into the effective (coarser) buckets
    # in SQL. The rollup holds one row per model_group per minute, so a large
    # window like 30d/YTD would otherwise return tens of thousands of rows for
    # Prisma to serialize over its Rust-engine HTTP transport (measured ~16s
    # for 96k rows). Collapsing to coarse buckets first cuts that to a few
    # hundred rows and makes the endpoint fast. The histogram is collected with
    # json_agg (a JSON array of arrays) because Prisma's raw query cannot
    # deserialize a native 2D Postgres array.
    #
    # Concurrent requests is the PEAK concurrency observed within each bucket.
    # Each 1-minute rollup row records ``starts`` (requests that began in that
    # minute) and ``ends`` (requests that ended in it); a running SUM of
    # (starts - ends) over the minute sequence is the number of simultaneously
    # active requests, so the max of that running sum inside a bucket is its
    # peak concurrency. The running sum is NOT reset at coarse-bucket
    # boundaries (a request spanning a boundary must keep its concurrency), so
    # it is computed over the whole window and then binned.
    sql_query = """
        WITH agg AS (
            SELECT
                model_group,
                date_bin($1::interval, bucket_start, timestamp '2000-01-01') AS bucket,
                SUM(request_count)::bigint AS request_count,
                SUM(completion_tokens)::bigint AS completion_tokens,
                SUM(throughput_tokens_sum) AS throughput_tokens,
                SUM(ttft_seconds_sum) AS ttft_seconds_sum,
                MAX(ttft_histogram_edges) AS ttft_histogram_edges,
                json_agg(ttft_histogram_counts) AS ttft_histogram_counts
            FROM "LiteLLM_ModelPerformanceRollup"
            WHERE
                bucket_start >= $2::timestamptz
                AND bucket_start < $3::timestamptz
                AND ($4::text IS NULL OR model_group = $4::text)
            GROUP BY model_group, bucket
        ),
        minute_changes AS (
            SELECT
                model_group,
                bucket_start AS ev_time,
                starts - ends AS change
            FROM "LiteLLM_ModelPerformanceRollup"
            WHERE
                bucket_start >= $2::timestamptz
                AND bucket_start < $3::timestamptz
                AND ($4::text IS NULL OR model_group = $4::text)
        ),
        running AS (
            SELECT
                model_group,
                ev_time,
                SUM(change) OVER (PARTITION BY model_group ORDER BY ev_time) AS cumulative
            FROM minute_changes
        ),
        concurrency AS (
            SELECT
                model_group,
                date_bin($1::interval, ev_time, timestamp '2000-01-01') AS bucket,
                GREATEST(MAX(cumulative), 0) AS concurrent_requests
            FROM running
            GROUP BY model_group, bucket
        )
        SELECT
            a.model_group AS model_group,
            a.bucket AS bucket,
            a.request_count AS request_count,
            a.completion_tokens AS completion_tokens,
            a.throughput_tokens AS throughput_tokens,
            a.ttft_seconds_sum AS ttft_seconds_sum,
            a.ttft_histogram_edges AS ttft_histogram_edges,
            a.ttft_histogram_counts AS ttft_histogram_counts,
            COALESCE(c.concurrent_requests, 0) AS concurrent_requests
        FROM agg a
        LEFT JOIN concurrency c
            ON c.model_group = a.model_group AND c.bucket = a.bucket
        ORDER BY a.model_group, a.bucket
    """

    range_duration = (end_time - start_time).total_seconds()
    query_client = (
        await _get_heavy_query_prisma_client() if range_duration >= _HEAVY_RANGE_THRESHOLD_SECONDS else prisma_client
    )
    rows = await query_client.db.query_raw(
        sql_query,
        effective_bucket,
        start_time,
        end_time,
        model_group,
    )

    models: dict[str, dict[str, Any]] = {}
    # Each row is one coarse bucket for its model_group. Group them so the
    # summary percentiles can fold every bucket's histogram into a single
    # whole-window histogram (percentiles over the per-request TTFT
    # distribution, not over per-bucket means).
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        mg = row.get("model_group") or ""
        if not mg:
            continue
        grouped.setdefault(mg, []).append(row)

    for mg, coarse_rows in grouped.items():
        models[mg] = _rollup_coarse_buckets_to_model(mg, coarse_rows)

    source = "rollup"
    result = {
        "window": window,
        "source": source,
        "step": effective_bucket,
        "models": list(models.values()),
    }

    if cache_ttl > 0:
        _DB_CACHE.set_cache(cache_key, result, ttl=cache_ttl)

    return result


def _rollup_coarse_buckets_to_model(
    model_group: str,
    coarse_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a model entry from pre-aggregated coarse-bucket rows.

    The SQL read already collapsed the 1-minute rollup rows into the effective
    (coarser) buckets and computed each bucket's peak concurrency via a
    whole-window running sum of (starts - ends). Each ``coarse_rows`` entry is
    therefore one bucket: the summed counts/tokens/throughput/TTFT, the
    ``json_agg``'d histogram (a JSON array of per-1-minute count arrays), the
    shared edges, and the peak concurrency.

    This builder only renders the time series and folds every bucket's
    histogram into a single whole-window histogram for the summary percentiles,
    so p50/p95 reflect the true per-request TTFT distribution rather than the
    distribution of bucket means.
    """
    model = _empty_model_dict(model_group)
    summary = model["summary"]

    window_edges: list[float] = []
    window_counts: Optional[list[int]] = None

    for row in coarse_rows:
        count = int(row.get("request_count", 0) or 0)
        if count == 0:
            continue

        # The histogram is json_agg'd as a list of per-1-minute count arrays;
        # fold them element-wise into this bucket's counts.
        bucket_counts: Optional[list[int]] = None
        hist_json = row.get("ttft_histogram_counts")
        if hist_json:
            for inner in hist_json:
                inner_counts = [int(c) for c in inner]
                if bucket_counts is None:
                    bucket_counts = inner_counts
                else:
                    bucket_counts = add_histogram_counts(bucket_counts, inner_counts)

        edges = row.get("ttft_histogram_edges") or []
        # The final histogram edge is stored as ``infinity`` in Postgres, which
        # comes back as NULL through ``MAX``; restore it so
        # ``histogram_percentile`` sees the expected open upper bound.
        edges = [float("inf") if e is None else float(e) for e in edges]

        # Mean TTFT per request (seconds). ttft_seconds_sum only accumulates
        # for requests with a valid TTFT; the histogram total gives that count.
        hist_total = sum(bucket_counts) if bucket_counts else 0
        ttft_mean = None
        if hist_total > 0:
            ttft_mean = float(row.get("ttft_seconds_sum", 0.0) or 0.0) / hist_total

        ts_bucket = _format_bucket(row.get("bucket"))
        model["time_series"]["throughput_tokens_per_sec"].append(
            {
                "timestamp": ts_bucket,
                "value": (float(row.get("throughput_tokens", 0.0) or 0.0) / count) if count else 0.0,
            }
        )
        model["time_series"]["concurrent_requests"].append(
            {"timestamp": ts_bucket, "value": float(row.get("concurrent_requests", 0) or 0)}
        )
        model["time_series"]["ttft_seconds"].append(
            {"timestamp": ts_bucket, "value": ttft_mean if ttft_mean is not None else None}
        )

        summary["total_requests"] += count
        summary["total_tokens"] += int(row.get("completion_tokens", 0) or 0)

        # Fold this bucket's histogram into the whole-window histogram.
        if bucket_counts:
            if window_counts is None:
                window_counts = bucket_counts
                window_edges = edges
            else:
                window_counts = add_histogram_counts(window_counts, bucket_counts)

    # Summary percentiles come from the merged whole-window histogram, not the
    # per-bucket means. _summarize_time_series fills avg_concurrent/avg_throughput
    # and would overwrite p50/p95 from the bucket-mean series, so compute the
    # histogram percentiles and assign them after the call.
    _summarize_time_series(model)
    if window_counts is not None:
        p50 = histogram_percentile(0.50, window_edges, window_counts)
        p95 = histogram_percentile(0.95, window_edges, window_counts)
        p99 = histogram_percentile(0.99, window_edges, window_counts)
        if p50 is not None:
            summary["p50_ttft"] = p50
        if p95 is not None:
            summary["p95_ttft"] = p95
        if p99 is not None:
            summary["p99_ttft"] = p99

    return model


def _strip_provider_prefix(model_name: str) -> str:
    """Remove a known LiteLLM provider prefix (``hosted_vllm/``, ``openai/``...) from a model name.

    The DB-backed path reads ``model_group`` from SpendLogs, which holds the
    client-requested name with no provider prefix. The Prometheus path derives
    its model group from the ``litellm_model_name`` label, which is the
    deployment's configured ``model`` value and can carry a provider prefix
    (e.g. ``hosted_vllm/Qwen/...``). Stripping a known prefix keeps the two
    sources consistent so the Live and historical views show the same names.
    """
    provider, sep, remainder = model_name.partition("/")
    if sep and provider in litellm.provider_list:
        return remainder
    return model_name


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
            "p99_ttft": None,
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
        mg_data["summary"]["p99_ttft"] = ttft_vals[min(len(ttft_vals) - 1, int(math.ceil(len(ttft_vals) * 0.99)) - 1)]
    else:
        mg_data["summary"]["p50_ttft"] = None
        mg_data["summary"]["p95_ttft"] = None
        mg_data["summary"]["p99_ttft"] = None


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
    """Render a bucket timestamp as an ISO-8601 string with a UTC offset.

    Prisma returns tz-aware datetimes whose ``isoformat()`` already carries the
    offset (e.g. ``+00:00``). Naive datetimes are assumed to be UTC and are
    tagged as such. Appending a second offset unconditionally would produce a
    malformed ``...+00:00+00:00`` timestamp, so only tag when the tz is missing.
    """
    if bucket is None:
        return ""
    if isinstance(bucket, datetime):
        if bucket.tzinfo is None:
            return bucket.replace(tzinfo=timezone.utc).isoformat()
        return bucket.isoformat()
    return str(bucket)
