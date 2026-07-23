import logging
import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

LITELLM_ADMIN_URL = os.getenv("LITELLM_ADMIN_URL", "http://localhost:4000")
LITELLM_ADMIN_KEY = os.getenv("LITELLM_ADMIN_KEY", "sk-1234")
NAMESPACE = os.getenv("WATCH_NAMESPACE", "adeo")
CLUSTER_DOMAIN = os.getenv("CLUSTER_DOMAIN", "svc.cluster.local")
MODEL_PORT = int(os.getenv("MODEL_PORT", "8080"))
SYNC_INTERVAL = int(os.getenv("SYNC_INTERVAL", "300"))
WATCH_TIMEOUT = int(os.getenv("WATCH_TIMEOUT", "300"))
HEALTH_PORT = int(os.getenv("HEALTH_PORT", "8090"))
HTTP_CONCURRENCY = int(os.getenv("HTTP_CONCURRENCY", "50"))

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
