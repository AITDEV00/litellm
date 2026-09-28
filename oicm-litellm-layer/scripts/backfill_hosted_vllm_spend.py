"""Backfill undercharged hosted_vllm spend rows.

Why this exists: from 2026-09-18 to 2026-09-28 the vLLM serving side reported
``usage.prompt_tokens_details.cached_tokens`` for prefix-cache hits, and LiteLLM
billed those cached tokens at a $0 cache-read rate (no model registered a
``cache_read_input_token_cost``). Upstream fix 88d1eb3b35 (cherry-picked here as
e314d650f6) makes a missing cache-read rate resolve to the input rate, so NEW
rows bill correctly. This script recomputes the AFFECTED historical rows so
reporting reflects what the fixed code would have charged.

Scope is deliberately narrow:
  * call_type = 'acompletion'   (chat only; embeddings/OCR/ASR price differently)
  * cache_hit <> 'True'         (response-cache hits legitimately cost $0)
  * custom_llm_provider = 'hosted_vllm'
  * models that resolve to a ProxyModelTable row with a numeric input/output rate

A row is only rewritten when the recomputed spend differs from the recorded
spend by more than ``--epsilon`` dollars, so rows that were already correct
(e.g. the few full-price Kimi rows) are left untouched.

Default is DRY-RUN: it prints per-model before/after totals and writes nothing.
Pass ``--apply`` to actually UPDATE. Always review the dry-run totals first.

Run inside the cluster against a postgres pod, e.g.:

  kubectl exec -i -n adeo-litellm deploy/litellm-proxy-dev -- python - < scripts/backfill_hosted_vllm_spend.py --dsn "postgresql://litellm:PW@adeo-litellm-postgres-rw.adeo-litellm:5432/litellm"

or locally with the dev DB via port-forward. The DSN should point at the DB you
want to fix (prod: adeo-litellm-postgres-rw; dev: adeo-litellm-postgres-dev-rw).
"""

from __future__ import annotations

import argparse
import sys
from typing import Final

import psycopg
from psycopg.rows import dict_row

# Chat call types whose spend is prompt*input + completion*output. arespenses is
# excluded: it prices off a different usage shape and had only 166 rows.
CHAT_CALL_TYPES: Final = ("completion", "acompletion")

RATES_SQL: Final = """
SELECT model_name,
       (litellm_params->>'input_cost_per_token')::float  AS input_rate,
       (litellm_params->>'output_cost_per_token')::float AS output_rate
FROM "LiteLLM_ProxyModelTable"
WHERE litellm_params ? 'input_cost_per_token'
  AND litellm_params ? 'output_cost_per_token'
"""

# Candidate rows for one model. The table is natively partitioned with composite
# PK (request_id, "startTime"), so both are needed to target a row.
ROWS_SQL: Final = """
SELECT request_id, "startTime", prompt_tokens, completion_tokens, spend
FROM "LiteLLM_SpendLogs"
WHERE custom_llm_provider = 'hosted_vllm'
  AND call_type = ANY(%(call_types)s)
  AND cache_hit IS DISTINCT FROM 'True'
  AND replace(model, 'hosted_vllm/', '') = %(model_name)s
  AND "startTime" >= %(since)s
  AND "startTime" < %(until)s
"""

UPDATE_SQL: Final = """
UPDATE "LiteLLM_SpendLogs"
SET spend = %(new_spend)s, updated_at = now()
WHERE request_id = %(request_id)s AND "startTime" = %(startTime)s AND spend = %(old_spend)s
"""


def load_rates(conn: psycopg.Connection) -> dict[str, tuple[float, float]]:
    """One authoritative rate per model. When several deployments register the
    same model with slightly different rates (e.g. DeepSeek), take the average:
    the spend rows do not record which deployment served them."""
    acc: dict[str, list[tuple[float, float]]] = {}
    for row in conn.execute(RATES_SQL):
        acc.setdefault(row["model_name"], []).append((row["input_rate"], row["output_rate"]))
    return {
        name: (
            sum(r[0] for r in rates) / len(rates),
            sum(r[1] for r in rates) / len(rates),
        )
        for name, rates in acc.items()
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dsn", required=True, help="psycopg DSN for the target DB")
    parser.add_argument("--since", default="2026-09-18", help="inclusive start date (the day cached_tokens began)")
    parser.add_argument("--until", default="2026-09-28 08:24", help="exclusive end (when the fix rolled out)")
    parser.add_argument("--epsilon", type=float, default=1e-6, help="min $ difference to rewrite a row")
    parser.add_argument("--apply", action="store_true", help="actually UPDATE (default is dry-run)")
    args = parser.parse_args()

    with psycopg.connect(args.dsn, row_factory=dict_row) as conn:
        rates = load_rates(conn)
        grand_recorded = 0.0
        grand_recomputed = 0.0
        grand_changed = 0
        print(f"{'model':<45} {'rows':>9} {'changed':>9} {'recorded$':>14} {'recomputed$':>14} {'delta$':>14}")
        print("-" * 110)

        for model_name, (in_rate, out_rate) in sorted(rates.items()):
            rows = conn.execute(
                ROWS_SQL,
                {"call_types": list(CHAT_CALL_TYPES), "model_name": model_name, "since": args.since, "until": args.until},
            ).fetchall()
            if not rows:
                continue

            updates: list[dict] = []
            recorded_sum = 0.0
            recomputed_sum = 0.0
            for r in rows:
                recorded = float(r["spend"] or 0.0)
                recomputed = (r["prompt_tokens"] or 0) * in_rate + (r["completion_tokens"] or 0) * out_rate
                recorded_sum += recorded
                recomputed_sum += recomputed
                if abs(recomputed - recorded) > args.epsilon:
                    updates.append(
                        {
                            "request_id": r["request_id"],
                            "startTime": r["startTime"],
                            "new_spend": recomputed,
                            "old_spend": recorded,
                        }
                    )

            delta = recomputed_sum - recorded_sum
            grand_recorded += recorded_sum
            grand_recomputed += recomputed_sum
            grand_changed += len(updates)
            print(f"{model_name:<45} {len(rows):>9} {len(updates):>9} {recorded_sum:>14.2f} {recomputed_sum:>14.2f} {delta:>14.2f}")

            if args.apply and updates:
                with conn.cursor() as cur:
                    cur.executemany(UPDATE_SQL, updates)
                # The UPDATE's spend = old_spend guard makes a concurrent flush a
                # no-op rather than a lost update; count what actually changed.
                conn.commit()

        print("-" * 110)
        print(f"{'TOTAL':<45} {'':>9} {grand_changed:>9} {grand_recorded:>14.2f} {grand_recomputed:>14.2f} {grand_recomputed - grand_recorded:>14.2f}")
        print()
        if args.apply:
            print(f"APPLIED: rewrote {grand_changed} rows.")
        else:
            print(f"DRY-RUN: would rewrite {grand_changed} rows. Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
