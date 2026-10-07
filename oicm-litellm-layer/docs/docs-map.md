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

## OICM Status

| Doc | Location |
|-----|----------|
| Deployment-status feasibility | `docs/oicm-status/OICM-STATUS-FEASIBILITY.md` |
| Question-by-question answers | `docs/oicm-status/FEASIBILITY-ANSWERS.md` |
| Implementation checklist | `docs/oicm-status/IMPLEMENTATION-CHECKLIST.md` |
| Write-access blocker (resolved) | `docs/oicm-write-access-blocked.md` |

## Incidents

| Doc | Location |
|-----|----------|
| Postgres disk-full (2026-09-08) | `docs/incidents/2026-09-08-postgres-disk-full/` (incident report, executive summary, resolutions) |
| Kimi-K3 mid-stream stop (2026-09-21) | `docs/incidents/2026-09-21-kimi-k3-midstream-stop/incident-report.md` |
| Spend-log cleanup not running (2026-09-21) | `docs/incidents/2026-09-21-spend-log-cleanup-not-running/incident-report.md` |

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
| Endpoint architecture | `docs/custom-providers/LITELLM_ENDPOINT_ARCHITECTURE.md` |
| HAMSA / INCEPTION / OMNIVOICE research & audits | `docs/custom-providers/HAMSA_*.md`, `INCEPTION_*.md`, `OMNIVOICE_*.md` |

## Usage Guides

| Doc | Location |
|-----|----------|
| Gateway guide (Hamsa TTS/LLM/voice/STT) | `docs/usage-guides/GATEWAY_GUIDE.md` |
| Reasoning control guide | `docs/usage-guides/REASONING_CONTROL_GUIDE.md` |
| HAMSA STT & TTS guide | `docs/usage-guides/HAMSA_STT_TTS_GUIDE.md` |
| Hamsa TTS New `/v1` guide | `docs/usage-guides/HAMSA-TTS-NEW-V1-GUIDE.md` |
| Inception TTS/STT guide | `docs/usage-guides/INCEPTION_TTS_STT_GUIDE.md` |
| OmniVoice TTS guide | `docs/usage-guides/OMNIVOICE_TTS_GUIDE.md` |
| VibeVoice ASR guide | `docs/usage-guides/VIBEVOICE_ASR_GUIDE.md` |
| Qwen image generation & editing guide | `examples/usage/image-generation/README.md` (included into `docs/usage-guides/QWEN_IMAGE_GUIDE.md`) |
| Qwen vision guide | `docs/usage-guides/QWEN_VISION_GUIDE.md` |

### Runnable usage harnesses

| Harness | Location |
|---------|----------|
| Qwen image generation + editing (36 cases, writes `request.sh` / `response.json` / `result.txt` / `output-N.png` per case) | `examples/usage/image-generation/test_image_endpoints.sh` |
| Single-image generation with an editable prompt | `examples/usage/image-generation/gen_image.sh` |
| Input images used by the image harness | `examples/usage/image-generation/inputs/` |
| HAMSA STT WebSocket test page | `examples/usage/hamsa-stt/hamsa-stt-realtime-test-ws.html` |
| OpenRouter `/api/v1/models` demo | `examples/usage/openrouter/demo_openrouter_models.py` |

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
| Analytics reads -> prisma engine OOM logic map | `docs/performance/analytics-reads-prisma-engine-oom-LOGIC-MAP.md` |
| Prisma OOM live-data evidence | `docs/performance/live-data/` (README indexes the artifacts) |
| Retention flags + OOM logic map v2 | `docs/performance/retention-flags-and-oom-LOGIC-MAP-v2.md` |
| Spend logs + RAM logic map | `docs/performance/spend-logs-ram-and-model-performance-LOGIC-MAP.md` |
| Routing-strategy sync-task leak OOM logic map | `docs/performance/routing-strategy-sync-task-leak-oom-LOGIC-MAP.md` |

## Custom routes

| Doc | Location |
|-----|----------|
| Clone route logic map | `docs/custom-routes-plans/CLONE-LOGIC-MAP.md` |
| Missing-routes VSA plan | `docs/custom-routes-plans/VSA-PLAN.md` |

## Session identity

| Doc | Location |
|-----|----------|
| Session-id resolver implementation plan | `docs/session-identity/IMPLEMENTATION-PLAN.md` |

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

## Reports

Generated / exported reports (data snapshots for use cases). Regenerate rather
than hand-edit; each file states its generation date.

| Doc | Location |
|-----|----------|
| ADEOGPT model performance (7d) | `docs/reports/ADEOGPT-MODEL-PERFORMANCE.md` |

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