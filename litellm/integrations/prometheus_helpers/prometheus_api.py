"""
Helper functions to query prometheus API
"""

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from litellm import get_secret
from litellm._logging import verbose_logger
from litellm.llms.custom_httpx.http_handler import (
    get_async_httpx_client,
    httpxSpecialProvider,
)

PROMETHEUS_URL: Optional[str] = get_secret("PROMETHEUS_URL")  # type: ignore
PROMETHEUS_SELECTED_INSTANCE: Optional[str] = get_secret("PROMETHEUS_SELECTED_INSTANCE")  # type: ignore
async_http_handler = get_async_httpx_client(llm_provider=httpxSpecialProvider.LoggingCallback)


async def get_metric_from_prometheus(
    metric_name: str,
):
    # Get the start of the current day in Unix timestamp
    if PROMETHEUS_URL is None:
        raise ValueError("PROMETHEUS_URL not set please set 'PROMETHEUS_URL=<>' in .env")

    query = f"{metric_name}[24h]"
    now = int(time.time())
    response = await async_http_handler.get(
        f"{PROMETHEUS_URL}/api/v1/query", params={"query": query, "time": now}
    )  # End of the day
    _json_response = response.json()
    verbose_logger.debug("json response from prometheus /query api %s", _json_response)
    results = response.json()["data"]["result"]
    return results


async def get_fallback_metric_from_prometheus():
    """
    Gets fallback metrics from prometheus for the last 24 hours
    """
    response_message = ""
    relevant_metrics = [
        "litellm_deployment_successful_fallbacks_total",
        "litellm_deployment_failed_fallbacks_total",
    ]
    for metric in relevant_metrics:
        response_json = await get_metric_from_prometheus(
            metric_name=metric,
        )

        if response_json:
            verbose_logger.debug("response json %s", response_json)
            for result in response_json:
                verbose_logger.debug("result= %s", result)
                metric = result["metric"]
                metric_values = result["values"]
                most_recent_value = metric_values[0]

                if PROMETHEUS_SELECTED_INSTANCE is not None:
                    if metric.get("instance") != PROMETHEUS_SELECTED_INSTANCE:
                        continue

                value = int(float(most_recent_value[1]))  # Convert value to integer
                primary_model = metric.get("primary_model", "Unknown")
                fallback_model = metric.get("fallback_model", "Unknown")
                response_message += f"`{value} successful fallback requests` with primary model=`{primary_model}` -> fallback model=`{fallback_model}`"
                response_message += "\n"
        verbose_logger.debug("response message %s", response_message)
    return response_message


def is_prometheus_connected() -> bool:
    if PROMETHEUS_URL is not None:
        return True
    return False


def _quote_promql_string_literal(value: str) -> str:
    """Render ``value`` as a PromQL double-quoted string literal.

    PromQL string literals follow Go's escape rules
    (https://prometheus.io/docs/prometheus/latest/querying/basics/): a
    backslash begins an escape sequence and a bare ``"`` ends the literal.
    Without escaping, callers that accept arbitrary user-supplied values
    (like the ``api_key`` filter on ``/global/spend/logs``) can inject extra
    label matchers or selectors and read cross-tenant metrics.

    JSON's quoting rules are a strict subset of Go's, so ``json.dumps`` of
    a Python string produces a literal Prometheus accepts: ``\\``, ``\\"``,
    and the standard ``\\n`` / ``\\t`` / ``\\uNNNN`` control-character
    escapes. The returned value already includes the surrounding quotes.
    """
    return json.dumps(value, ensure_ascii=False)


async def get_daily_spend_from_prometheus(api_key: Optional[str]):
    """
    Expected Response Format:
    [
    {
        "date": "2024-08-18T00:00:00+00:00",
        "spend": 1.001818099998933
    },
    ...]
    """
    if PROMETHEUS_URL is None:
        raise ValueError("PROMETHEUS_URL not set please set 'PROMETHEUS_URL=<>' in .env")

    # Calculate the start and end dates for the last 30 days
    end_date = datetime.utcnow()
    start_date = end_date - timedelta(days=30)

    # Format dates as ISO 8601 strings with UTC offset
    start_str = start_date.isoformat() + "+00:00"
    end_str = end_date.isoformat() + "+00:00"

    url = f"{PROMETHEUS_URL}/api/v1/query_range"

    if api_key is None:
        query = "sum(delta(litellm_spend_metric_total[1d]))"
    else:
        quoted_api_key = _quote_promql_string_literal(api_key)
        query = f"sum(delta(litellm_spend_metric_total{{hashed_api_key={quoted_api_key}}}[1d]))"

    params = {
        "query": query,
        "start": start_str,
        "end": end_str,
        "step": "86400",  # Step size of 1 day in seconds
    }

    response = await async_http_handler.get(url, params=params)
    _json_response = response.json()
    verbose_logger.debug("json response from prometheus /query api %s", _json_response)
    results = response.json()["data"]["result"]
    formatted_results = []

    for result in results:
        metric_data = result["values"]
        for timestamp, value in metric_data:
            # Convert timestamp to ISO 8601 string with UTC offset
            date = datetime.fromtimestamp(float(timestamp)).isoformat() + "+00:00"
            spend = float(value)
            formatted_results.append({"date": date, "spend": spend})

    return formatted_results


# ---------------------------------------------------------------------------
# Per-model real-time metrics (Tier 2)
# ---------------------------------------------------------------------------

_WINDOW_CONFIG: dict[str, tuple[str, str]] = {
    "1m": ("1m", "15s"),
    "15m": ("15m", "15s"),
    "1h": ("1h", "30s"),
    "24h": ("24h", "5m"),
    "7d": ("7d", "1h"),
}

_DEPLOYMENT_LABELS = (
    "model_id",
    "litellm_model_name",
    "api_base",
    "api_provider",
)


async def query_prometheus_range(
    query: str,
    start: datetime,
    end: datetime,
    step: str,
) -> list[dict]:
    """Run a Prometheus ``query_range`` and return raw result entries.

    Returns the list under ``data.result`` (each entry has ``metric`` and
    ``values`` keys). Use ``_parse_range_result`` to flatten into
    ``[{timestamp, value}, ...]``.
    """
    if PROMETHEUS_URL is None:
        raise ValueError("PROMETHEUS_URL not set")

    params = {
        "query": query,
        "start": start.timestamp(),
        "end": end.timestamp(),
        "step": step,
    }
    response = await async_http_handler.get(
        f"{PROMETHEUS_URL}/api/v1/query_range",
        params=params,
    )
    response.raise_for_status()
    data = response.json()
    return data.get("data", {}).get("result", [])


async def query_prometheus_instant(query: str) -> list[dict]:
    """Run a Prometheus instant query and return raw result entries."""
    if PROMETHEUS_URL is None:
        raise ValueError("PROMETHEUS_URL not set")

    response = await async_http_handler.get(
        f"{PROMETHEUS_URL}/api/v1/query",
        params={"query": query},
    )
    response.raise_for_status()
    data = response.json()
    return data.get("data", {}).get("result", [])


def _parse_range_result(result: list[dict]) -> list[dict]:
    """Convert a Prometheus range-query result into ``[{timestamp, value}, ...]``."""
    points: list[dict] = []
    for series in result:
        for ts, val in series.get("values", []):
            points.append(
                {
                    "timestamp": datetime.fromtimestamp(float(ts)).isoformat() + "+00:00",
                    "value": float(val),
                }
            )
    return points


def _extract_deployment_key(metric_labels: dict) -> tuple[str, str, str, str]:
    return (
        metric_labels.get("model_id", ""),
        metric_labels.get("litellm_model_name", ""),
        metric_labels.get("api_base", ""),
        metric_labels.get("api_provider", ""),
    )


def _empty_deployment_dict(key: tuple[str, str, str, str]) -> dict:
    return {
        "model_id": key[0],
        "litellm_model_name": key[1],
        "api_base": key[2],
        "api_provider": key[3],
        "rpm_limit": 0,
        "concurrent_requests": [],
        "request_rate": [],
        "output_tokens_per_sec": [],
        "latency_per_token_p50": [],
    }


async def get_per_model_metrics(
    window: str,
    model_id: Optional[str] = None,
) -> dict:
    """Query Prometheus for per-deployment time-series metrics.

    Returns a dict with ``prometheus_connected``, ``window``, ``step``, and
    ``deployments`` keys. Each deployment has 4 time-series arrays.
    """
    if PROMETHEUS_URL is None:
        return {
            "prometheus_connected": False,
            "window": window,
            "step": "",
            "deployments": [],
        }

    range_str, step = _WINDOW_CONFIG.get(window, _WINDOW_CONFIG["1h"])
    end = datetime.now(timezone.utc)
    start = end - _parse_window_to_timedelta(range_str)

    label_filter = ""
    if model_id:
        quoted = _quote_promql_string_literal(model_id)
        label_filter = f"{{model_id={quoted}}}"

    queries = {
        "concurrent_requests": f"max_over_time(litellm_deployment_in_progress_requests{label_filter}[{range_str}])",
        "request_rate": f"sum by ({','.join(_DEPLOYMENT_LABELS)}) (rate(litellm_deployment_total_requests_total{label_filter}[{range_str}]))",
        "output_tokens_per_sec": f"sum by ({','.join(_DEPLOYMENT_LABELS)}) (rate(litellm_output_tokens_metric_total{label_filter}[{range_str}]))",
        "latency_per_token_p50": f"histogram_quantile(0.50, sum by (le, {','.join(_DEPLOYMENT_LABELS)}) (rate(litellm_deployment_latency_per_output_token_bucket{label_filter}[{range_str}])))",
    }

    deployments: dict[tuple[str, str, str, str], dict] = {}

    for series_name, promql in queries.items():
        try:
            raw = await query_prometheus_range(promql, start, end, step)
        except Exception as e:  # noqa: BLE001
            verbose_logger.debug(f"Prometheus query '{series_name}' failed: {e}")
            continue

        for entry in raw:
            key = _extract_deployment_key(entry.get("metric", {}))
            if key not in deployments:
                deployments[key] = _empty_deployment_dict(key)
            deployments[key][series_name] = _parse_range_result([entry])

    try:
        rpm_raw = await query_prometheus_instant(f"litellm_deployment_rpm_limit{label_filter}")
        for entry in rpm_raw:
            key = _extract_deployment_key(entry.get("metric", {}))
            if key in deployments:
                deployments[key]["rpm_limit"] = int(float(entry.get("value", [0, "0"])[1]))
    except Exception as e:  # noqa: BLE001
        verbose_logger.debug(f"Prometheus rpm_limit query failed: {e}")

    return {
        "prometheus_connected": True,
        "window": window,
        "step": step,
        "deployments": list(deployments.values()),
    }


async def get_in_progress_requests_instant() -> list[dict]:
    """Get the current instant value of in-progress requests per deployment.

    Used as a fallback when Prometheus is not connected; queries the gauge
    via the Prometheus ``/api/v1/query`` instant endpoint.
    """
    if PROMETHEUS_URL is None:
        return []

    try:
        raw = await query_prometheus_instant("litellm_deployment_in_progress_requests")
        result: list[dict] = []
        for entry in raw:
            labels = entry.get("metric", {})
            result.append(
                {
                    "model_id": labels.get("model_id", ""),
                    "litellm_model_name": labels.get("litellm_model_name", ""),
                    "api_base": labels.get("api_base", ""),
                    "api_provider": labels.get("api_provider", ""),
                    "value": float(entry.get("value", [0, "0"])[1]),
                }
            )
        return result
    except Exception as e:  # noqa: BLE001
        verbose_logger.debug(f"Prometheus instant query failed: {e}")
        return []


def _parse_window_to_timedelta(window: str) -> timedelta:
    """Parse a PromQL-style duration string (``1m``, ``24h``, ``7d``) into ``timedelta``."""
    if window.endswith("m"):
        return timedelta(minutes=int(window[:-1]))
    if window.endswith("h"):
        return timedelta(hours=int(window[:-1]))
    if window.endswith("d"):
        return timedelta(days=int(window[:-1]))
    if window.endswith("w"):
        return timedelta(weeks=int(window[:-1]))
    raise ValueError(f"Cannot parse duration string: {window!r}")
