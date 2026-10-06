"""Probe the OICM status REST API and emit a markdown catalog.

Runs inside the discovery-controller pod (has controller.status + OICM_* env).
For each workspace-scoped endpoint it captures the live HTTP status, the real
JSON (truncated), and the derived field schema, then prints a single markdown
document to stdout.

Usage:
  kubectl exec deploy/oicm-discovery-controller-dev -- \
      python3 /app/scripts/probe_oicm_status_api.py > oicm-api-catalog.md

Only the workspace is an input (OICM_WORKSPACE_ID env, or --workspace). The
workload, workload-run, and metric ids are discovered from the live API: the
first deployment in the workspace, the newest run of that deployment's
workload, and every id advertised by inference_metrics_meta. There is no
workspace-list endpoint, so the workspace id cannot be discovered and must be
supplied.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from controller.config import (
    OICM_BASE_URL,
    OICM_CLIENT_ID,
    OICM_REALM,
    OICM_USERNAME,
    OICM_VERIFY_TLS,
)
from controller.status import OicmStatusSource

# Recent window keeps the metrics query fast (a 24h window times out).
_METRICS_LOOKBACK = timedelta(minutes=30)


def schema_of(value: Any, depth: int = 0) -> Any:
    """Summarize a JSON value's shape: type names + one level of nested keys."""
    if depth > 3:
        return "..."
    if isinstance(value, dict):
        return {k: schema_of(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        if not value:
            return "[]"
        return [schema_of(value[0], depth + 1), f"... ({len(value)} items)"]
    return type(value).__name__


def truncate(value: Any, max_str: int = 80) -> Any:
    if isinstance(value, dict):
        return {k: truncate(v, max_str) for k, v in value.items()}
    if isinstance(value, list):
        return [truncate(v, max_str) for v in value[:3]]
    if isinstance(value, str) and len(value) > max_str:
        return value[:max_str] + "…"
    return value


async def probe(c: httpx.AsyncClient, label: str, path: str, note: str = "", sse: bool = False) -> dict:
    url = f"{OICM_BASE_URL}/api/v1{path}"
    try:
        if sse:
            # Server-sent event stream never terminates; read a bounded chunk.
            lines: list[str] = []
            async with c.stream("GET", url, timeout=20) as r:
                async for line in r.aiter_lines():
                    if line:
                        lines.append(line)
                    if len(lines) >= 40:
                        break
            return {
                "label": label,
                "method": "GET",
                "path": f"/api/v1{path}",
                "note": note,
                "http": r.status_code,
                "json": None,
                "text": "\n".join(lines)[:2500],
            }
        r = await c.get(url, timeout=25)
        body: Any
        try:
            body = r.json()
        except Exception:
            body = r.text[:400]
        return {
            "label": label,
            "method": "GET",
            "path": f"/api/v1{path}",
            "note": note,
            "http": r.status_code,
            "json": body if isinstance(body, (dict, list)) else None,
            "text": body if isinstance(body, str) else None,
        }
    except Exception as e:
        return {
            "label": label,
            "method": "GET",
            "path": f"/api/v1{path}",
            "note": note,
            "http": "ERROR",
            "json": None,
            "text": f"{type(e).__name__}: {e}",
        }


async def _get_json(c: httpx.AsyncClient, path: str) -> Any:
    """GET a path under /api/v1 and return parsed JSON, or None on any failure."""
    try:
        r = await c.get(f"{OICM_BASE_URL}/api/v1{path}", timeout=25)
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError):
        return None


def _items(body: Any) -> list[dict]:
    """Return the list under ``items`` for a paged OICM response."""
    if isinstance(body, dict) and isinstance(body.get("items"), list):
        return [i for i in body["items"] if isinstance(i, dict)]
    return []


def _ids(body: Any, key: str = "id") -> list[str]:
    return [i[key] for i in _items(body) if isinstance(i.get(key), str)]


def _first_metric_ids(meta: Any) -> list[str]:
    """Metric ids advertised by inference_metrics_meta (falls back to the queue)."""
    metrics = meta.get("metrics") if isinstance(meta, dict) else None
    if isinstance(metrics, list):
        ids = [m["id"] for m in metrics if isinstance(m, dict) and isinstance(m.get("id"), str)]
        if ids:
            return ids
    return ["num_of_requests_waiting_in_queue"]


async def _discover(c: httpx.AsyncClient, ws: str) -> dict[str, Any]:
    """Resolve the representative workload, run, and metric ids from the live API.

    Picks the first deployment that has at least one workload run, so the
    run-scoped endpoints below return data instead of 404. Falls back to the
    first deployment when none has runs.
    """
    deployments = _ids(await _get_json(c, f"/workspaces/{ws}/deployments"))
    run_lists = await asyncio.gather(
        *(_get_json(c, f"/workspaces/{ws}/workloads/{dep}/workload_runs") for dep in deployments)
    )
    pairs = [(dep, _ids(body)) for dep, body in zip(deployments, run_lists)]
    chosen = next(((dep, runs) for dep, runs in pairs if runs), None)
    wl = chosen[0] if chosen else (deployments[0] if deployments else "")
    wr = chosen[1][0] if chosen else ""

    meta = await _get_json(c, f"/workspaces/{ws}/deployments/{wl}/inference_metrics_meta") if wl else None
    metric_ids = _first_metric_ids(meta)

    start = (datetime.now(timezone.utc) - _METRICS_LOOKBACK).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"wl": wl, "wr": wr, "metric_ids": metric_ids, "start": start}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=os.getenv("OICM_WORKSPACE_ID", ""))
    args = parser.parse_args()
    ws = args.workspace
    if not ws:
        parser.error("workspace id required: pass --workspace or set OICM_WORKSPACE_ID")

    source = OicmStatusSource()
    token = await source.auth.token()
    headers = {"Authorization": f"Bearer {token}"}

    try:
        async with httpx.AsyncClient(timeout=30, verify=OICM_VERIFY_TLS, headers=headers) as c:
            d = await _discover(c, ws)
            wl, wr = d["wl"], d["wr"]
            metric_ids, metrics_start = d["metric_ids"], d["start"]

            jobs = [
                ("List deployments (workspace)", f"/workspaces/{ws}/deployments",
                 "All model deployments in the workspace.", False),
                ("Get deployment", f"/workspaces/{ws}/deployments/{wl}",
                 "Single deployment record. id == workload_id.", False),
                ("Deployment health", f"/workspaces/{ws}/deployments/{wl}/health",
                 "Independent readiness signal (is_ready).", False),
                ("Deployment summary (workspace)", f"/workspaces/{ws}/deployment_summary",
                 "Roll-up of deployment states.", False),
                ("Inference metrics meta", f"/workspaces/{ws}/deployments/{wl}/inference_metrics_meta",
                 "The metric-id vocabulary + units.", False),
                *(
                    (f"Inference metrics ({mid})",
                     f"/workspaces/{ws}/deployments/{wl}/inference_metrics?metric_id={mid}&start={metrics_start}",
                     "Prometheus-style series [[epoch, value], ...]. Requires metric_id + start (ISO).",
                     False)
                    for mid in metric_ids
                ),
                ("List workload runs", f"/workspaces/{ws}/workloads/{wl}/workload_runs",
                 "Runs of a workload.", False),
                ("Get workload run", f"/workspaces/{ws}/workloads/{wl}/workload_runs/{wr}",
                 "One run incl. status_detail[].", False),
                ("Workload run events", f"/workspaces/{ws}/workloads/{wl}/workload_runs/{wr}/events",
                 "SSE stream of K8s events. NOTE: this endpoint buffers/streams slowly and "
                 "may exceed the read timeout on a busy run; the captured stream is in "
                 "evidence/workload-run-events.sse.", True),
                ("Workload run resources", f"/workspaces/{ws}/workloads/{wl}/workload_runs/{wr}/resources",
                 "Resource objects of the run.", False),
                ("Workload run workers", f"/workspaces/{ws}/workloads/{wl}/workload_runs/{wr}/workers",
                 "Worker pods of the run.", False),
                ("Model servers summary", "/model_servers/summary",
                 "Serving-image catalog (NOT workspace-scoped).", False),
            ]

            captured = datetime.now(timezone.utc).date().isoformat()

            # Emit markdown header immediately, then stream each section as it lands.
            print("# OICM Status REST API - live catalog\n")
            print(f"- Base URL: `{OICM_BASE_URL}`")
            print(f"- Auth: realm `{OICM_REALM}`, client `{OICM_CLIENT_ID}`, password grant (`{OICM_USERNAME}`)")
            print(f"- Workspace: `{ws}` (supplied; no workspace-list endpoint exists)")
            print(f"- Discovered: workload `{wl}`, run `{wr}`, metrics {metric_ids}")
            print(f"- Captured: {captured} (dev controller, live responses)\n")
            print("Every endpoint below was probed live. Schemas are derived from the "
                  "captured payload, not guessed.\n")
            print("---\n", flush=True)

            for label, path, note, sse in jobs:
                r = await probe(c, label, path, note, sse=sse)
                out = []
                out.append(f"## {r['label']}")
                out.append(f"`{r['method']} {r['path']}`  ")
                if r["note"]:
                    out.append(f"{r['note']}  ")
                out.append(f"**HTTP {r['http']}**\n")
                if r["json"] is not None:
                    out.append("**Response (truncated):**\n")
                    out.append("```json")
                    out.append(json.dumps(truncate(r["json"]), indent=2)[:2500])
                    out.append("```")
                    out.append("**Field schema:**\n")
                    out.append("```json")
                    out.append(json.dumps(schema_of(r["json"]), indent=2)[:1800])
                    out.append("```\n")
                else:
                    out.append("```")
                    out.append(str(r["text"])[:1500])
                    out.append("```\n")
                out.append("---\n")
                print("\n".join(out), flush=True)
    finally:
        await source.aclose()


if __name__ == "__main__":
    asyncio.run(main())
