#!/usr/bin/env python3
"""Build a consolidated OpenRouter mock-data file from captured cluster fixtures.

Reads:
  - litellm_model_info.json   (authoritative /model/info from the gateway)
  - upstream/*.json           (raw runtime probe responses)

Writes:
  - openrouter-models.json    (single file for local dev + tests)
"""
from __future__ import annotations

import json
from pathlib import Path

BASE = Path(__file__).resolve().parent

RUNTIME_BY_UUID_PREFIX = {
    "0fc81706": "vllm",
    "100af4eb": "vllm",
    "1744c7c9": "sglang",
    "18f9cc7f": "sglang",
    "58e70e08": "vllm",
    "63254d75": "vllm",
    "698c3cfd": "flood-compute",
    "8197d3d2": "sglang",
    "8ac37081": "vllm",
    "9310947d": "vllm",
    "9917a251": "vllm",
    "9aff17c0": "inception",
    "9c57bce9": "hamsa",
    "a500a62d": "vllm",
    "b55d006f": "omnivoice",
    "cd2850fc": "hamsa",
    "d72f732b": "vllm",
    "d974abd1": "vllm",
    "dbc727e2": "vllm",
    "e2e85fcc": "inception",
    "0f8674c2": "vllm",
}

REMOTE_API_BASE = "242.0.0.253"


def infer_runtime(uuid: str, api_base: str) -> str:
    if REMOTE_API_BASE in api_base:
        return "vllm"
    candidates = [uuid]
    if "s-" in api_base:
        candidates.append(api_base.split("s-")[1])
    for candidate in candidates:
        for prefix, runtime in RUNTIME_BY_UUID_PREFIX.items():
            if candidate and prefix in candidate:
                return runtime
    return "unknown"


def main() -> None:
    model_info = json.loads((BASE / "litellm_model_info.json").read_text())
    models: list[dict] = []
    for entry in model_info.get("data", []):
        lp = entry.get("litellm_params", {})
        mi = entry.get("model_info", {})
        api_base = lp.get("api_base", "")
        uuid = mi.get("oicm_uuid", "")
        models.append(
            {
                "logical_model_name": entry.get("model_name"),
                "mode": mi.get("mode"),
                "litellm_provider": mi.get("litellm_provider"),
                "provider": lp.get("model"),
                "api_base": api_base,
                "runtime": infer_runtime(uuid, api_base),
                "input_cost_per_token": lp.get("input_cost_per_token"),
                "output_cost_per_token": lp.get("output_cost_per_token"),
                "max_tokens": mi.get("max_tokens"),
                "max_input_tokens": mi.get("max_input_tokens"),
                "max_output_tokens": mi.get("max_output_tokens"),
            }
        )
    payload = {
        "generated_from": "live adeo cluster",
        "runtime_mapping_note": "runtime inferred from deployment image; verify per model",
        "models": models,
    }
    out = BASE / "openrouter-models.json"
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {out} ({out.stat().st_size} bytes, {len(models)} models)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())