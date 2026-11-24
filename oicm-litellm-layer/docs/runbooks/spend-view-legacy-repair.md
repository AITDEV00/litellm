# Spend View Legacy Repair Runbook

One-shot operator repair for the seven spend aggregation views when they are owned
by the wrong role (the legacy state: created by `postgres` instead of the gateway's
application role). The regular Prisma migration refuses that state on purpose, so
this is the only sanctioned path out of it.

Status: proven against a legacy-state fixture (2026-11-24). Not yet run against any
live environment.

## When to run

Symptoms on the affected gateway:

- `/global/spend/models` and `/global/spend/keys` return HTTP 500 with
  `permission denied for view "Last30dModelsBySpend"` (or the keys view) in the
  gateway logs.
- `prisma migrate deploy` fails with
  `spend view ... is owned by postgres, expected litellm; refusing to borrow another owner`.

Precheck (read-only, from the cluster):

```bash
kubectl -n adeo-litellm exec <postgres-pod> -- psql -U postgres -d litellm -t -A -F '|' -c \
  "SELECT c.relname, pg_get_userbyid(c.relowner), c.relkind, coalesce(c.relacl::text,'NULL')
   FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE n.nspname='public' AND c.relname IN
   ('MonthlyGlobalSpend','Last30dKeysBySpend','Last30dModelsBySpend','MonthlyGlobalSpendPerKey',
    'MonthlyGlobalSpendPerUserPerKey','DailyTagSpend','Last30dTopEndUsersSpend');"
```

Any row whose owner is not the gateway's DB role (check `DATABASE_USERNAME` in the
gateway deployment, and the `username` key of the CNPG `<cluster>-app` secret) means
this runbook applies.

## What it does

`scripts/repair_legacy_spend_views.py` (repo root):

1. Connects with an operator-supplied superuser URL. Never reads or prints the
   gateway's password; `--gateway-url-env` only extracts the *username* in memory
   for a cross-check.
2. Prechecks: the running role is a superuser; the target application role exists
   and is not itself a superuser; it matches the gateway's configured username when
   provided; every existing same-named object is a view owned by either the running
   superuser or the application role (a third-party owner is refused).
3. Snapshots owner/relkind/ACL/definition for all seven views to a local JSON file
   (`--snapshot-out`, default `spend-view-repair-snapshot.json`), never into the
   database and never with credentials.
4. In one transaction with `lock_timeout = 10s`,
   `idle_in_transaction_session_timeout = 30s`, `statement_timeout = 120s`: drops
   and recreates all seven from the single authoritative source
   (`litellm/proxy/db/spend_view_definitions.py`, the same renders the migration and
   boot bootstrap use) and sets `OWNER TO` the application role. No row rewrites,
   no `GRANT ALL`, no blanket `REVOKE`: the intended end state is the default ACL
   (`relacl IS NULL`) with the application role as owner.
5. Re-runs the exact guarded migration SQL under `SET ROLE <application role>` in
   the same session, proving the application role can now converge the views itself.
   If role membership does not permit this, the script fails rather than substituting
   a different owner.
6. Verifies every view is readable as the application role (a real `SELECT`, not a
   superuser `has_table_privilege` helper) and that `DailyTagSpend` exposes the
   4-column contract (`individual_request_tag, spend_date, log_count, total_spend`).

## Invocation

From a machine with superuser access to the target database (CNPG pod exec or a
port-forward with the superuser secret). Never run against prod without the change
window and approval described in the spend-view repair plan.

```bash
# dev example (port-forward the dev cluster first):
kubectl -n adeo-litellm port-forward svc/adeo-litellm-postgres-dev-rw 15432:5432

python scripts/repair_legacy_spend_views.py \
  --superuser-url postgresql://postgres@127.0.0.1:15432/litellm \
  --app-role litellm
```

For CNPG the superuser URL can come from the pod's trust auth (run the script inside
the postgres container, or forward and use the `<cluster>-superuser` secret). Do not
put the URL in shell history files that are shared; prefer an env var or pod exec.

After the repair, the next gateway rollout runs the guarded migration, which now
finds every view owned by the application role and converges definitions in place.

## Rollback

Views hold no data. If the repair result is wrong, restore each view from the
snapshot JSON: recreate it with the recorded definition, then
`ALTER VIEW ... OWNER TO <recorded owner>` (only if that role still exists). No
spend rows were touched, so there is nothing else to roll back.

## Evidence from the fixture run (2026-11-24)

Legacy state: `Last30dModelsBySpend` and `DailyTagSpend` owned by `postgres`, tag
view in the legacy 3-column shape. The migration refused this state both as the
application role and as a superuser session under `SET ROLE` (the guard checks
ownership, not privilege). After the repair: all seven views owned by `litellm` with
`relacl IS NULL`, readable as `litellm`, tag view at the 4-column contract, and the
guarded migration SQL re-ran clean under `SET ROLE litellm`.
