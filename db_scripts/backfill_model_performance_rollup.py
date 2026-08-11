#!/usr/bin/env python3
"""
One-time backfill of ``LiteLLM_ModelPerformanceRollup`` from the raw spend log.

The rollup table is populated incrementally by the proxy's write path
(``DBSpendUpdateWriter.add_spend_log_transaction_to_model_performance_rollup``),
so on first deploy the table is empty and only starts filling as new requests
arrive. This script reconstructs historical buckets from the existing
``LiteLLM_SpendLogs`` rows so the Model Performance tab shows history right
away instead of waiting for new traffic.

It mirrors the write path exactly:
  - the same (model_group, bucket_start) bucketing, 1-minute aligned,
  - the same TTFT skip rule (``completionStartTime == endTime`` rows excluded,
    negative TTFT dropped),
  - the same (starts, ends) concurrency counters (end placed in the end minute),
  - the same histogram edges / binning (reused from the rollup module),
  - the same merge monoid per bucket, then the same ON CONFLICT upsert.

Design notes
------------
- Standalone: uses only stdlib + ``psycopg`` (psycopg3). It does NOT import the
  proxy/prisma stack, so it can run from a bare venv with just ``DATABASE_URL``
  set. The only litellm import is the histogram edge/bin helpers, which are
  dependency-free and must stay bit-identical to the write path.
- Safe to run any time and re-run: it upserts with the same ON CONFLICT
  semantics the live writer uses, so re-running after more spend logs have
  landed just adds the missing buckets. It never deletes.
- Paginated: processes the spend log in small batches so it does not hold the
  whole table in memory. Commits happen per batch; restart resumes.
- Optional time window: pass ``--start`` / ``--end`` to backfill only a slice.
  Defaults to all rows if neither is given.

Usage
-----
    export DATABASE_URL="postgresql://user:pass@host:5432/litellm"

    # backfill everything
    python db_scripts/backfill_model_performance_rollup.py

    # backfill a window
    python db_scripts/backfill_model_performance_rollup.py \\
        --start 2026-08-01T00:00:00Z --end 2026-08-11T00:00:00Z

    # only one model group, dry run (aggregate, don't write)
    python db_scripts/backfill_model_performance_rollup.py \\
        --model-group gpt-4o --start 2026-08-01T00:00:00Z --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from typing import Iterator, Optional

import psycopg
from psycopg.rows import dict_row

# The only litellm dependency: fixed histogram geometry. Kept bit-identical to
# the write path so a backfilled bin lands where the live writer would put it.
try:
    from litellm.proxy.db.db_transaction_queue.model_performance_rollup_update_queue import (
        build_ttft_histogram,
        ttft_histogram_edges,
    )
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from litellm.proxy.db.db_transaction_queue.model_performance_rollup_update_queue import (
        build_ttft_histogram,
        ttft_histogram_edges,
    )


BATCH_SIZE = 5000


def parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def iter_spend_rows(
    conn: psycopg.Connection,
    start: Optional[datetime],
    end: Optional[datetime],
    model_group: Optional[str],
) -> Iterator[dict]:
    """Yield ``LiteLLM_SpendLogs`` rows in small batches ordered by startTime.

    Keyset-paginated on (startTime, request_id) so it is stable even if rows are
    appended while the backfill runs.
    """
    where: list[str] = []
    params: list[object] = []
    if start is not None:
        where.append('"startTime" >= %s')
        params.append(start)
    if end is not None:
        where.append('"startTime" < %s')
        params.append(end)
    if model_group is not None:
        where.append('"model_group" = %s')
        params.append(model_group)

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    select_sql = f"""
        SELECT
            "request_id", "model_group", "startTime", "endTime", "completionStartTime",
            "completion_tokens", "request_duration_ms", "cache_hit"
        FROM "LiteLLM_SpendLogs"
        {where_sql}
    """
    order_sql = ' ORDER BY "startTime" ASC, "request_id" ASC LIMIT %s'

    anchor_sql = select_sql + order_sql
    # Keyset clause. When no filters are present ``select_sql`` has no WHERE, so
    # the pagination predicate must introduce one with ``WHERE`` rather than
    # ``AND``.
    pagination_sql = (
        " WHERE "
        if not where
        else " AND "
    ) + '(("startTime" > %s) OR ("startTime" = %s AND "request_id" > %s))'
    page_sql = select_sql + pagination_sql + order_sql

    with conn.cursor(row_factory=dict_row) as cur:
        last_ts: Optional[datetime] = None
        last_id: Optional[str] = None
        while True:
            if last_ts is None and last_id is None:
                cur.execute(anchor_sql, [*params, BATCH_SIZE])
            else:
                cur.execute(page_sql, [*params, last_ts, last_ts, last_id, BATCH_SIZE])

            rows = cur.fetchall()
            if not rows:
                break

            for row in rows:
                yield row

            last_ts = rows[-1]["startTime"]
            last_id = rows[-1]["request_id"]

            if len(rows) < BATCH_SIZE:
                break


def build_start_transaction(row: dict) -> Optional[dict]:
    """Replicate the write path's per-request rollup transaction for one row.

    Returns a bucket dict, or None if the row is skipped (no model_group, cache
    hit, invalid startTime).
    """
    model_group = row.get("model_group")
    if not model_group:
        return None
    if row.get("cache_hit") == "True":
        return None

    start_time = row.get("startTime")
    if not isinstance(start_time, datetime):
        return None
    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=timezone.utc)

    bucket_start = start_time.replace(second=0, microsecond=0)

    completion_tokens = row.get("completion_tokens") or 0
    request_duration_ms = row.get("request_duration_ms")
    throughput_tokens_sum = 0.0
    if request_duration_ms is not None and request_duration_ms > 0:
        throughput_tokens_sum = float(completion_tokens) / float(request_duration_ms) * 1000.0

    # TTFT skip rule, mirroring the write path and the raw read path.
    end_time = row.get("endTime")
    completion_start_time = row.get("completionStartTime")
    ttft_seconds: Optional[float] = None
    if completion_start_time is not None and end_time is not None and completion_start_time != end_time:
        if isinstance(completion_start_time, datetime):
            cst = completion_start_time
            if cst.tzinfo is None:
                cst = cst.replace(tzinfo=timezone.utc)
            ttft_seconds = (cst - start_time).total_seconds()
    if ttft_seconds is not None and ttft_seconds < 0:
        ttft_seconds = None

    end_dt: Optional[datetime] = None
    if end_time is not None:
        end_dt = end_time
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
    same_minute = end_dt is not None and end_dt.replace(second=0, microsecond=0) == bucket_start

    edges = ttft_histogram_edges()

    return {
        "model_group": model_group,
        "bucket_start": bucket_start.isoformat(),
        "request_count": 1,
        "completion_tokens": completion_tokens,
        "throughput_tokens_sum": throughput_tokens_sum,
        "ttft_seconds_sum": ttft_seconds if ttft_seconds is not None else 0.0,
        "ttft_seconds_sum_sq": ttft_seconds**2 if ttft_seconds is not None else 0.0,
        "ttft_seconds_min": ttft_seconds,
        "ttft_seconds_max": ttft_seconds,
        "edges": edges,
        "histogram_counts": build_ttft_histogram(ttft_seconds, edges),
        "starts": 1,
        "ends": 1 if same_minute else 0,
    }


def build_end_transaction(row: dict) -> Optional[dict]:
    """Build the dedicated -1 end-minute transaction for a cross-minute request.

    Returns None if the request ends in the same minute it starts, or if it is
    not eligible (no model_group, cache hit, missing timestamps).
    """
    model_group = row.get("model_group")
    if not model_group or row.get("cache_hit") == "True":
        return None

    start_time = row.get("startTime")
    end_time = row.get("endTime")
    if not isinstance(start_time, datetime) or not isinstance(end_time, datetime):
        return None
    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=timezone.utc)
    if end_time.tzinfo is None:
        end_time = end_time.replace(tzinfo=timezone.utc)

    start_bucket = start_time.replace(second=0, microsecond=0)
    end_bucket = end_time.replace(second=0, microsecond=0)
    if end_bucket == start_bucket:
        return None

    edges = ttft_histogram_edges()
    return {
        "model_group": model_group,
        "bucket_start": end_bucket.isoformat(),
        "request_count": 0,
        "completion_tokens": 0,
        "throughput_tokens_sum": 0.0,
        "ttft_seconds_sum": 0.0,
        "ttft_seconds_sum_sq": 0.0,
        "ttft_seconds_min": None,
        "ttft_seconds_max": None,
        "edges": edges,
        "histogram_counts": [0] * len(edges),
        "starts": 0,
        "ends": 1,
    }


def merge_into(account: dict[tuple, dict], txn: dict) -> None:
    """Merge one transaction into the per-bucket accumulator (monoid combine)."""
    key = (txn["model_group"], txn["bucket_start"])
    cur = account.get(key)
    if cur is None:
        account[key] = {
            "model_group": txn["model_group"],
            "bucket_start": txn["bucket_start"],
            "request_count": txn["request_count"],
            "completion_tokens": txn["completion_tokens"],
            "throughput_tokens_sum": txn["throughput_tokens_sum"],
            "ttft_seconds_sum": txn["ttft_seconds_sum"],
            "ttft_seconds_sum_sq": txn["ttft_seconds_sum_sq"],
            "ttft_seconds_min": txn["ttft_seconds_min"],
            "ttft_seconds_max": txn["ttft_seconds_max"],
            "edges": list(txn["edges"]),
            "histogram_counts": list(txn["histogram_counts"]),
            "starts": txn["starts"],
            "ends": txn["ends"],
        }
        return

    cur["request_count"] += txn["request_count"]
    cur["completion_tokens"] += txn["completion_tokens"]
    cur["throughput_tokens_sum"] += txn["throughput_tokens_sum"]
    cur["ttft_seconds_sum"] += txn["ttft_seconds_sum"]
    cur["ttft_seconds_sum_sq"] += txn["ttft_seconds_sum_sq"]
    if txn["ttft_seconds_min"] is not None:
        cur["ttft_seconds_min"] = (
            txn["ttft_seconds_min"]
            if cur["ttft_seconds_min"] is None
            else min(cur["ttft_seconds_min"], txn["ttft_seconds_min"])
        )
    if txn["ttft_seconds_max"] is not None:
        cur["ttft_seconds_max"] = (
            txn["ttft_seconds_max"]
            if cur["ttft_seconds_max"] is None
            else max(cur["ttft_seconds_max"], txn["ttft_seconds_max"])
        )
    for i, c in enumerate(txn["histogram_counts"]):
        cur["histogram_counts"][i] += c
    cur["starts"] += txn["starts"]
    cur["ends"] += txn["ends"]


def upsert_batch(conn: psycopg.Connection, account: dict[tuple, dict]) -> None:
    """Flush merged buckets with the same ON CONFLICT upsert the writer uses."""
    if not account:
        return

    rows_sql: list[str] = []
    params: list[object] = []

    for txn in account.values():
        # Build Postgres array literals from the histogram (all floats/ints we
        # control, so direct interpolation is safe). ``Infinity`` is the
        # element spelling PostgreSQL accepts for the final +inf edge.
        edges_literal = "{" + ",".join(
            ("Infinity" if e == float("inf") else repr(float(e))) for e in txn["edges"]
        ) + "}"
        counts_literal = "{" + ",".join(str(c) for c in txn["histogram_counts"]) + "}"
        rows_sql.append(
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, "
            f"'{edges_literal}'::double precision[], '{counts_literal}'::bigint[], %s, %s)"
        )
        params.extend(
            [
                txn["model_group"],
                txn["bucket_start"],
                txn["request_count"],
                txn["completion_tokens"],
                txn["throughput_tokens_sum"],
                txn["ttft_seconds_sum"],
                txn["ttft_seconds_sum_sq"],
                txn["ttft_seconds_min"],
                txn["ttft_seconds_max"],
                txn["starts"],
                txn["ends"],
            ]
        )

    sql = f"""
        INSERT INTO "LiteLLM_ModelPerformanceRollup" (
            "model_group", "bucket_start",
            "request_count", "completion_tokens", "throughput_tokens_sum",
            "ttft_seconds_sum", "ttft_seconds_sum_sq", "ttft_seconds_min", "ttft_seconds_max",
            "ttft_histogram_edges", "ttft_histogram_counts",
            "starts", "ends"
        ) VALUES {",".join(rows_sql)}
        ON CONFLICT ("model_group", "bucket_start") DO UPDATE SET
            "request_count" = "LiteLLM_ModelPerformanceRollup"."request_count" + EXCLUDED."request_count",
            "completion_tokens" = "LiteLLM_ModelPerformanceRollup"."completion_tokens" + EXCLUDED."completion_tokens",
            "throughput_tokens_sum" = "LiteLLM_ModelPerformanceRollup"."throughput_tokens_sum" + EXCLUDED."throughput_tokens_sum",
            "ttft_seconds_sum" = "LiteLLM_ModelPerformanceRollup"."ttft_seconds_sum" + EXCLUDED."ttft_seconds_sum",
            "ttft_seconds_sum_sq" = "LiteLLM_ModelPerformanceRollup"."ttft_seconds_sum_sq" + EXCLUDED."ttft_seconds_sum_sq",
            "ttft_seconds_min" = LEAST(
                COALESCE("LiteLLM_ModelPerformanceRollup"."ttft_seconds_min", EXCLUDED."ttft_seconds_min"),
                EXCLUDED."ttft_seconds_min"
            ),
            "ttft_seconds_max" = GREATEST(
                COALESCE("LiteLLM_ModelPerformanceRollup"."ttft_seconds_max", EXCLUDED."ttft_seconds_max"),
                EXCLUDED."ttft_seconds_max"
            ),
            "ttft_histogram_counts" = _rollup_array_add_bigint(
                "LiteLLM_ModelPerformanceRollup"."ttft_histogram_counts",
                EXCLUDED."ttft_histogram_counts"
            ),
            "starts" = "LiteLLM_ModelPerformanceRollup"."starts" + EXCLUDED."starts",
            "ends" = "LiteLLM_ModelPerformanceRollup"."ends" + EXCLUDED."ends"
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL"),
        help="Postgres DSN (default: $DATABASE_URL)",
    )
    parser.add_argument("--start", help="Backfill from this time (ISO 8601, e.g. 2026-08-01T00:00:00Z)")
    parser.add_argument("--end", help="Backfill up to (exclusive) this time")
    parser.add_argument("--model-group", help="Only backfill this model_group")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Aggregate but don't write; print bucket/row counts",
    )
    args = parser.parse_args()

    if not args.database_url:
        parser.error("--database-url is required (or set DATABASE_URL)")

    start = parse_dt(args.start) if args.start else None
    end = parse_dt(args.end) if args.end else None
    if start is not None and end is not None and start >= end:
        parser.error("--start must be before --end")

    conn = psycopg.connect(args.database_url)

    # Pre-flight: the rollup table + array-add helper must exist (migration ran).
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('\"LiteLLM_ModelPerformanceRollup\"')")
        if cur.fetchone()[0] is None:
            return
        cur.execute("SELECT to_regprocedure('_rollup_array_add_bigint(bigint[], bigint[])')")
        if cur.fetchone()[0] is None:
            return

    row_count = 0
    bucket_count = 0
    account: dict[tuple, dict] = {}

    for row in iter_spend_rows(conn, start, end, args.model_group):
        row_count += 1
        start_txn = build_start_transaction(row)
        if start_txn is not None:
            merge_into(account, start_txn)
        end_txn = build_end_transaction(row)
        if end_txn is not None:
            merge_into(account, end_txn)

        if row_count % BATCH_SIZE == 0:
            bucket_count += len(account)
            if args.dry_run:
                pass
            else:
                upsert_batch(conn, account)
            account.clear()

    # Tail batch.
    bucket_count += len(account)
    if args.dry_run:
        pass
    else:
        upsert_batch(conn, account)



if __name__ == "__main__":
    main()