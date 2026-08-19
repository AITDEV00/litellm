# Config (component #5)

The LiteLLM proxy configuration, deployed as a ConfigMap. This is what the
proxy loads on startup via `--config /app/config.yaml`.

## Config files

| File | Environment | Purpose |
|------|-------------|---------|
| `config/litellm_config.yaml` | Production | Deployed to the cluster. Models are registered dynamically by the controller (`model_list: []`) |
| `config/local_dev.yaml` | Local dev | `master_key: os.environ/LITELLM_MASTER_KEY`, no DB persistence |
| `config/local_test_voice.yaml` | Local test | voice test, `master_key: os.environ/LITELLM_MASTER_KEY` |
| `config/local_datasource.yaml` | Local datasource | reads `LITELLM_MASTER_KEY` from env |

## Where the production config lives in the cluster

The production config is **inlined into** `deploy/prod/litellm-proxy.yaml` as the
`litellm-config` ConfigMap `data.config.yaml`. When you edit production proxy
settings, you edit that inline block in `deploy/prod/litellm-proxy.yaml`, not
`config/litellm_config.yaml` (that file is the reference template).

!!! important
    `config/litellm_config.yaml` is the source-of-truth **reference template**.
    The actually-deployed copy is the inline `config.yaml` inside
    `deploy/prod/litellm-proxy.yaml`. If you change one, keep the other in sync or
    note the drift deliberately.

## Sections to edit

| Section | Where | Notes |
|---------|-------|-------|
| `model_list` | ConfigMap inline block | Empty in prod (controller registers models) |
| `litellm_settings` | ConfigMap inline block | callbacks, caching, priority reservation |
| `general_settings.master_key` | ConfigMap / `config/*.yaml` | See [Credentials](../credentials.md) |
| `router_settings` | ConfigMap inline block | routing strategy, pre-call checks |
| pass-through endpoints | ConfigMap inline block | `/vllm/tts` etc. |