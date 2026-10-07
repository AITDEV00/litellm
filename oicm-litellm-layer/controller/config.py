import logging
import os
from pathlib import Path

import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

LITELLM_ADMIN_URL = os.getenv("LITELLM_ADMIN_URL", "http://localhost:4000")


def _master_key_from_manifest() -> str | None:
    """Read the master key from the single source of truth when running locally.

    The authoritative value lives in the inline `litellm-master-key` Secret in
    `deploy/prod/litellm-proxy.yaml`. In-cluster the Deployment always overrides it
    via `LITELLM_ADMIN_KEY` + `secretKeyRef`, so this fallback only matters for
    local runs (controller/ relative to this file). Returns None if the manifest
    cannot be read, in which case callers fall back to a hardcoded dev default.
    """
    manifest = Path(__file__).resolve().parent.parent / "deploy" / "prod" / "litellm-proxy.yaml"
    try:
        for document in yaml.safe_load_all(manifest.read_text(encoding="utf-8")):
            if not isinstance(document, dict):
                continue
            if document.get("kind") != "Secret":
                continue
            if (document.get("metadata") or {}).get("name") != "litellm-master-key":
                continue
            value = (document.get("stringData") or {}).get("master-key")
            if isinstance(value, str) and value.strip():
                return value.strip()
    except OSError:
        return None
    return None


LITELLM_ADMIN_KEY = os.getenv("LITELLM_ADMIN_KEY") or _master_key_from_manifest() or "sk-1234"
NAMESPACE = os.getenv("WATCH_NAMESPACE", "adeo")


def _cluster_name() -> str:
    """The cluster this controller runs in, from ``CLUSTER_NAME``.

    Required rather than defaulted. A model's cluster is what answers "Abu Dhabi
    or Al Ain" and is how a consumer finds the right source heartbeat, so a
    guessed value would be a wrong answer that looks authoritative. A missing
    value fails at startup, which is the only place it can be noticed; the
    Deployment manifests all set it.
    """
    value = (os.getenv("CLUSTER_NAME") or "").strip()
    if not value:
        raise RuntimeError(
            "CLUSTER_NAME is not set. It names the cluster this controller runs "
            "in (for example 'alain') and is stored on every model row as "
            "oicm_cluster. Set it in the Deployment manifest."
        )
    return value


CLUSTER_NAME = _cluster_name()
CLUSTER_DOMAIN = os.getenv("CLUSTER_DOMAIN", "svc.cluster.local")
MODEL_PORT = int(os.getenv("MODEL_PORT", "8080"))
SYNC_INTERVAL = int(os.getenv("SYNC_INTERVAL", "300"))
WATCH_TIMEOUT = int(os.getenv("WATCH_TIMEOUT", "300"))
HEALTH_PORT = int(os.getenv("HEALTH_PORT", "8090"))
HTTP_CONCURRENCY = int(os.getenv("HTTP_CONCURRENCY", "50"))
# Per-deployment discovery fan-out. Each deployment costs a ConfigMap read plus
# two pod-local probes, so this bounds the probe sockets opened at once.
DISCOVER_CONCURRENCY = int(os.getenv("DISCOVER_CONCURRENCY", "20"))

# Shared HTTP timeouts. One client per component is reused across calls, so
# these are the per-request ceilings rather than per-connection.
HTTP_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", "30"))
# Pod-local probes (in-cluster Service DNS) fail fast.
PROBE_TIMEOUT_SECONDS = float(os.getenv("PROBE_TIMEOUT_SECONDS", "5"))
# Cross-cluster probes (Submariner globalnet) and gateway writes.
REMOTE_TIMEOUT_SECONDS = float(os.getenv("REMOTE_TIMEOUT_SECONDS", "10"))

# When true, the controller discovers models and computes the reconciliation
# plan but NEVER writes to the LiteLLM gateway. Writes become logged no-ops.
# Used by the debug controller so it can observe / reproduce discovery without
# mutating the production gateway.
CONTROLLER_READ_ONLY = os.getenv("CONTROLLER_READ_ONLY", "false").lower() in (
    "true",
    "1",
    "yes",
)

WORKLOAD_TYPE_LABEL = "oip/workload-type"
WORKLOAD_ID_LABEL = "oip/workload-id"
MODEL_DEPLOYMENT_TYPE = "model_deployment"

# OICM status sources are declared in a ConfigMap, not here. See
# ``sources_config.py`` for the schema and ``deploy/oicm/sources.yaml`` for the
# definitions. Credentials come from each source's own environment variables,
# wired from a Secret by the Deployment.

# Status poll cadence. OICM itself refreshes deployment status on a 5s DB sync
# plus a 10s informer reload, so polling faster than 10s adds load without
# fresher data. One call covers every deployment in a source's workspace.
STATUS_SYNC_INTERVAL = int(os.getenv("STATUS_SYNC_INTERVAL", "10"))

# How long a source's last successful poll may be old before its status is
# reported unknown rather than trusted. Three poll intervals, so one dropped or
# slow cycle does not flap a healthy source.
STATUS_STALE_AFTER = int(os.getenv("STATUS_STALE_AFTER", "90"))

# Heartbeat write cadence, derived from the staleness window rather than the
# poll interval: a per-source liveness write every 10s would be six writes a
# minute for a timestamp no consumer reads at that resolution.
HEARTBEAT_INTERVAL = max(1, STATUS_STALE_AFTER // 3)

ENABLE_SUBMARINER_IMPORTS = os.getenv("ENABLE_SUBMARINER_IMPORTS", "true").lower() in (
    "true",
    "1",
    "yes",
)

PRICING_ENABLED = os.getenv("PRICING_ENABLED", "true").lower() in (
    "true",
    "1",
    "yes",
)
PRICING_JSON_PATH = os.getenv(
    "PRICING_JSON_PATH",
    "/app/model_prices_and_context_window.json",
)
PRICING_REFRESH_INTERVAL_SECONDS = int(
    os.getenv("PRICING_REFRESH_INTERVAL_SECONDS", "3600")
)
PRICING_MATCH_THRESHOLD = float(os.getenv("PRICING_MATCH_THRESHOLD", "0.80"))
