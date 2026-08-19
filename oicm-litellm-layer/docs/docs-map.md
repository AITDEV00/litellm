# Docs Map

Where every documentation file lives, so you can find an existing doc quickly.

## This site (mkdocs)

| Page | Purpose |
|------|---------|
| `docs/index.md` | Entry point / quick navigator |
| `docs/structure.md` | Full directory map |
| `docs/credentials.md` | **Master key & password contract + rotation runbook + ⚠️ SALT KEY (DO NOT TOUCH) ⚠️** |
| `docs/deployment.md` | Apply / rollout / cluster access |
| `docs/components/*.md` | Per-component navigation |

## Discovery Controller

| Doc | Location |
|-----|----------|
| Controller dev docs + env vars | `controller/README.md` |
| Controller overview | `docs/discovery-controller/README.md` |
| Logic map + code smell audit | `docs/discovery-controller/logic-map-and-code-smell-audit.md` |
| Pricing logic map | `docs/model-pricing/PRICING-LOGIC-MAP.md` |
| Pricing matching plan | `docs/model-pricing/PRICING-MATCHING-PLAN.md` |

## Admin API

| Doc | Location |
|-----|----------|
| Full admin REST API guide | `docs/admin-api/LITELLM-ADMIN-REST-API.md` |
| Athena read-only key guide | `docs/admin-api/ATHENA-READ-ONLY-KEY-GUIDE.md` |

## Rate limiting / priority

| Doc | Location |
|-----|----------|
| HTB README | `docs/htb-rate-limiting/HTB-README.md` |
| Priority bridge feasibility | `docs/htb-rate-limiting/PRIORITY-BRIDGE-FEASIBILITY.md` |
| Priority logic map | `docs/htb-rate-limiting/PRIORITY-LOGIC-MAP.md` |
| Priority queue evidence/results | `docs/htb-rate-limiting/PRIORITY-QUEUE-EVIDENCE.md`, `PRIORITY-QUEUE-RESULTS-SUMMARY.md` |
| Priority behaviour | `docs/htb-rate-limiting/priority_behaviour.md` |
| Executive summary | `docs/htb-rate-limiting/HTB-EXECUTIVE-SUMMARY.md` |

## Custom providers

| Doc | Location |
|-----|----------|
| Custom providers README | `docs/custom-providers/README.md` |
| Gateway guide | `docs/custom-providers/GATEWAY_GUIDE.md` |
| Endpoint architecture | `docs/custom-providers/LITELLM_ENDPOINT_ARCHITECTURE.md` |
| HAMSA / INCEPTION / OMNIVOICE | `docs/custom-providers/HAMSA_*.md`, `INCEPTION_*.md`, `OMNIVOICE_*.md` |

## Dashboard / frontend

| Doc | Location |
|-----|----------|
| Frontend analysis process | `docs/dashboard-plan/FRONTEND-ANALYSIS-PROCESS.md` |
| UI lint + change process | `docs/dashboard-plan/UI-LINT-AND-CHANGE-PROCESS.md` |
| Observability plan | `docs/dashboard-plan/OBSERVABILITY-IMPLEMENTATION-PLAN.md` |

## Performance

| Doc | Location |
|-----|----------|
| Executive summary | `docs/performance/executive-summary.md` |
| Before / after | `docs/performance/before-optimization.md`, `after-optimization.md` |
| Perf recovery / session notes | `docs/performance/model-performance-perf-recovery.md` |

## Techniques

| Doc | Location |
|-----|----------|
| Logic mapping technique | `docs/techniques/logic_mapping_technique.md` |
| Code smell detection technique | `docs/techniques/code_smell_detection_technique.md` |
| Upstream pull & branch merge | `docs/techniques/upstream_merge_technique.md` |
| Debug pod technique | `docs/techniques/debug_pod_technique.md` |

## OICM custom code

| Doc | Location |
|-----|----------|
| OICM vertical-slice locations & pattern | `docs/oicm-slices.md` |
| Drop-detection wiring tests | `tests/test_litellm/proxy/test_oicm_drop_detection.py` |

## Runbooks

| Doc | Location |
|-----|----------|
| MkDocs setup | `docs/runbooks/MKDOCS-SETUP.md` |
| Datasource local validation | `docs/runbooks/DATASOURCE-LOCAL-VALIDATION.md` |

## TLS / Certificates

| Doc | Location |
|-----|----------|
| Apply wildcard cert guideline | `docs/SSL/CERT-GUIDELINE.md` |
| Serve litellm.ecouncil.ae runbook | `docs/SSL/LITELLM-ECOUNCIL-RUNBOOK.md` |
| TLS secret scripts | `docs/SSL/create-tls-secret*.sh` |

## Architecture

| Doc | Location |
|-----|----------|
| Integration-layer implementation plan | `docs/architecture/IMPLEMENTATION_PLAN.md` |

## Cache invalidation

| Doc | Location |
|-----|----------|
| Cache invalidation fix testing | `docs/cache-invalidation/CACHE_INVALIDATION_FIX_TESTING.md` |