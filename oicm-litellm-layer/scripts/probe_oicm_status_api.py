"""Probe the OICM status REST API and emit a markdown catalog.

Runs inside the discovery-controller pod (has controller.status + OICM_* env).
For each workspace-scoped endpoint it captures the live HTTP status, the real
JSON (truncated), and the derived field schema, then prints a single markdown
document to stdout.

Usage:
  kubectl exec deploy/oicm-discovery-controller-dev -- \
      python3 /app/scripts/probe_oicm_status_api.py > oicm-api-catalog.md
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from controller.config import (
    OICM_AUTH_URL,
    OICM_BASE_URL,
    OICM_CLIENT_ID,
    OICM_PASSWORD,
    OICM_REALM,
    OICM_USERNAME,
)

WS = "dfec2a9f-cc5c-4b7b-b608-990d3804e80c"
# One representative workload + its workload-run-id label (K8s oip/workload-run-id).
WL = "4f0a7c56-2ce3-4968-8022-c1d80fdd6ed8"
WR = "bdaab232-7c78-40fb-8c84-a2c888486f25"
METRIC_ID = "num_of_requests_waiting_in_queue"
# Short recent window keeps the metrics query fast (a 24h window times out).
METRICS_START = "2026-09-29T06:00:00Z"


async def get_token() -> str:
    url = f"{OICM_AUTH_URL}/realms/{OICM_REALM}/protocol/openid-connect/token"
    payload = {
        "client_id": OICM_CLIENT_ID,
        "username": OICM_USERNAME,
        "password": OICM_PASSWORD,
        "grant_type": "password",
        "scope": "openid",
    }
    async with httpx.AsyncClient(timeout=30, verify=False) as c:
        r = await c.post(url, data=payload)
        r.raise_for_status()
        return r.json()["access_token"]


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


async def main() -> None:
    token = await get_token()
    headers = {"Authorization": f"Bearer {token}"}
    jobs = [
        ("List deployments (workspace)", f"/workspaces/{WS}/deployments",
         "All model deployments in the workspace.", False),
        ("Get deployment", f"/workspaces/{WS}/deployments/{WL}",
         "Single deployment record. id == workload_id.", False),
        ("Deployment health", f"/workspaces/{WS}/deployments/{WL}/health",
         "Independent readiness signal (is_ready).", False),
        ("Deployment summary (workspace)", f"/workspaces/{WS}/deployment_summary",
         "Roll-up of deployment states.", False),
        ("Inference metrics meta", f"/workspaces/{WS}/deployments/{WL}/inference_metrics_meta",
         "The metric-id vocabulary + units.", False),
        ("Inference metrics (queue)", f"/workspaces/{WS}/deployments/{WL}/inference_metrics?metric_id={METRIC_ID}&start={METRICS_START}",
         "Prometheus-style series [[epoch, value], ...]. Requires metric_id + start (ISO).", False),
        ("List workload runs", f"/workspaces/{WS}/workloads/{WL}/workload_runs",
         "Runs of a workload.", False),
        ("Get workload run", f"/workspaces/{WS}/workloads/{WL}/workload_runs/{WR}",
         "One run incl. status_detail[].", False),
        ("Workload run events", f"/workspaces/{WS}/workloads/{WL}/workload_runs/{WR}/events",
         "SSE stream of K8s events. NOTE: this endpoint buffers/streams slowly and "
         "may exceed the read timeout on a busy run; the captured stream is in "
         "evidence/workload-run-events.sse.", True),
        ("Workload run resources", f"/workspaces/{WS}/workloads/{WL}/workload_runs/{WR}/resources",
         "Resource objects of the run.", False),
        ("Workload run workers", f"/workspaces/{WS}/workloads/{WL}/workload_runs/{WR}/workers",
         "Worker pods of the run.", False),
        ("Model servers summary", "/model_servers/summary",
         "Serving-image catalog (NOT workspace-scoped).", False),
    ]

    # Emit markdown header immediately, then stream each section as it lands.
    print("# OICM Status REST API - live catalog\n")
    print(f"- Base URL: `{OICM_BASE_URL}`")
    print(f"- Auth: realm `{OICM_REALM}`, client `{OICM_CLIENT_ID}`, password grant (`{OICM_USERNAME}`)")
    print(f"- Workspace: `{WS}`")
    print("- Captured: 2026-09-29 (dev controller, live responses)\n")
    print("Every endpoint below was probed live. Schemas are derived from the "
          "captured payload, not guessed.\n")
    print("---\n", flush=True)

    async with httpx.AsyncClient(timeout=30, verify=False, headers=headers) as c:
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


if __name__ == "__main__":
    asyncio.run(main())
