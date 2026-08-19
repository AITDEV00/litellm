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
CLUSTER_DOMAIN = os.getenv("CLUSTER_DOMAIN", "svc.cluster.local")
MODEL_PORT = int(os.getenv("MODEL_PORT", "8080"))
SYNC_INTERVAL = int(os.getenv("SYNC_INTERVAL", "300"))
WATCH_TIMEOUT = int(os.getenv("WATCH_TIMEOUT", "300"))
HEALTH_PORT = int(os.getenv("HEALTH_PORT", "8090"))
HTTP_CONCURRENCY = int(os.getenv("HTTP_CONCURRENCY", "50"))

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
