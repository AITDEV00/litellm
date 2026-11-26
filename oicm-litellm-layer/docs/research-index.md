# Plans, research, and point-in-time evidence

Start with this index when a filename looks like a design study, implementation plan, measurement, audit, or diagnosis rather than an operating guide. The owning component remains the primary boundary, but one central index prevents planet-wide searches across an incident, a completed plan, and a future proposal

Check [documentation trust](document-status.md) before executing anything. A plan's acceptance criteria are not a claim that its feature was implemented, and a live-state observation remains tied to its observation date. Code and deployed configuration win over research

## Most referenced records

| Concern | Owning start | Secondary research/evidence |
|---|---|---|
| MCP source broker and OpenCode | [Running system](mcp/source-broker/running-system/README.md) | [MCP research](mcp/architecture-research-index.md), [pilot evidence](mcp/opencode-pilot-evidence.md) |
| Access grants, team membership, SSO | [Access control](administration/access-control/README.md) | [SAML airgap research](administration/access-control/microsoft-saml-airgap.md), [dev SSO deployment record](administration/access-control/dev-microsoft-sso-deployed.md) |
| Gateway OOMs and slow analytics | [Performance evidence index](#performance-and-spend-analytics) | [Dated incident reports](#incidents) |
| OICM registrations and model status | [OICM status](#oicm-platform-and-model-registration) and [OpenRouter](#openrouter-model-discovery) | Controller code plus current API catalog |
| UI planning, custom routes, voice providers | [Custom providers](#providers-and-routes) | Their guides and provider README |
| CLI/source connector distribution | [Current client distribution plan](client-distribution/PLAN.md) | [Completed design history](client-distribution/history/lite-cli-PLAN.md) |

## Performance and spend analytics

| Page | Subject | Trust |
|---|---|---|
| [Executive summary](operations/performance/executive-summary.md) | Gateway performance, retained measurements | Dated performance record |
| [Before and after optimization](operations/performance/before-after-correct-configuration.md) | Configured versus unconfigured comparative measurements | Point-in-time evidence |
| [Before optimization](operations/performance/before-optimization.md) | Earlier resource/performance configuration | Historical observation |
| [After optimization](operations/performance/after-optimization.md) | Optimized configuration findings | Dated observation |
| [Analytics reads and Prisma-engine OOM](operations/performance/analytics-reads-prisma-engine-oom-LOGIC-MAP.md) | Dashboard-read database bloat and OOM analysis | Investigation/solution logic map |
| [Routing-strategy sync leak OOM](operations/performance/routing-strategy-sync-task-leak-oom-LOGIC-MAP.md) | Routing strategy task leak investigation | Investigation logic map |
| [Spend-log retention flags and OOM](operations/performance/retention-flags-and-oom-LOGIC-MAP-v2.md) | Retention behavior and backend resource pressure, version 2 | Investigation logic map, recheck current flags |
| [Spend logs, RAM, and performance reads](operations/performance/spend-logs-ram-and-model-performance-LOGIC-MAP.md) | Backend spend-log memory and performance | Investigation logic map |
| [Spend-log UI window fix](operations/performance/spend-logs-ui-enrichment-window-fix-LOGIC-MAP.md) | UI enrichment query-bound correction | Investigation logic map |
| [Spend dashboard repair](operations/performance/spend-view-repair-plan.md) | Dev and prod repair procedure | Operation plan; verify the deployed state before reuse |
| [Model performance session recovery](operations/performance/model-performance-perf-recovery.md) | Session handoff for concurrent performance work | Historical session notes, not a current repo status page |
| [Model performance 30d/YTD read fix](operations/performance/model-performance-30d-read-fix-LOGIC-MAP.md) | Slow analytics fix | Research implementation analysis |
| [Model performance 30d read audit](operations/performance/model-performance-30d-read-fix-CODESMELL-AUDIT.md) | Read-path code quality review | Historical audit |
| [Model performance optimization](operations/performance/model-performance-optimization-LOGIC-MAP.md) | Optimization route and tradeoffs | Historical implementation analysis |
| [Live Prisma evidence](operations/performance/live-data/README.md) | Artifact-backed measurements for the Prisma OOM investigation | Point-in-time evidence |

## OICM platform and model registration

The OICM status folder mixes a documented implementation path with a live component API. Treat source and current registration behavior as authoritative; older plans can contain resolved blockers

| Topic | Documents | Trust |
|---|---|---|
| Feasibility and answers | [Study](platform/oicm-status/OICM-STATUS-FEASIBILITY.md), [Q&A answers](platform/oicm-status/FEASIBILITY-ANSWERS.md) | Historical design research |
| Implementation plan and finished/not-finished work | [Implementation checklist](platform/oicm-status/IMPLEMENTATION-CHECKLIST.md), [Progress and paused work](platform/oicm-status/PROGRESS-AND-PAUSED-WORK.md) | Historical progress; verify any resume against code |
| Supported status persistence | [Status design](platform/oicm-status/DESIGN-STATUS-PERSISTENCE.md), [Health-table logic map](platform/oicm-status/LOGIC-MAP-2026-10-07-health-table.md) | Design and dated implementation map |
| Proposed manual routing override | [Override design](platform/oicm-status/DESIGN-MANUAL-ROUTING-OVERRIDE.md) | Proposal, not a claim of implemented overrides |
| Live OICM catalog | [OICM status REST API](platform/oicm-status/OICM-API-CATALOG.md) | Versioned API catalog, date recorded on page |
| Prior write-access block | [Write-access root-cause findings](platform/oicm-status/oicm-write-access-blocked.md) | Point-in-time investigation |
| Earlier controller changes and review | [Health-table change report](platform/oicm-status/CHANGE-REPORT-2026-10-06-health-table.md), [Code-smell pass](platform/oicm-status/CODE-SMELL-PASS-2026-10-06.md) | Historical audit/change record |

## OpenRouter model discovery

| Document | Use for | Trust |
|---|---|---|
| [Implemented-model design](platform/openrouter/litellm_openrouter_models_design.md) | OpenRouter-compatible model discovery, response mapping, and feature behavior | Reference; verify against source |
| [Implementation study](platform/openrouter/IMPLEMENTATION-STUDY.md) | Initial design reasoning and compatibility findings | Historical design record |
| [`/endpoints` consumer logic map](platform/openrouter/LOGIC-MAP-2026-10-07-endpoints-consumer.md) | Gateway-to-controller contract sequencing | Dated implementation map |
| [LiteLLM inputs to `/endpoints`](platform/openrouter/MAPPING-litellm-to-endpoints.md) | Input/registration transformation contract | Mechanism mapping |
| [Usage and telemetry mapping](platform/openrouter/MAPPING-usage-metrics.md) | Latency, throughput, and accuracy limitations | Mechanism mapping with measurement caveats |

## Architecture and session identity

[Integration-layer implementation plan](architecture/IMPLEMENTATION_PLAN.md), [edge-mirror design exploration](architecture/EDGE-MIRROR-DESIGN.md), [agent-platform fit assessment](architecture/AGENT-PLATFORM-FIT-ASSESSMENT.md), and [session identity plan](session-identity/IMPLEMENTATION-PLAN.md) are early or subject-specific plans. Their headings and status at the document location govern; do not assume an implementation plan has been completed because it has a detailed checklist

## Providers and routes

Provider behavior is current-user-facing reference in [Custom-provider reference](providers/custom-providers/README.md) and the [usage guides](docs-map.md#usage-guides) in the docs map. Treat detailed logic maps, audits, and capability plans as research evidence; link them with their owning guide rather than treating them as endpoint contracts

| Concern | Research record | Notes |
|---|---|---|
| OmniVoice routes | [Custom-route clone logic map](providers/custom-routes-plans/CLONE-LOGIC-MAP.md), [Missing-routes VSA plan](providers/custom-routes-plans/VSA-PLAN.md) | Custom-route implementation analysis and plan |
| Nemotron native audio | [Provider plan](providers/custom-providers/NEMOTRON_NATIVE_AUDIO_IMPLEMENTATION_PLAN.md) | Long design plan; actual provider behavior comes later from implementations |
| OmniVoice | [Logic mapping](providers/custom-providers/OMNIVOICE_LOGIC_MAPPING.md), [Map v2](providers/custom-providers/OMNIVOICE_LOGIC_MAP_v2.md), [Post-build logic map](providers/custom-providers/OMNIVOICE_POST_BUILD_LOGIC_MAP.md) | Version-specific design reviews; do not automatically apply to a later image |
| Provider extensibility and probes | [OmniVoice audit](providers/custom-providers/OMNIVOICE_EXTENSIBILITY_AUDIT.md), [Capability probe](providers/custom-providers/CAPABILITY_REGISTRATION_BY_PROBE.md) and [Review 2](providers/custom-providers/CAPABILITY_REGISTRATION_REVIEW2.md) | The document itself says review 2 demoted the probe-only approach to last-resort |
| HAMSA and inception evaluation | [HAMSA research](providers/custom-providers/HAMSA_RESEARCH.md), [Behavior](providers/custom-providers/HAMSA_TTS_BEHAVIOR.md), [Inception audit](providers/custom-providers/inception-hamsa-audit.md), [Pod parity](providers/custom-providers/inception-gateway-pod-parity.md) | Dated implementation and behavior evidence |
| Fish Audio registration | [Registration diagnosis](providers/custom-providers/FISHAUDIO_DRAMA3_REGISTRATION_DIAGNOSIS.md), [Gateway gaps](providers/custom-providers/FISHAUDIO_DRAMA3_GATEWAY_GAPS.md), [Robust fix design](providers/custom-providers/FISHAUDIO_DRAMA3_ROBUST_FIX_DESIGN.md) | Diagnosis and proposed fix; use the usage guide for supported invocation |

## HTB, priority, and diagnostics data

[HTB overview](operations/rate-limiting/HTB-README.md) and [Feasibility](operations/rate-limiting/PRIORITY-BRIDGE-FEASIBILITY.md) cover the rate-limit control design. [Priority logic map](operations/rate-limiting/PRIORITY-LOGIC-MAP.md), [Queue results](operations/rate-limiting/PRIORITY-QUEUE-RESULTS-SUMMARY.md), [Behaviour](operations/rate-limiting/priority_behaviour.md), and [Executive summary](operations/rate-limiting/HTB-EXECUTIVE-SUMMARY.md) are the associated investigation and measurement pages. [Live HTB evidence](operations/rate-limiting/live-data/README.md) retains exact artifacts for the Lua window-value test

Do not infer queue depth or rate limiting from an intended setting alone; check live Lua artifacts and request-path behavior

## Incidents

Incidents remain separate from component plans. Each contains the original findings and recovery evidence rather than a presumed permanent configuration:

- [2026-09-08 Postgres disk full](operations/incidents/2026-09-08-postgres-disk-full/incident-report.md), with its [resolutions](operations/incidents/2026-09-08-postgres-disk-full/resolutions.md)
- [2026-09-21 Mid-stream stop](operations/incidents/2026-09-21-kimi-k3-midstream-stop/incident-report.md)
- [2026-09-21 Spend-log cleanup not running](operations/incidents/2026-09-21-spend-log-cleanup-not-running/incident-report.md)
- [2026-11-03 Token-limits misreported](operations/incidents/2026-11-03-gateway-token-limits-misreported/incident-report.md)
- [2026-11-03 Undeclared deployed vision](operations/incidents/2026-11-03-gateway-vision-capability-undeclared/incident-report.md)
- [2026-11-18 Transcription latency](operations/incidents/2026-11-18-nemotron-gateway-transcription-latency/incident-report.md)

## Component source-broker decisions and earlier designs

[Source-broker slice index](mcp/source-broker/README.md) points to current record, live measurements, approaches, historical proposal and app-side MCP scope. The transport pilot and earlier design files are there for replication or provenance, not a standing exact deployment map

## Reusable methods

[Techniques](docs-map.md#techniques) lists procedure-level logic mapping, code-smell detection, debug-pod isolation, and upstream merges. These describe workflow, not component state. [MCP source-link audit](techniques/smell-audit-mcp-source-link.md) is the dated result of one such audit, so treat it separately from the reusable audit process
