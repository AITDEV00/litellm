# Docs Map

> **The dev LiteLLM gateway has no ingress. `kubectl port-forward` is how you test it.**
>
> `litellm-proxy-dev` is not exposed outside the cluster, and `litellm.ecouncil.ae` fronts prod
> plus the dev broker's gRPC tunnel path, not the dev gateway. That is not a blocker: forward, for
> example `kubectl -n adeo-litellm port-forward svc/litellm-proxy-dev 14001:4000`, and re-run the
> forward after a dev rollout. Test against that forward, never the public host, because a proof
> described as running through "the public ingress" reached prod, not the dev gateway.

Where the documentation family owners live, so you can find an existing doc quickly. The [complete catalog](COMPLETE-DOC-CATALOG.md) supplies every filename and relative path

Start at [index.md](index.md) for task-oriented routing. Read [document-status.md](document-status.md) before acting on older design files or deployment measurements, and use [research-index.md](research-index.md) to locate plans, implementation logic maps, audits, and live evidence by concern. [AGENTS.md](AGENTS.md) defines the ownership and update contract for agents editing this documentation tree

The [complete documentation catalog](COMPLETE-DOC-CATALOG.md) lists every page in this tree, including generated pages. This curated map covers each topic directory and its important files

Use the trust labels in the owning indexes. "Current reference" means behavior is checked against its owning code or schema and still depends on current configuration. "Dated deployment observation" and "point-in-time evidence" do not claim current live-state facts. "Plan" is not a completed implementation

## Physical ownership families

`mkdocs.yml` organizes pages for readers, while physical directories identify component ownership. Keep durable behavior, plans, and evidence near their component; trust labels distinguish current references from historical work

| Family | Contents |
|---|---|
| `docs/platform/` | Gateway/controller components, discovery, pricing, OICM status, OpenRouter, and custom feature origins |
| `docs/administration/` | Admin API reference and generated schemas, team/access grants, identity, RBAC, SSO, and cache invalidation |
| `docs/providers/` | Provider implementation and diagnosis, custom routes, and maintainer design |
| `docs/mcp/` | MCP runtime, topology, guardrails, OpenCode, and the source broker's current record, decisions, measurements, transport, and adjacent history |
| `docs/distribution/` | Client and source-agent artifact delivery, storage, publisher API, release safety, and plans |
| `docs/guides/` | User-facing model/voice guides and MCP client operation; implementation research stays with the provider or MCP component |
| `docs/operations/` | Deployment, credentials, TLS assets, runbooks, incidents, rate limiting, performance, reports, and evidence artifacts |
| `docs/engineering/` | Architecture, dashboard workflows, session identity, persona drift, and reusable analysis techniques |

## This site (mkdocs)

| Page | Purpose |
|------|---------|
| `docs/index.md` | Task and symptom entry point |
| `docs/AGENTS.md` | Agent-read discovery and documentation maintenance contract |
| `docs/document-status.md` | Trust levels, ownership layout, and published-doc update requirements |
| `docs/research-index.md` | Plans, dated investigations, API mapping, audits and measurement index |
| `docs/structure.md` | Full directory map |
| `docs/operations/credentials.md` | **Master key & password contract + rotation runbook + ⚠️ SALT KEY (DO NOT TOUCH) ⚠️** |
| `docs/operations/deployment.md` | Apply / rollout / cluster access |
| `docs/operations/Makefile-reference.md` | Makefile variables, primitives, and compositions |
| `docs/operations/runbooks/postgres-backup-and-recovery.md` | Postgres backup architecture, the org CNPG+MinIO convention, snapshot retirement |
| `docs/operations/runbooks/spend-view-legacy-repair.md` | One-shot operator repair for foreign-owned spend views (2026-11-24 dashboard 500s) |
| `docs/platform/components/*.md` | Per-component navigation |

## Discovery Controller

| Doc | Location |
|-----|----------|
| Controller dev docs + env vars | `controller/README.md` |
| Controller overview | `docs/platform/discovery-controller/README.md` |
| Logic map + code smell audit | `docs/platform/discovery-controller/logic-map-and-code-smell-audit.md` |
| Pricing logic map | `docs/platform/model-pricing/PRICING-LOGIC-MAP.md` |
| Pricing matching plan | `docs/platform/model-pricing/PRICING-MATCHING-PLAN.md` |

## OICM Status

| Doc | Location |
|-----|----------|
| Deployment-status feasibility | `docs/platform/oicm-status/OICM-STATUS-FEASIBILITY.md` |
| Question-by-question answers | `docs/platform/oicm-status/FEASIBILITY-ANSWERS.md` |
| Implementation checklist | `docs/platform/oicm-status/IMPLEMENTATION-CHECKLIST.md` |
| Status persistence design | `docs/platform/oicm-status/DESIGN-STATUS-PERSISTENCE.md` |
| Proposed manual routing override | `docs/platform/oicm-status/DESIGN-MANUAL-ROUTING-OVERRIDE.md` |
| Progress & open work register | `docs/platform/oicm-status/PROGRESS-AND-PAUSED-WORK.md` |
| Health-table change report | `docs/platform/oicm-status/CHANGE-REPORT-2026-10-06-health-table.md` |
| Health-table logic map | `docs/platform/oicm-status/LOGIC-MAP-2026-10-07-health-table.md` |
| Controller code-smell pass | `docs/platform/oicm-status/CODE-SMELL-PASS-2026-10-06.md` |
| Live OICM status REST API catalog | `docs/platform/oicm-status/OICM-API-CATALOG.md` |
| `/endpoints` consumer logic map | `docs/platform/openrouter/LOGIC-MAP-2026-10-07-endpoints-consumer.md` |
| litellm-to-`/endpoints` field mapping | `docs/platform/openrouter/MAPPING-litellm-to-endpoints.md` |
| usage/telemetry sources + accuracy audit | `docs/platform/openrouter/MAPPING-usage-metrics.md` |
| OpenRouter-compatible design | `docs/platform/openrouter/litellm_openrouter_models_design.md` |
| Write-access blocker (resolved) | `docs/platform/oicm-status/oicm-write-access-blocked.md` |

## Incidents

| Doc | Location |
|-----|----------|
| Postgres disk-full (2026-09-08) | `docs/operations/incidents/2026-09-08-postgres-disk-full/` (incident report, executive summary, resolutions) |
| Kimi-K3 mid-stream stop (2026-09-21) | `docs/operations/incidents/2026-09-21-kimi-k3-midstream-stop/incident-report.md` |
| Spend-log cleanup not running (2026-09-21) | `docs/operations/incidents/2026-09-21-spend-log-cleanup-not-running/incident-report.md` |
| Gateway token-limits misreported (2026-11-03) | `docs/operations/incidents/2026-11-03-gateway-token-limits-misreported/incident-report.md` |
| Gateway vision capability undeclared (2026-11-03) | `docs/operations/incidents/2026-11-03-gateway-vision-capability-undeclared/incident-report.md` |

## Admin API

| Doc | Location |
|-----|----------|
| Full admin REST API guide | `docs/administration/admin-api/LITELLM-ADMIN-REST-API.md` |
| Administration capability map and scope caveats | `docs/administration/admin-api/ADMIN-CAPABILITY-MAP.md` |
| Teams, access groups, MCP and semantic tool grants playbook | `docs/administration/admin-api/TEAM-AND-ACCESS-GROUP-PLAYBOOK.md` |
| Athena read-only key guide | `docs/administration/admin-api/ATHENA-READ-ONLY-KEY-GUIDE.md` |
| Complete OpenAPI method register, linked to generated family pages | `docs/administration/admin-api/OPERATION-REGISTER.md`, `docs/administration/admin-api/operations/` (generated) |

## Identity and access control

| Doc | Location |
|-----|----------|
| Delegated SSO model/MCP calls and proxy-admin provisioning | `docs/administration/access-control/sso-application-integration.md` |
| MCP permission layers and scope | `docs/administration/access-control/permissions-and-scope.md` |
| RBAC, roles, budgets, and authorization granularity | `docs/administration/access-control/rbac-and-full-potential.md` |
| Key reassignment and historical spend ownership | `docs/administration/access-control/key-reassignment.md` |
| SSO versus application authentication | `docs/administration/access-control/sso-vs-app-auth.md` |
| Temporary delegated-key pattern | `docs/administration/access-control/sso-temporary-key-pattern.md` |
| SAML airgap research and dev SSO deployment evidence | `docs/administration/access-control/microsoft-saml-airgap.md`, `docs/administration/access-control/dev-microsoft-sso-deployed.md` (dated) |

## MCP architecture and source broker

| Doc | Location |
|-----|----------|
| Current component record | `docs/mcp/source-broker/running-system/README.md` |
| Architecture and research reading index | `docs/mcp/architecture-research-index.md` |
| Topology, runtime, client installation, production cluster, and file map | `docs/mcp/` |
| MCP input/output guardrailing | `docs/mcp/guardrailing.md` |
| OpenCode plan and dated pilot evidence | `docs/mcp/opencode-sso-oauth-skills-pilot.md`, `docs/mcp/opencode-pilot-evidence.md` |

## Rate limiting / priority

| Doc | Location |
|-----|----------|
| HTB README | `docs/operations/rate-limiting/HTB-README.md` |
| Priority bridge feasibility | `docs/operations/rate-limiting/PRIORITY-BRIDGE-FEASIBILITY.md` |
| Priority logic map | `docs/operations/rate-limiting/PRIORITY-LOGIC-MAP.md` |
| Priority queue evidence/results | `docs/operations/rate-limiting/PRIORITY-QUEUE-EVIDENCE.md`, `PRIORITY-QUEUE-RESULTS-SUMMARY.md` |
| Priority behaviour | `docs/operations/rate-limiting/priority_behaviour.md` |
| Executive summary | `docs/operations/rate-limiting/HTB-EXECUTIVE-SUMMARY.md` |

## Custom providers

| Doc | Location |
|-----|----------|
| Custom providers README | `docs/providers/custom-providers/README.md` |
| pyannote diarization provider plan | `docs/providers/custom-providers/PYANNOTE_DIARIZATION_PROVIDER_PLAN.md` |
| pyannote speaker embeddings plan | `docs/providers/custom-providers/PYANNOTE_SPEAKER_EMBEDDINGS_PLAN.md` |
| Nemotron native transcription, translation, documents, cache, and SSE implementation plan | `docs/providers/custom-providers/NEMOTRON_NATIVE_AUDIO_IMPLEMENTATION_PLAN.md` |
| Drama 3 gateway gaps (ownership + custom-provider proposal) | `docs/providers/custom-providers/FISHAUDIO_DRAMA3_GATEWAY_GAPS.md` |
| Endpoint architecture | `docs/providers/custom-providers/LITELLM_ENDPOINT_ARCHITECTURE.md` |
| HAMSA research / TTS behavior | `docs/providers/custom-providers/HAMSA_RESEARCH.md`, `HAMSA_TTS_BEHAVIOR.md` |
| OmniVoice logic maps + audit + test methods | `docs/providers/custom-providers/OMNIVOICE_LOGIC_MAPPING.md`, `OMNIVOICE_LOGIC_MAP_v2.md`, `OMNIVOICE_POST_BUILD_LOGIC_MAP.md`, `OMNIVOICE_EXTENSIBILITY_AUDIT.md`, `EXAMPLE_OMNIVOICE_TEST_METHODS.md` |
| Inception audit / pod parity | `docs/providers/custom-providers/inception-hamsa-audit.md`, `inception-gateway-pod-parity.md` |
| Gateway field-drop investigation | `docs/providers/custom-providers/gateway-field-drop-investigation.md` |
| Drama 3 registration diagnosis (mode + field drop) | `docs/providers/custom-providers/FISHAUDIO_DRAMA3_REGISTRATION_DIAGNOSIS.md` |
| Drama 3 robust fix design (pass-through + declared mode) | `docs/providers/custom-providers/FISHAUDIO_DRAMA3_ROBUST_FIX_DESIGN.md` |
| Capability registration by probe (superseded by review 2) | `docs/providers/custom-providers/CAPABILITY_REGISTRATION_BY_PROBE.md` |
| Capability registration review 2 (task tier + probes as fallback) | `docs/providers/custom-providers/CAPABILITY_REGISTRATION_REVIEW2.md` |

## Usage Guides

| Doc | Location |
|-----|----------|
| Gateway guide (Hamsa TTS/LLM/voice/STT) | `docs/guides/models-and-voice/GATEWAY_GUIDE.md` |
| Reasoning control guide | `docs/guides/models-and-voice/REASONING_CONTROL_GUIDE.md` |
| HAMSA STT guide | `docs/guides/models-and-voice/HAMSA_STT_GUIDE.md` |
| Hamsa TTS New `/v1` guide | `docs/guides/models-and-voice/HAMSA-TTS-NEW-V1-GUIDE.md` |
| Inception TTS/STT guide | `docs/guides/models-and-voice/INCEPTION_TTS_STT_GUIDE.md` |
| OmniVoice TTS guide | `docs/guides/models-and-voice/OMNIVOICE_TTS_GUIDE.md` |
| Fish Audio Drama 3 TTS guide | `docs/guides/models-and-voice/FISHAUDIO_DRAMA3_TTS_GUIDE.md` |
| VibeVoice ASR guide | `docs/guides/models-and-voice/VIBEVOICE_ASR_GUIDE.md` |
| Nemotron Transcribe guide (including gateway caveats and concurrency) | `docs/guides/models-and-voice/NEMOTRON_TRANSCRIBE_GUIDE.md` |
| Pyannote diarization guide | `docs/guides/models-and-voice/PYANNOTE_DIARIZATION_GUIDE.md` |
| Qwen image generation & editing guide | `examples/usage/image-generation/README.md` (included into `docs/guides/models-and-voice/QWEN_IMAGE_GUIDE.md`) |
| Qwen vision guide | `docs/guides/models-and-voice/QWEN_VISION_GUIDE.md` |
| MCP source agent guide (install, log in, register sources, run the agent, approve, call tools, revoke) | `docs/guides/mcp/MCP_SOURCE_AGENT_GUIDE.md` |

### Runnable usage harnesses

| Harness | Location |
|---------|----------|
| Qwen image generation + editing (36 cases, writes `request.sh` / `response.json` / `result.txt` / `output-N.png` per case) | `examples/usage/image-generation/test_image_endpoints.sh` |
| Single-image generation with an editable prompt | `examples/usage/image-generation/gen_image.sh` |
| Input images used by the image harness | `examples/usage/image-generation/inputs/` |
| HAMSA STT WebSocket test page | `examples/usage/hamsa-stt/hamsa-stt-realtime-test-ws.html` |
| Nemotron functional route/timestamp/chat validation + unique-WAV burst | `examples/usage/nemotron-transcribe/test_gateway.py`, `benchmark_gateway.py` |
| Nemotron complete streaming-chat text reconstruction | `examples/usage/nemotron-transcribe/transcribe_chat_stream.py` |
| OpenRouter `/api/v1/models` demo | `examples/usage/openrouter/demo_openrouter_models.py` |

## Dashboard / frontend

| Doc | Location |
|-----|----------|
| Frontend analysis process | `docs/engineering/dashboard/FRONTEND-ANALYSIS-PROCESS.md` |
| UI lint + change process | `docs/engineering/dashboard/UI-LINT-AND-CHANGE-PROCESS.md` |
| Observability plan | `docs/engineering/dashboard/OBSERVABILITY-IMPLEMENTATION-PLAN.md` |

## Performance

| Doc | Location |
|-----|----------|
| Executive summary | `docs/operations/performance/executive-summary.md` |
| Before / after | `docs/operations/performance/before-optimization.md`, `after-optimization.md` |
| Perf recovery / session notes | `docs/operations/performance/model-performance-perf-recovery.md` |
| Analytics reads -> prisma engine OOM logic map | `docs/operations/performance/analytics-reads-prisma-engine-oom-LOGIC-MAP.md` |
| Spend-logs UI enrichment window-bound fix | `docs/operations/performance/spend-logs-ui-enrichment-window-fix-LOGIC-MAP.md` |
| Prisma OOM live-data evidence | `docs/operations/performance/live-data/` (README indexes the artifacts) |
| Retention flags + OOM logic map v2 | `docs/operations/performance/retention-flags-and-oom-LOGIC-MAP-v2.md` |
| Spend logs + RAM logic map | `docs/operations/performance/spend-logs-ram-and-model-performance-LOGIC-MAP.md` |
| Routing-strategy sync-task leak OOM logic map | `docs/operations/performance/routing-strategy-sync-task-leak-oom-LOGIC-MAP.md` |
| Spend-view repair plan, dev worker prompt, prod runbook | `docs/operations/performance/spend-view-repair-plan.md` |

## Custom routes

| Doc | Location |
|-----|----------|
| Clone route logic map | `docs/providers/custom-routes-plans/CLONE-LOGIC-MAP.md` |
| Missing-routes VSA plan | `docs/providers/custom-routes-plans/VSA-PLAN.md` |

## Session identity

| Doc | Location |
|-----|----------|
| Session-id resolver implementation plan | `docs/engineering/session-identity/IMPLEMENTATION-PLAN.md` |

## Persona drift

| Doc | Location |
|-----|----------|
| Persona / language-confusion drift spike note | `docs/engineering/persona-drift/SPIKE.md` |

## Client distribution (same-origin LiteLLM CLI install)

| Doc | Location |
|-----|----------|
| Design plan (start here) | `docs/distribution/client-distribution/PLAN.md` |
| Implementation plan (phase by phase) | `docs/distribution/client-distribution/implementation.md` |
| Architecture | `docs/distribution/client-distribution/architecture.md` |
| Mirror content (uv / wheelhouse / Python) | `docs/distribution/client-distribution/mirror-content.md` |
| Served install script design | `docs/distribution/client-distribution/installer-script.md` |
| Supported platform matrix | `docs/distribution/client-distribution/platform-matrix.md` |
| Deployment (ingress, artifact pod, sync job) | `docs/distribution/client-distribution/deployment.md` |
| RustFS backend (object storage, shared S3 client) | `docs/distribution/client-distribution/rustfs.md` |
| Source-connector release safeguards (partially implemented; remaining evidence gaps recorded) | `docs/distribution/client-distribution/source-agent-releases.md` |
| Admin sync API (push/upsert/delete artifacts) | `docs/distribution/client-distribution/admin-sync.md` |
| Security (threat model, checksums) | `docs/distribution/client-distribution/security.md` |
| Rollout plan | `docs/distribution/client-distribution/rollout.md` |
| Open questions | `docs/distribution/client-distribution/open-questions.md` |

## Techniques

| Doc | Location |
|-----|----------|
| Logic mapping technique | `docs/engineering/techniques/logic_mapping_technique.md` |
| Code smell detection technique | `docs/engineering/techniques/code_smell_detection_technique.md` |
| Upstream pull & branch merge | `docs/engineering/techniques/upstream_merge_technique.md` |
| Debug pod technique | `docs/engineering/techniques/debug_pod_technique.md` |

## OICM custom code

| Doc | Location |
|-----|----------|
| Additional features — origin (where OICM custom code lives, by slice) | `docs/platform/additional-features-origin.md` |
| Vertical slice code layout technique (folders by behavior, agent-readable) | `docs/engineering/techniques/vertical_slice_technique.md` |
| Drop-detection wiring tests | `tests/unit/proxy/test_oicm_drop_detection.py` |
| Live smoke suite (post-merge, against a deployed gateway) | `tests/smoke/AGENTS.md` |

## Reports

Generated / exported reports (data snapshots for use cases). Regenerate rather
than hand-edit; each file states its generation date.

| Doc | Location |
|-----|----------|
| ADEOGPT model performance (7d) | `docs/operations/reports/ADEOGPT-MODEL-PERFORMANCE.md` |

## Runbooks

| Doc | Location |
|-----|----------|
| MkDocs setup | `docs/operations/runbooks/MKDOCS-SETUP.md` |
| Datasource local validation | `docs/operations/runbooks/DATASOURCE-LOCAL-VALIDATION.md` |

## TLS / Certificates

| Doc | Location |
|-----|----------|
| Apply wildcard cert guideline | `docs/operations/ssl/CERT-GUIDELINE.md` |
| Serve litellm.ecouncil.ae runbook | `docs/operations/ssl/LITELLM-ECOUNCIL-RUNBOOK.md` |
| TLS secret scripts | `docs/operations/ssl/create-tls-secret*.sh` |

## Architecture

| Doc | Location |
|-----|----------|
| Integration-layer implementation plan | `docs/engineering/architecture/IMPLEMENTATION_PLAN.md` |
| Secondary gateway (edge mirror) design exploration | `docs/engineering/architecture/EDGE-MIRROR-DESIGN.md` |
| Agent platform fit assessment: the director's one-layer vision mapped to the implemented layer, with gaps and build order | `docs/engineering/architecture/AGENT-PLATFORM-FIT-ASSESSMENT.md` |
| **MCP source broker: owning implementation record (start here)** | `docs/mcp/source-broker/running-system/README.md` |
| MCP source broker: slice index, maps each question to the folder that answers it | `docs/mcp/source-broker/README.md` |
| MCP source broker: measurements (concurrency batches, proven and inconclusive results) | `docs/mcp/source-broker/measurements/go-driver-concurrency-2026-11-18.md` |
| MCP source lifecycle and pooling research questions (mostly closed; §2 credential bootstrap shipped as BYOM self-service) | `docs/mcp/source-broker/decisions/approaches/lifecycle-and-pooling-questions.md` |
| BYOM self-service registration design, shipped 2026-11-18: reasoning plus where the code departed from it | `docs/mcp/source-broker/decisions/approaches/byom-self-service.md` |
| Connector-owned enrollment requirement, recorded 2026-11-18: user identity not a team key, and the connector owns the registration calls (open blocker) | `docs/mcp/source-broker/decisions/approaches/connector-owned-enrollment.md` |
| Multi-replica broker scaling: UNFINALIZED debate record with positions, corrections, extension seam, and resume conditions | `docs/mcp/source-broker/decisions/approaches/multi-replica-scaling.md` |
| Nginx reverse-tunnel pilot: the lab flow that preceded the broker | `docs/mcp/source-broker/transport/pilot/nginx-reverse-tunnel.md` |
| MCP source broker: superseded design proposal, kept for decision history only | `docs/mcp/source-broker/history/superseded-design-proposal.md` |
| Unified MCP implementation guide: gateway-owned tool namespace and harness flow (different behavior, filed under history) | `docs/mcp/source-broker/history/app-side-mcp-scope/gateway-mcp-execution-guide.md` |

## Cache invalidation

| Doc | Location |
|-----|----------|
| Cache invalidation fix testing | `docs/administration/cache-invalidation/CACHE_INVALIDATION_FIX_TESTING.md` |