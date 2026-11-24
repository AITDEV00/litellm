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
   database and never with credentials. It also records every non-owner reader
   grant (a dashboard role, an operator group) so the repair can re-issue them.
4. Prechecks `pg_depend` for dependent objects (a materialized view or another
   view selecting from `DailyTagSpend` or `Last30dTopEndUsersSpend`): the repair
   never cascades, so any dependent is a hard stop before any DDL.
5. In ONE transaction with `lock_timeout = 10s`,
   `idle_in_transaction_session_timeout = 30s`, `statement_timeout = 120s`:
   CREATE OR REPLACEs the six column-stable views from the single authoritative
   source, drops/recreates only `DailyTagSpend` (the one view whose legacy shape
   lacks columns CREATE OR REPLACE cannot add) plus `Last30dTopEndUsersSpend`
   (plain-CREATE definition), sets `OWNER TO` the application role, then re-runs
   the exact guarded migration SQL under `SET LOCAL ROLE` proving the application
   role can converge the views itself, then re-issues the recorded reader
   grants. A failure anywhere rolls the whole transaction back: no partially
   repaired state survives.
6. After that transaction commits, opens a SEPARATE connection and verifies the
   post-state catalog (ownership, relkind, 4-column tag contract) BEFORE any
   read, then performs real SELECTs as the application role: a trivial probe for
   the six ordinary views, and for the tag view only a narrow checked date window
   (yesterday..today), never an unbounded scan of retained tag history. A failure
   here is reported explicitly as "DDL committed but verification failed", never
   as total failure. No row rewrites, no `GRANT ALL`, no blanket `REVOKE`, no
   `CASCADE`.

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

Views hold no data. If the repair transaction itself fails, nothing changed:
it rolled back atomically. If the repair result is wrong after commit, restore
each view from the snapshot JSON: recreate it with the recorded definition, then
`ALTER VIEW ... OWNER TO <recorded owner>` (only if that role still exists), and
re-grant the recorded reader ACL entries. No spend rows were touched, so there is
nothing else to roll back.

## Evidence from the fixture run (2026-11-24)

Legacy state: `Last30dModelsBySpend` and `DailyTagSpend` owned by `postgres`, tag
view in the legacy 3-column shape. The migration refused this state both as the
application role and as a superuser session under `SET ROLE` (the guard checks
ownership, not privilege). After the repair: all seven views owned by `litellm`,
readable as `litellm`, tag view at the 4-column contract, the guarded migration
SQL re-ran clean under `SET ROLE litellm`, and an operator-installed
`dashboard_reader` SELECT grant survived the repair. Failure paths are pinned by
tests: a mid-transaction DDL failure rolls the whole repair back to the legacy
state, a post-commit verification failure is reported as "DDL committed" rather
than total failure, and a view with a dependent materialized view is refused
before any DDL runs.
