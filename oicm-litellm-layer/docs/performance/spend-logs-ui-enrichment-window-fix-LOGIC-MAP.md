# Spend-Logs UI Session Enrichment — Window-Bound Fix — Logic Map

> The `/spend/logs/ui` "Request Logs" page appeared to load forever (~14s for
> the last hour) after session-identity shipped. This is the fix that was
> already called out but never implemented in
> [analytics-reads-prisma-engine-oom-LOGIC-MAP.md](analytics-reads-prisma-engine-oom-LOGIC-MAP.md)
> §3.4 ("`_view_spend_logs` session enrichment — GROUP BY over raw SpendLogs
> **on every logs page load**"). Applied 2026-10-13, commit `d0b68e2465`.

---

## 0. One-paragraph answer

`ui_view_spend_logs` enriches each page row with per-session aggregates via
`_build_ui_spend_logs_response`. That enrichment query had **no `startTime`
filter** — it aggregated every `session_id` on the page across **all** of
`LiteLLM_SpendLogs`. With daily partitioning, Postgres had to probe every
partition per session (plus a `LEFT JOIN LATERAL` per session for distinct
models). Before session-identity, most rows had `session_id = NULL`, so the
enrichment was trivial or skipped. Once session-identity stamped **every** row
with a `session_id` (1.38M rows in 7 days on prod), a 50-row page probed all
**77** partitions per session and took ~14s. The fix threads the listing's own
date window into the enrichment query so partition pruning drops the partitions
outside it. Prod last-hour page: **14s -> 0.40s (35x)**.

---

## 1. Why it surfaced only after session-identity

The enrichment query is **stock upstream v1.102.0** — unchanged. It was always
unbounded, but it was never expensive in practice because:

- Without session-identity, almost no rows carried a `session_id`, so
  `session_ids` collected in `_build_ui_spend_logs_response` was small or empty
  and the `GROUP BY (session_id, api_key)` had little to aggregate.
- The affinity feature (`DeploymentAffinityCheck`) already pinned explicit
  client session ids, but those were a minority of traffic.

Session-identity flipped the denominator: 100% of the last 7 days' rows
(1,381,828) now carry a `session_id`, and those sessions are long-lived (TTL
86400s), so each one spans many daily partitions. The previously-harmless query
became a per-page full-table fan-out.

This is the same class of hazard the sibling `END_USER_FACET_SCAN_CAP` bound
(commit `229a288d39`) addressed for the `global_view_all_end_users` DISTINCT —
an unbounded analytics read over the raw table — but the enrichment query was
left out of that fix.

---

## 2. The exact fault

`_build_ui_spend_logs_response`
(`litellm/proxy/spend_tracking/spend_management_endpoints.py`) runs, for the
page's session ids:

```sql
SELECT s.*, COALESCE(m.session_models, ...) AS session_models
FROM (
    SELECT session_id, api_key, COUNT(*) AS session_total_count, ... 
    FROM "LiteLLM_SpendLogs"
    WHERE session_id = ANY($1::text[])
      AND api_key = ANY($2::text[])
    -- NO startTime bound: probes every partition
    GROUP BY session_id, api_key
) s
LEFT JOIN LATERAL (
    SELECT ARRAY_AGG(d.model ORDER BY d.model) AS session_models
    FROM (
        SELECT DISTINCT LEFT(model, $3::int) AS model
        FROM "LiteLLM_SpendLogs"
        WHERE session_id = s.session_id AND api_key = s.api_key
          AND model IS NOT NULL AND model <> ''
        -- NO startTime bound here either
        ORDER BY 1 LIMIT $4::int
    ) d
) m ON TRUE
```

Measured on prod (2026-10-13):

| Signal | Value |
|---|---|
| `/spend/logs/ui` last 1h, page_size=50 | **13.997s** |
| `/spend/logs/ui` last 1h, page_size=1 | 0.130s |
| `/spend/logs/v2/` same window (no enrichment) | 0.405s |
| Rows with `session_id`, last 7d | 1,381,828 (100%) |
| Partitions on `LiteLLM_SpendLogs` | 77 |
| Partitions scanned per session (no window) | all 77 |

The `page_size=1` vs `page_size=50` contrast proves the cost is **per enriched
row** (the aggregate + LATERAL per session), not the page COUNT and not query
planning.

---

## 3. The fix

Commit `d0b68e2465`. Pass the listing's date window
(`start_time_utc` / `end_time_utc`) into `_build_ui_spend_logs_response` and add
matching `startTime` bounds to **both** the aggregate and the LATERAL subquery
(`window_condition`). The window is optional: the request-id-lookup path has no
date window and keeps the prior unbounded behavior. Both the flat listing and
the session-grouped path (`_ui_session_grouped_spend_logs`) forward the window.

Result: partition pruning drops every partition outside the window. On dev (30
partitions) the per-session scan went **30 -> 2** partitions; on prod (77
partitions) the effect is larger.

The frontend only needs to know whether a session is expandable (multi-call),
so bounding the aggregate to the visible window is semantically correct for the
UI — the "total" for the page is unchanged (`total` comes from a separate,
already-bounded count query).

---

## 4. Verification

- **SQL**: `EXPLAIN` of the enrichment aggregate — no window scans all
  partitions; with the window bound it scans only the window's partitions
  (dev: 30 -> 2).
- **Endpoint (prod, after rollout)**:

| Window | Before | After |
|---|---|---|
| last 1 hour, page_size=50 | 14.0s | 0.40s |
| last 6 hours | — | 0.46s |
| last 24 hours | — | 0.53s |
| session-grouped view | — | 0.25s |

- Enrichment still populates correctly (`session_total_count` present); no
  `Error in ui_view_spend_logs` or prisma errors in prod logs after rollout.

---

## 5. Follow-ups

- **Upstream candidate.** Any upstream deployment with `use_spend_logs_partitioning`
  AND a meaningful fraction of rows carrying `session_id` hits the same wall.
  The window-bound is a safe, behavior-preserving improvement worth sending
  upstream.
- **Same hazard, still open** (from the OOM map §3): the GROUPING SETS
  aggregate (§3.1) and the lazily-created heavy-query engine (§3.5) are
  separate unbounded-read drivers not addressed by this fix. See
  [analytics-reads-prisma-engine-oom-LOGIC-MAP.md](analytics-reads-prisma-engine-oom-LOGIC-MAP.md)
  §5 for those.
