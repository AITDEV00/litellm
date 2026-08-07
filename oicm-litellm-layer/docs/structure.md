# Directory Structure

Single source of truth for **where things live** in `oicm-litellm-layer/`.
Every file maps to its purpose so an agent (or human) knows exactly which file
to open to edit a given thing.

## Top-level layout

```
oicm-litellm-layer/
├── README.md               ← project overview + architecture diagram
├── CHANGELOG.md            ← change history
├── Makefile                ← build/push/deploy targets
├── pyproject.toml          ← Python project config
├── mkdocs.yml              ← THIS documentation site's config
├── .python-version
├── .env.datasource         ← local datasource env (see example)
├── .env.datasource.example
│
├── controller/             ← DISCOVERY CONTROLLER (component #1)
│   ├── __main__.py         ← entry point
│   ├── config.py           ← env vars, constants (incl. LITELLM_ADMIN_KEY default)
│   ├── controller.py       ← orchestration, reconcile loop
│   ├── reconciler.py       ← model reconciliation
│   ├── models.py           ← OicmModel dataclass
│   ├── litellm_client.py   ← LiteLLM REST API client (uses LITELLM_ADMIN_KEY)
│   ├── health.py           ← health server
│   ├── Dockerfile
│   ├── README.md           ← controller dev docs (env var table here)
│   ├── sources/            ← model sources (ABC + impls)
│   │   ├── base.py
│   │   ├── local_deployments.py
│   │   └── submariner_imports.py
│   ├── fallbacks/          ← fallback service
│   │   ├── client.py  models.py  service.py
│   └── pricing/            ← model pricing resolution
│       ├── aggregator.py  matchers.py  models.py  normalizer.py
│       ├── resolver.py  source.py  utils.py
│
├── config/                 ← component #5: LiteLLM PROXY CONFIG
│   ├── litellm_config.yaml   ← production config (deployed as ConfigMap)
│   ├── local_dev.yaml        ← local dev proxy config (master_key: sk-1234)
│   ├── local_datasource.yaml ← local datasource validation config
│   └── local_test_voice.yaml ← local voice test config
│
├── hooks/                  ← components #3 & #4: LiteLLM callbacks/hooks
│   ├── vllm_param_injector.py  ← relocates vLLM params to extra_body
│   ├── keda_metrics.py         ← Prometheus gauge for KEDA
│   ├── priority_bridge.py      ← HTB priority bridge
│   └── __init__.py
│
├── custom-routes/          ← custom route plugins
│   ├── CLONE-LOGIC-MAP.md
│   └── VSA-PLAN.md
│
├── patches/                ← fork patches against upstream litellm
│   └── embedding-extra-body.patch
│
├── decor/                  ← images/assets (logo, favicon)
│
├── deploy/                 ← KUBERNETES MANIFESTS (apply these)
│   ├── litellm-proxy.yaml        ← proxy Deployment + Secret + ConfigMap + Service + PDB
│   ├── discovery-controller.yaml ← controller Deployment + RBAC + ServiceAccount
│   ├── litellm-redis.yaml        ← Redis StatefulSet
│   ├── litellm-ingress.yaml      ← ingress
│   └── litellm-servicemonitor.yaml ← Prometheus ServiceMonitor
│
├── docs/                   ← human/agent documentation (this site + existing)
│   ├── index.md            ← THIS page (mkdocs home)
│   ├── structure.md
│   ├── credentials.md
│   ├── deployment.md
│   ├── docs-map.md
│   ├── components/
│   │   ├── controller.md
│   │   ├── config.md
│   │   ├── hooks.md
│   │   ├── custom-routes.md
│   │   └── patches.md
│   ├── admin-api/          ← LiteLLM admin REST API guides
│   ├── custom-providers/   ← provider guides (HAMSA, INCEPTION, OMNIVOICE)
│   ├── dashboard-plan/     ← dashboard/frontend analysis
│   ├── discovery-controller/
│   ├── htb-rate-limiting/  ← HTB rate limiting + priority queue
│   ├── model-pricing/      ← pricing logic maps
│   └── performance/        ← performance before/after
│
├── scripts/                ← helper scripts
│   ├── htb_test.py  htb_test_v2.py
│   └── port-forward-datasources.sh
│
├── benchmarks/             ← benchmark scripts
│
├── examples/               ← example files
│
├── tests/                  ← tests (controller, hooks)
│   ├── controller/
│   │   ├── test_reconciler.py
│   │   └── pricing/        ← pricing tests
│   └── hooks/
│       └── test_priority_bridge.py
│
└── decor/                  ← image assets (logo, favicon)
```

## What maps to what task

| You want to... | Open |
|----------------|------|
| Change the proxy master key / UI password | `deploy/litellm-proxy.yaml` + `controller/config.py` fallback + `docs/credentials.md` |
| Edit discovery controller logic | `controller/controller.py`, `controller/reconciler.py`, `controller/sources/*` |
| Edit controller env defaults | `controller/config.py` |
| Edit LiteLLM proxy settings | `config/litellm_config.yaml` |
| Add/edit a callback hook | `hooks/*.py` |
| Add a custom route | `custom-routes/` |
| Deploy / apply / rollout | `deploy/*.yaml` (see `docs/deployment.md`) |
| Apply an upstream patch | `patches/embedding-extra-body.patch` |
| Run local proxy | `config/local_dev.yaml` via `Makefile` |
| Find a doc | `docs/docs-map.md` |