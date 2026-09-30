# Directory Structure

Single source of truth for **where things live** in `oicm-litellm-layer/`.
Every file maps to its purpose so an agent (or human) knows exactly which file
to open to edit a given thing.

## Top-level layout

```
oicm-litellm-layer/
├── README.md               ← project overview + architecture diagram
├── CHANGELOG.md            ← change history (newest first)
├── CLAUDE.md               ← layer coding guidelines (docs nav rules)
├── Makefile                ← build/push/deploy targets
├── pyproject.toml          ← Python project config (oicm-discovery entry point)
├── mkdocs.yml              ← THIS documentation site's config (nav = registry)
├── requirements-docs.txt   ← docs-site build deps (mkdocs + material)
├── Dockerfile.dev          ← local dev container
├── docker-compose.dev.yml  ← local dev stack
├── .python-version
├── .env.datasource         ← local datasource env (see example)
├── .env.datasource.example
│
├── controller/             ← DISCOVERY CONTROLLER (component #1)
│   ├── __main__.py         ← entry point
│   ├── config.py           ← env vars, constants (incl. LITELLM_ADMIN_KEY default)
│   ├── controller.py       ← orchestration, reconcile loop, inline health server
│   ├── reconciler.py       ← model reconciliation
│   ├── models.py           ← OicmModel dataclass
│   ├── litellm_client.py   ← LiteLLM REST API client (uses LITELLM_ADMIN_KEY)
│   ├── Dockerfile
│   ├── README.md           ← controller dev docs (env var table here)
│   ├── sources/            ← model sources (ABC + impls)
│   │   ├── base.py
│   │   ├── local_deployments.py
│   │   └── submariner_imports.py
│   ├── fallbacks/          ← fallback service
│   │   ├── client.py  service.py
│   ├── pricing/            ← model pricing resolution
│   │   ├── aggregator.py  matchers.py  models.py  normalizer.py
│   │   ├── resolver.py  source.py  utils.py
│   └── status/             ← OICM status → LiteLLM status mapping
│       ├── base.py  builder.py  models.py  oicm.py
│
├── config/                 ← component #5: LiteLLM PROXY CONFIG
│   ├── litellm_config.yaml   ← production config (deployed as ConfigMap)
│   ├── local_dev.yaml        ← local dev proxy config (master_key: os.environ/LITELLM_MASTER_KEY)
│   ├── local_datasource.yaml ← local datasource validation config
│   └── local_test_voice.yaml ← local voice test config
│
├── hooks/                  ← components #3 & #4: LiteLLM callbacks/hooks
│   ├── vllm_param_injector.py  ← relocates vLLM params to extra_body
│   ├── keda_metrics.py         ← Prometheus gauge for KEDA
│   ├── priority_bridge.py      ← HTB priority bridge
│   └── __init__.py
│
├── decor/                  ← images/assets (logo, favicon)
│
├── deploy/                 ← KUBERNETES MANIFESTS (grouped by environment)
│   ├── prod/                          ← production manifests (apply these)
│   │   ├── litellm-proxy.yaml              ← proxy Deployment + Secrets +
│   │   │                                      ConfigMaps + Service + PDB
│   │   ├── discovery-controller.yaml       ← controller Deployment + RBAC + SA
│   │   ├── litellm-redis.yaml              ← Redis StatefulSet
│   │   ├── litellm-ingress.yaml            ← ingress
│   │   ├── litellm-servicemonitor.yaml     ← Prometheus ServiceMonitor
│   │   ├── litellm-network-policy-to-adeo.yaml
│   │   ├── litellm-postgres-cluster.yaml
│   │   ├── litellm-postgres-recovery.yaml
│   │   ├── old-postgres-pvcs.yaml
│   │   └── spend-logs-janitor/             ← CronJob + PVC + scripts/sql
│   ├── dev/                           ← dev variants (proxy, controller,
│   │   │                                  config, postgres, servicemonitor,
│   │   │                                  + spend-logs-janitor/)
│   └── rollback/                      ← rollback manifests pinned to versions
│       ├── litellm-proxy-rollback-jya0-v1.97.0.yaml ← pinned to image jya0-v1.97.0
│       ├── litellm-proxy-rollback-jya0-v1.96.2.yaml ← pinned to image jya0-v1.96.2
│       ├── litellm-proxy-rollback-key.yaml ← Secret for rollback apply
│       └── discovery-controller-rollback-key.yaml
│
├── docs/                   ← ALL documentation (this site; mkdocs.yml nav is the
│   │                          registry — every .md must appear in nav)
│   ├── index.md            ← mkdocs home (quick navigator)
│   ├── structure.md
│   ├── credentials.md
│   ├── deployment.md
│   ├── docs-map.md
│   ├── oicm-slices.md
│   ├── components/
│   │   ├── controller.md
│   │   ├── config.md
│   │   ├── hooks.md
│   │   ├── custom-routes.md
│   │   └── patches.md
│   ├── admin-api/          ← LiteLLM admin REST API guides
│   ├── custom-providers/   ← custom provider research/audit/architecture (HAMSA, INCEPTION, OMNIVOICE)
│   ├── custom-routes-plans/ ← custom-route logic map + VSA implementation plan
│   ├── dashboard-plan/     ← dashboard/frontend analysis
│   ├── discovery-controller/
│   ├── htb-rate-limiting/  ← HTB rate limiting + priority queue (+ live-data/ artifacts)
│   ├── incidents/          ← dated incident reports (one dir per incident)
│   ├── model-pricing/      ← pricing logic maps
│   ├── oicm-status/        ← status-API feasibility + evidence artifacts
│   ├── usage-guides/       ← how-to call providers/models through the gateway (Hamsa, Inception, OmniVoice, Qwen, VibeVoice)
│   ├── performance/        ← performance before/after + OOM logic maps (+ live-data/ evidence)
│   ├── reports/            ← generated / exported reports (e.g. model performance snapshots)
│   ├── session-identity/   ← session-id resolver implementation plan
│   ├── techniques/         ← reusable analysis techniques (logic mapping, code smells)
│   ├── runbooks/           ← operational runbooks (mkdocs setup, datasource validation)
│   ├── architecture/       ← integration-layer implementation plan
│   ├── cache-invalidation/  ← cache invalidation design + testing
│   ├── oicm-slices.md       ← OICM vertical-slice locations & pattern (see below)
│
├── ../ (upstream litellm source tree, co-located OICM slices live there)
│   ├── litellm/proxy/voice_routes.py                          ← voice routes slice
│   ├── litellm/endpoints/voice/                          ← voice/script SDK slice
│   ├── litellm/llms/oicm_providers/                     ← OICM provider config registry
│   ├── litellm/integrations/prometheus_helpers/          ← in-flight deployment gauge slice
│   ├── litellm/proxy/hooks/dynamic_rate_limiter_v3_htb.py ← HTB rate limiter
│   ├── litellm/proxy/management_helpers/                  ← team cache invalidation slice
│   ├── tests/test_litellm/proxy/test_oicm_drop_detection.py
│   └── ui/litellm-dashboard/src/components/UsagePage/components/ModelPerformance/
│
├── scripts/                ← helper scripts
│   ├── get_master_key.py       ← prints the master key from deploy/prod/litellm-proxy.yaml (single source)
│   ├── mkdocs_master_key.py    ← MkDocs hook injecting {{ master_key }} into docs
│   ├── port-forward-datasources.sh ← datasource local-validation port-forwards
│   ├── copy-prod-db-to-dev.sh  ← prod DB copy for dev analysis
│   ├── htb_test.py  htb_test_v2.py ← HTB limiter live tests
│   ├── backfill_hosted_vllm_spend.py  ← spend backfill after model onboarding
│   ├── rebuild_daily_spend_rollups.py ← daily-spend rollup rebuild
│   ├── add_model_if_has_refs.sh ← add model only if price refs exist
│   ├── remove_dangling_model_refs.sh ← clean dangling model references
│   ├── probe_oicm_status_api.py ← OICM status API probe
│   ├── vllm-0.20.0/        ← vLLM 0.20.0 onboarding bundle (onboard/offboard)
│   ├── vllm-0.20.0-no-work/ ← same bundle, no-work variant
│   └── model-server-onboarding-no-work/ ← generic onboarding bundle
│
├── benchmarks/             ← benchmark scripts
│   ├── bench_after.py  bench_final.py  bench_2replicas.py  bench_minimax_vision.py
│
├── mock-data/              ← OpenRouter / model-info mock data for local dev + tests
│   ├── build_master_mock.py  litellm_model_info.json  openrouter-models.json
│   └── upstream/             ← raw runtime probes (sglang/vllm)
│
├── examples/               ← example files
│   ├── custom/tryhamsastt/  ← HAMSA STT WebSocket test page
│   └── openrouter/          ← /api/v1/models demo
│
├── tests/                  ← tests (controller, hooks)
│   ├── controller/
│   │   ├── test_reconciler.py
│   │   └── pricing/        ← pricing tests
│   └── hooks/
│       └── test_priority_bridge.py
│
└── downloaded_sources/     ← git-ignored; pinned upstream source trees kept
                               locally for reference (litellm_v1.102.0,
                               vllm_router_pr217, sglang_reference,
                               llmd_issue1980_pr14)
```

## What maps to what task

| You want to... | Open |
|----------------|------|
| Change the proxy master key / UI password | `deploy/prod/litellm-proxy.yaml` (single source) + restart both Deployments. See `docs/credentials.md` |
| Edit discovery controller logic | `controller/controller.py`, `controller/reconciler.py`, `controller/sources/*` |
| Edit controller env defaults | `controller/config.py` |
| Edit LiteLLM proxy settings | `config/litellm_config.yaml` |
| Add/edit a callback hook | `hooks/*.py` |
| Review a custom-route plan | `docs/custom-routes-plans/*` (implementation code lives in the litellm source tree per the VSA plan) |
| Deploy / apply / rollout | `deploy/*.yaml` (see `docs/deployment.md`) |
| Apply an upstream patch | none active (see `docs/components/patches.md`) |
| Run local proxy | `config/local_dev.yaml` via `Makefile` |
| Generate / serve mock model data | `mock-data/` |
| Apply the wildcard TLS cert | `docs/SSL/` runbooks + scripts |
| Find a doc | `docs/docs-map.md` |
| Read an incident report | `docs/incidents/<date>-<slug>/` |