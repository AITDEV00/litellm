# Config (component #5)

The LiteLLM proxy configuration, deployed as a ConfigMap. This is what the
proxy loads on startup via `--config /app/config.yaml`.

## Config files

| File | Environment | Purpose |
|------|-------------|---------|
| `deploy/base/gateway/config/litellm-config.yaml` | Production | The **deployed** config, inlined as the `litellm-config` ConfigMap `data.config.yaml`. Models are registered dynamically by the controller (`model_list: []`) |
| `config/local_dev.yaml` | Local dev | `master_key: os.environ/LITELLM_MASTER_KEY`, no DB persistence |
| `config/local_test_voice.yaml` | Local test | voice test, `master_key: os.environ/LITELLM_MASTER_KEY` |
| `config/local_datasource.yaml` | Local datasource | reads `LITELLM_MASTER_KEY` from env |

## Where the production config lives in the cluster

The production config is the `litellm-config` ConfigMap declared at
`deploy/base/gateway/config/litellm-config.yaml` and rendered by both overlays.
When you edit production proxy settings, edit that file, then
`kubectl apply -k deploy/overlays/prod` (or `make deploy`).

!!! important
    The three `config/local_*.yaml` files are **local-run configs only**. There
    is no `config/litellm_config.yaml` in this repo: the production template is
    the ConfigMap at `deploy/base/gateway/config/litellm-config.yaml`.

## Sections to edit

| Section | Where | Notes |
|---------|-------|-------|
| `model_list` | `litellm-config` ConfigMap | Empty in prod (controller registers models) |
| `litellm_settings` | `litellm-config` ConfigMap | callbacks, caching, priority reservation, `store_model_in_db`, `require_auth_for_metrics_endpoint` |
| `general_settings.master_key` | `litellm-config` ConfigMap / `config/*.yaml` | See [Credentials](../credentials.md) |
| `router_settings` | `litellm-config` ConfigMap | routing strategy, `optional_pre_call_checks`, retry policy, timeouts |
| pass-through endpoints | `litellm-config` ConfigMap | `/vllm/tts` etc. |