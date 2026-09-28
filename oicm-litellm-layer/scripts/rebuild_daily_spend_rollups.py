"""Rebuild the LiteLLM_Daily*Spend rollup ``spend`` column from LiteLLM_SpendLogs.

Companion to backfill_hosted_vllm_spend.py. That script corrected the per-request
``spend`` in LiteLLM_SpendLogs; the proxy's own aggregation job does not reprocess
old rows, so the LiteLLM_Daily*Spend rollups keep the stale (undercharged) totals.
This recomputes each rollup's ``spend`` from the corrected source.

How it stays faithful to the proxy's aggregation (db_spend_update_writer.py):
  * same unique key: (entity_id, date, api_key, model, custom_llm_provider,
    mcp_namespaced_tool_name, endpoint)
  * date = "startTime"::date
  * endpoint = ROUTE_ENDPOINT_MAPPING[call_type] (replicated below)
  * only ``spend`` is rewritten; counters and *_savings_spend columns are untouched.

Scope is restricted to the affected window and the chat call types that were
undercharged, so unrelated rollups are not disturbed. A row is only rewritten when
the recomputed spend differs by more than --epsilon. Dry-run by default; --apply writes.

Run inside a proxy pod:
  kubectl exec -i -n adeo-litellm deploy/litellm-proxy-dev -- sh -c "python - --dsn '$DSN'" < scripts/rebuild_daily_spend_rollups.py
"""

from __future__ import annotations

import argparse
import sys
from typing import Final

import psycopg
from psycopg.rows import dict_row

# call_type -> endpoint, mirrored from litellm/proxy/route_llm_request.py
# ROUTE_ENDPOINT_MAPPING (only the entries we scope to).
ROUTE_ENDPOINT: Final = {
    "completion": "/chat/completions",
    "acompletion": "/chat/completions",
}

# entity -> (rollup table, entity-id column in BOTH source and rollup)
ENTITIES: Final = {
    "user": ("LiteLLM_DailyUserSpend", "user_id"),
    "team": ("LiteLLM_DailyTeamSpend", "team_id"),
    "org": ("LiteLLM_DailyOrganizationSpend", "organization_id"),
    "end_user": ("LiteLLM_DailyEndUserSpend", "end_user_id"),
    "agent": ("LiteLLM_DailyAgentSpend", "agent_id"),
}

# entity-id column in LiteLLM_SpendLogs for each rollup (they mostly match).
SOURCE_ID_COLUMN: Final = {
    "user": '"user"',
    "team": "team_id",
    "org": "organization_id",
    "end_user": "end_user",
    "agent": "agent_id",
}


def recompute_sql(rollup: str, source_id_col: str, entity_col: str) -> str:
    return f"""
    WITH src AS (
      SELECT
        {source_id_col}                                          AS entity_id,
        to_char("startTime", 'YYYY-MM-DD')                       AS date,
        COALESCE(api_key, '')                                    AS api_key,
        model                                                    AS model,
        custom_llm_provider                                      AS custom_llm_provider,
        COALESCE(mcp_namespaced_tool_name, '')                   AS mcp_namespaced_tool_name,
        %(endpoint)s                                             AS endpoint,
        SUM(spend)                                               AS spend
      FROM "LiteLLM_SpendLogs"
      WHERE custom_llm_provider = 'hosted_vllm'
        AND call_type = ANY(%(call_types)s)
        AND cache_hit IS DISTINCT FROM 'True'
        AND "startTime" >= %(since)s AND "startTime" < %(until)s
      GROUP BY 1, 2, 3, 4, 5, 6, 7
    )
    SELECT r.id, r.spend AS old_spend, COALESCE(src.spend, 0.0) AS new_spend
    FROM "{rollup}" r
    JOIN src
      ON r.{entity_col} = src.entity_id
     AND r.date = src.date
     AND r.api_key = src.api_key
     AND r.model = src.model
     AND r.custom_llm_provider = src.custom_llm_provider
     AND COALESCE(r.mcp_namespaced_tool_name, '') = src.mcp_namespaced_tool_name
     AND COALESCE(r.endpoint, '') = src.endpoint
    WHERE r.date >= to_char(%(since)s::timestamp, 'YYYY-MM-DD')
      AND r.date < to_char(%(until)s::timestamp, 'YYYY-MM-DD')
      AND abs(COALESCE(src.spend, 0.0) - r.spend) > %(epsilon)s
    """


UPDATE_SQL: Final = 'UPDATE "{rollup}" SET spend = %(new_spend)s, updated_at = now() WHERE id = %(id)s'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--since", default="2026-09-18")
    parser.add_argument("--until", default="2026-09-28 08:24")
    parser.add_argument("--epsilon", type=float, default=1e-6)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    call_types = list(ROUTE_ENDPOINT.keys())

    with psycopg.connect(args.dsn, row_factory=dict_row) as conn:
        grand = 0
        print(f"{'entity':<10} {'rollup':<35} {'changed':>9} {'old$':>14} {'new$':>14} {'delta$':>14}")
        print("-" * 100)
        for entity, (rollup, entity_col) in ENTITIES.items():
            total_changed = 0
            old_sum = 0.0
            new_sum = 0.0
            for call_type, endpoint in ROUTE_ENDPOINT.items():
                rows = conn.execute(
                    recompute_sql(rollup, SOURCE_ID_COLUMN[entity], entity_col),
                    {
                        "call_types": [call_type],
                        "endpoint": endpoint,
                        "since": args.since,
                        "until": args.until,
                        "since_date": args.since,
                        "until_date": args.until,
                        "epsilon": args.epsilon,
                    },
                ).fetchall()
                if not rows:
                    continue
                old_sum += sum(float(r["old_spend"]) for r in rows)
                new_sum += sum(float(r["new_spend"]) for r in rows)
                total_changed += len(rows)
                if args.apply:
                    with conn.cursor() as cur:
                        cur.executemany(
                            UPDATE_SQL.format(rollup=rollup),
                            [{"id": r["id"], "new_spend": float(r["new_spend"])} for r in rows],
                        )
                    conn.commit()
            grand += total_changed
            print(f"{entity:<10} {rollup:<35} {total_changed:>9} {old_sum:>14.2f} {new_sum:>14.2f} {new_sum-old_sum:>14.2f}")
        print("-" * 100)
        print(("APPLIED" if args.apply else "DRY-RUN") + f": {grand} rollup rows rewritten.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
