#!/usr/bin/env bash
#
# Mirrors every model the production gateway serves onto this edge proxy, each
# pointed back at production. Credentials are baked into the image at
# /etc/litellm-edge/credentials.env, so this needs no arguments:
#
#   podman exec -it litellm-postgres /usr/local/bin/configure_edge_models.sh
#
# or from the host, through the Makefile:
#
#   make litellm-pg-mirror
#
# Optional, all defaulting to the baked-in file:
#   PROD_API_KEY          production virtual key, used as the edge's upstream key
#   PROD_BASE             default https://litellm.ecouncil.ae
#   EDGE_BASE             default http://localhost:4000
#   EDGE_MASTER_KEY       falls back to LITELLM_MASTER_KEY, which the container has
#   DRY_RUN               set to 1 to print what would change, touching nothing
#   ONLY                  comma-separated substrings; only matching model ids copy
#   EDGE_CREDENTIALS_FILE default /etc/litellm-edge/credentials.env
#
# Two production endpoints are read, because neither has everything:
#   /api/v1/models  the OpenRouter-compatible catalogue: context_length and
#                   pricing, which /v1/models does not carry
#   /model/info     the only source of mode (chat, embedding, audio_speech, ...)
# mode is load-bearing: an embedding or TTS deployment registered as chat will
# not route.
#
# Everything below runs in Python rather than curl because the runtime image has
# no curl, and the catalogue payload is far past the per-argument limit, so it
# cannot be passed around as an environment variable either.
#
# Credentials are read from EDGE_CREDENTIALS_FILE, which the image bakes in, so
# a plain `podman exec` needs no flags. The file is parsed as key=value rather
# than sourced: the password contains a literal $$ that any shell would expand.

set -euo pipefail

exec python3 - <<'PYTHON'
import json
import os
import sys
import urllib.error
import urllib.request


def load_credentials(path):
    """Parse key=value lines without shell expansion, so literal $$ survives."""
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return {}
    pairs = (
        (line.split("=", 1)[0].strip(), line.split("=", 1)[1])
        for line in lines
        if "=" in line and not line.lstrip().startswith("#")
    )
    return {key: value for key, value in pairs if key}


CREDENTIALS_FILE = os.environ.get("EDGE_CREDENTIALS_FILE", "/etc/litellm-edge/credentials.env")
CREDENTIALS = load_credentials(CREDENTIALS_FILE)


def setting(name, default=""):
    """Explicit environment wins, so a one-off override needs no file edit."""
    return os.environ.get(name) or CREDENTIALS.get(name) or default


EDGE_BASE = setting("EDGE_BASE", "http://localhost:4000").rstrip("/")
PROD_BASE = setting("PROD_BASE", "https://litellm.ecouncil.ae").rstrip("/")
PROD_API_KEY = setting("PROD_API_KEY")
EDGE_MASTER_KEY = setting("EDGE_MASTER_KEY") or setting("LITELLM_MASTER_KEY")
DRY_RUN = setting("DRY_RUN", "0") == "1"
ONLY = [p.strip() for p in setting("ONLY").split(",") if p.strip()]

if not PROD_API_KEY:
    sys.exit(f"PROD_API_KEY is not set and {CREDENTIALS_FILE} does not provide one")
if not EDGE_MASTER_KEY:
    sys.exit(f"EDGE_MASTER_KEY is not set and {CREDENTIALS_FILE} does not provide LITELLM_MASTER_KEY")


def call(url, key, payload=None, method=None):
    """Return (status, parsed_json_or_text). Never raises on HTTP status."""
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", "Bearer " + key)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        status = exc.code
    except Exception as exc:
        return 0, str(exc)
    try:
        return status, json.loads(body)
    except ValueError:
        return status, body


def catalogue():
    """name -> {context_length, prompt_price, completion_price} from production."""
    status, payload = call(PROD_BASE + "/api/v1/models", PROD_API_KEY)
    if status == 0:
        sys.exit(f"could not reach {PROD_BASE} ({payload}); not changing anything")
    if status in (401, 403):
        sys.exit(f"PROD_API_KEY rejected by {PROD_BASE} (HTTP {status}); not changing anything")
    if status != 200 or not isinstance(payload, dict):
        sys.exit(f"{PROD_BASE}/api/v1/models returned HTTP {status}; not changing anything")
    found = {}
    for entry in payload.get("data") or []:
        name = entry.get("id")
        if not name:
            continue
        pricing = entry.get("pricing") or {}
        found[name] = {
            "context_length": entry.get("context_length"),
            "prompt_price": pricing.get("prompt"),
            "completion_price": pricing.get("completion"),
        }
    return found


def modes():
    """name -> mode, from the one endpoint that reports it."""
    _, payload = call(PROD_BASE + "/model/info", PROD_API_KEY)
    if not isinstance(payload, dict):
        return {}
    return {
        row["model_name"]: (row.get("model_info") or {}).get("mode") or ""
        for row in payload.get("data") or []
        if row.get("model_name")
    }


def registered():
    """name -> model_id already on the edge, so reruns update instead of duplicate.

    /model/new mints a fresh row on every call, so without this a rerun doubles
    every deployment and the router load-balances across identical copies.
    """
    status, payload = call(EDGE_BASE + "/model/info", EDGE_MASTER_KEY)
    if status != 200 or not isinstance(payload, dict):
        sys.exit(f"edge not reachable at {EDGE_BASE} (HTTP {status}); is the proxy up?")
    return {
        row["model_name"]: (row.get("model_info") or {}).get("id")
        for row in payload.get("data") or []
        if row.get("model_name") and (row.get("model_info") or {}).get("id")
    }


def per_token(value):
    """Catalogue prices are per million tokens; LiteLLM stores per token."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number / 1_000_000 if number > 0 else None


def deployment(name, meta, mode):
    params = {
        "model": "openai/" + name,
        "api_base": PROD_BASE + "/v1",
        "api_key": PROD_API_KEY,
    }
    info = {"mode": mode or "chat"}

    context = meta.get("context_length")
    if isinstance(context, int) and context > 0:
        info["max_input_tokens"] = context
        info["max_tokens"] = context

    for source, target in (("prompt_price", "input_cost_per_token"),
                           ("completion_price", "output_cost_per_token")):
        cost = per_token(meta.get(source))
        if cost is not None:
            info[target] = cost
            params[target] = cost

    return params, info


def wanted(name):
    return not ONLY or any(pattern in name for pattern in ONLY)


def main():
    catalogue_models = catalogue()
    model_modes = modes()
    existing = registered()

    names = sorted(n for n in catalogue_models if wanted(n))
    skipped = len(catalogue_models) - len(names)

    print(f"discovered {len(catalogue_models)} model(s) at {PROD_BASE}")
    if ONLY:
        print(f"filtering to: {','.join(ONLY)}")
    print()

    created = updated = failed = 0

    for name in names:
        params, info = deployment(name, catalogue_models[name], model_modes.get(name, ""))
        mode = info["mode"]
        context = info.get("max_input_tokens")

        if DRY_RUN:
            action = "update" if name in existing else "create"
            print(f"[dry-run] {action:<7} {name:<45} mode={mode:<20} ctx={context}")
            continue

        if name in existing:
            status, body = call(
                f"{EDGE_BASE}/model/{existing[name]}/update",
                EDGE_MASTER_KEY,
                {"litellm_params": params, "model_info": info},
                method="PATCH",
            )
            verb = "updated"
        else:
            status, body = call(
                EDGE_BASE + "/model/new",
                EDGE_MASTER_KEY,
                {"model_name": name, "litellm_params": params, "model_info": info},
                method="POST",
            )
            verb = "created"

        if status == 200:
            print(f"{verb:<8} {name:<45} mode={mode}")
            if verb == "created":
                created += 1
            else:
                updated += 1
        else:
            detail = body if isinstance(body, str) else json.dumps(body)
            print(f"FAILED   {name:<45} HTTP {status}: {detail[:200]}", file=sys.stderr)
            failed += 1

    print()
    if DRY_RUN:
        print("dry run: nothing changed")
        return 0

    print(f"created={created} updated={updated} skipped={skipped} failed={failed}")

    status, payload = call(EDGE_BASE + "/v1/models", EDGE_MASTER_KEY)
    served = len(payload.get("data") or []) if isinstance(payload, dict) else "?"
    print(f"edge now serves {served} model(s)")

    return 0 if failed == 0 else 1


sys.exit(main())
PYTHON
