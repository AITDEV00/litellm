# Spend View Legacy Repair Runbook

One-shot operator repair for the seven spend aggregation views when they are owned
by the wrong role (the legacy state: created by `postgres` instead of the gateway's
application role). The regular Prisma migration refuses that state on purpose, so
this is the only sanctioned path out of it.

Status: prod repair and rollout independently verified 2026-11-24. The regression
fixture and this runbook are available for a future legacy installation only. Do not
re-run the repair on the already repaired prod DB merely to check it is working.

## Completed prod outcome, 2026-11-24

The fork's guarded migration and legacy ownership repair are live in prod:

- Commit `ac831e149f` pins prod gateway image `litellm-src:jya0-v1.108.5-prod` at
  digest `sha256:9ff9cd4f9c13e2eccb886410ad2455333d4ba2331a
  d23e33d44d6a25fe6e6d6e`.
- Both replicas run the same digest with no restarts as independently checked after
  rollout.
- `20261124120000_spend_view_definitions_and_ownership` applied successfully during
  boot and the migration ledger has no unfinished migration.
- All seven views belong to `litellm`, including the four-column
  `individual_request_tag, spend_date, log_count, total_spend` tag-view contract.
- Admin and the existing `proxy_admin_viewer` Athena reader each received 200 on
  `/global/spend/models`, `/global/spend/keys`, `/global/spend/tags`, and
  `/global/spend/all_tag_names`. Athena still receives 403 on inference.
- Representative public-ingress reads were about 8-10s for model/key aggregate
  views, about 0.2s for tag aggregation, and about 0.05s for tag names. Model/key
  are not unbounded history scans but attractive follow-up optimization candidates.
- Tag `log_count` is now `SUM(api_requests)`, documented in the repair plan. This is
  caller-facing request count (excluding internal calls and billed retries), not
  the former raw physical-log count.
- The first reader-verification attempt timed out after commit, but the final
  `LIMIT 0` autocommit proof succeeded. No rollback was executed and no data rows
  were changed.

Operator artifacts were retained outside git at
`~/adeo-prod-ops/spend-view-repair-2026-11-24`: pre/post snapshots, catalog checks,
migration reference, and the verification log. Do not put credentials or gateway
connection URLs into that directory or into Git.

The first operator-run verification attempted a bounded-by-result `LIMIT 1` tag
probe, but the full-history view aggregation timed out at the configured statement
timeout. The DDL had already committed and the output said so. The script was then
hardened to `LIMIT 0` plus autocommit verification and the second run succeeded.
This was a verification-time issue, not an endpoint regression. The final tag HTTP
proof did not hit the raw log at all.

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
   repaired state survives. `oicm` is not a legacy grant on prod because the
   pre-repair catalog had `relacl NULL` on all seven views, so the restore set is
   empty there. Any pre-existing admin grant from unrelated DB default privileges
   is a separate cleanup decision, not a reader-grant restoration.
6. After that transaction commits, opens a SEPARATE autocommit connection and
   verifies the post-state catalog (ownership, relkind, four-column tag contract)
   BEFORE any read. All probes are `LIMIT 0`, so they prove ACL and view parsing
   without executing raw-tag aggregation or fetching rows. A failure is reported
   explicitly as "DDL committed but verification failed", never as total failure.
   No row rewrites, no explicit `GRANT ALL`, no blanket owner `REVOKE`, no `CASCADE`.

## Invocation

Run from the operator workstation using the repo's `.venv/bin/python`, so installed
psycopg and LiteLLM dependencies are available. Local TCP to a CNPG port-forward is
not trusted in the same manner as the container's local Unix socket, so provide the
prod superuser password in memory through its protected secret. Never use an
unqualified `postgresql://postgres@localhost` URL for the remote forwarded prod DB.
The CNPG pod itself has psql but no psycopg, so do not simply copy this Python
invocation directly into the pod.

The command must make the expected application-role cross-check explicit for prod.
The script's `--gateway-url-env` option is otherwise optional. Obtain the gateway's
`DATABASE_URL` value into memory but do not write or echo it:

```bash
kubectl -n adeo-litellm port-forward svc/adeo-litellm-postgres-rw 15432:5432

PG_SUPERUSER_PASSWORD="$(kubectl -n adeo-litellm get secret adeo-litellm-postgres-superuser -o jsonpath='{.data.password}' | base64 -d)"
export DATABASE_URL="$(kubectl -n adeo-litellm get secret litellm-db-credentials -o jsonpath='{.data.DATABASE_URL}' | base64 -d)"
.venv/bin/python scripts/repair_legacy_spend_views.py \
  --superuser-url "postgresql://postgres:${PG_SUPERUSER_PASSWORD}@127.0.0.1:15432/litellm" \
  --app-role litellm \
  --gateway-url-env DATABASE_URL \
  --snapshot-out ~/adeo-prod-ops/spend-view-repair-YYYYMMDD/pre-repair-snapshot.json
```

Run these in the operator's private shell rather than a shared terminal log. Close
the forward and clear those in-memory values afterward. The script only parses the
gateway role from `DATABASE_URL` in memory, never its password for the repair proof.
Local Unix-socket trust is for interactive psql inside the CNPG postgres container,
not an instruction to drop password authentication from port-forwarded connections.

Default privileges like `ALTER DEFAULT PRIVILEGES ... GRANT ALL` are outside the
reader-grant capture/restore semantics and can reapply at view recreation. Audit the
final catalog ACLs and any new inherited grants before signing off; the guarded
migration cannot reset them because it deliberately runs under the application role.

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

## Follow-up task: clean up broad default view grants

This is a planning task only; no live grant has been changed as part of this doc
update. Do not alter live privileges until the implementation is separately approved
and dev proof is recorded here.

### Verified baseline and conditions to act

The upstream `v1.108.5` `litellm/proxy/db/create_views.py`, `db_scripts/create_views.py`,
and `litellm-proxy-extras/litellm_proxy_extras/migrations/` contain no `GRANT`,
`REVOKE`, `ALTER DEFAULT PRIVILEGES`, or `OWNER TO` statements for the ordinary
views. Creating a view under `litellm` gives the application role implicit complete
view rights without an explicit admin grant, but does not widen any other principal's
grant on that view. In this narrow sense the upstream contract is better because it does not grant
unrelated admin access when an app-owned view is created.

The fork-local prod janitor bootstrap contains:

```sql
ALTER DEFAULT PRIVILEGES FOR ROLE litellm GRANT ALL ON TABLES TO oicm;
```

This policy applies to all later-created tables **and views**, not just archive
partitions, and reintroduces broad grants whenever tag-view repair or a migration
recreates the view. After the actual prod repair the tag view acquired
`oicm=arwdDxtm/litellm`, `Last30dTopEndUsersSpend` acquired a non-NULL self-all ACL,
and actual pre-repair ACLs were NULL on all seven. Removing only the bootstrap line
does not clean up the already effective `pg_default_acl` row.

The grant does not add a new administrative category for the current janitor: prod
`oicm` has explicit `litellm` role membership so that it can dump/detach partitions
already owned by `litellm`. Nevertheless the broad grant is not needed for the
role-membership design, weakens least privilege, and obscures the intended view
permission contract when inspecting the catalog. A staged cleanup is justified for
these reasons, not by claiming the current janitor would become harmless.

No equivalent upstream change is needed because upstream has no explicit alternate
reader/admin grant to restore. Keep this fork-local.

### Exact targeted state

Only target the `litellm`-creator `oicm`-grantee `r` table default grant that the
fork uses for future partitions. Leave the separate `postgres`-creator `litellm`
default grant alone, because it exists to cover historical superuser-created
partitions and cannot be conflated with the automatic `oicm` view problem.

The cleanup needs two coordinated actions, separately verified:

1. Remove the broad per-database default grant by `ALTER DEFAULT PRIVILEGES FOR ROLE
   litellm REVOKE ALL ON TABLES FROM oicm;` executed under `postgres` superuser.
   Preserve any other default entries and `GRANT litellm TO oicm` membership, `CREATE`
   DB permission, and ledger access. Do not remove `litellm` role access itself.
2. For exactly the seven named spend views and only any residual janitor-specific
   `ALL ON TABLE "LiteLLM_SpendLogs"` grant the Janitor actually does not need (see
   below), revoke excessive non-owner privileges while retaining intended dedicated
   reader grants where applicable. On the standard prod baseline expected outcome
   for `DailyTagSpend` is NULL ACL to `oicm`, not empty `{}` and not another
   auto-created admin row. Because Postgres `DEFAULT PRIVILEGES` auto-materialize
   after every view recreation, clean persistent grant configuration before cleaning
   existing views.

`CREATE OR REPLACE VIEW` does not reset ACLs for all stable views, so do not expect
an automatic global grant reset. Manually review actual `aclexplode` before touching
each object. Use an existence guard and collect its explicit `oicm` non-read entries
into the operator evidence; if the grantee/privilege changed after the prod snapshot,
treat that as unexpected configuration and request review rather than revoking it.

Do not revoke any legitimate non-inherited, explicitly granted reader role as a side
effect. Decide what "janitor access to raw `LiteLLM_SpendLogs`" specifically remains
needed: the partition conversion document instructs access via short-lived
`SET ROLE litellm`. `pg_dump` on the parent needs that ownership, while direct SELECT
privileges inherited through role inheritance are sufficient without a default
`GRANT ALL` for every future object. Prove this with a running fixture or an
appropriate dev data trial rather than checking SQL text alone.

Leave `LiteLLM_VerificationTokenView` outside scope: it powers authentication and
already has the correct owner. Avoid registry-wide default privilege rewrites for
other tables and other services.

### Implementation steps

1. Create a dev real-Postgres fixture with the actual roles: superuser `postgres`,
   non-superuser `litellm`, and `oicm MEMBER OF litellm`, with `CREATE DATABASE` and
   ledger grants. Recreate the exact `pg_default_acl` row and the seven canonical
   views, not just one dummy view.
2. Confirm negative behavior first: new tag view creates `oicm=arwdDxtm/litellm`, so
   default privileges recreate the reported failure. Grant a separate fixture
   `dashboard_reader SELECT` explicitly, test that capture/restore retains the
   intended reader role without incidental removal.
3. Implement a separate OICM layer SQL/script operator command with explicit prod /
   dev selectors. Default it to dry-run: fetch `pg_default_acl`, `pg_roles`,
   role-membership, current view ACLs, ownership, and dependencies and produce only a
   proposed change list. Refuse unknown entries and refuse any rollback that would
   require owner downgrade or third-party object alteration. The script may not run
   at proxy boot or as a `SET ROLE`-capable opportunistic superuser: ordinary
   migrations cannot downgrade this database-level grant.
4. In the privileged explicitly-gated run, one transaction with bounded
   `lock_timeout`, `statement_timeout`, and idle-in-transaction timeout does (a)
   persistent default-grant cleanup, (b) scoped excessive-view-grant cleanup, (c)
   catalog verification. No user-row updates, no table drop, and no rebuild. Use
   parameterized SQL for role identities. Preserve explicitly-authorized read grants
   and original view owner. `REVOKE ALL ... FROM <view-owner>` is banned. End state
   app owner should be able to read every view whether it relies on default implicit
   owner rights or explicitly recorded owner ACL.
5. Re-run the exact existing view repair path once in fixture and in dev to prove
   default grants no longer return `ALL` to `oicm`. A no-mutation revoking test must
   demonstrate `pg_default_acl` is gone, then another recreate does not resurrect it.
6. Dev database verification: apply only chosen janitor default-grant cleanup after
   capturing default and view ACLs. Verify `Litellm_SpendLogs` partitions remain
   litellm-owned, `oicm` remains member of `litellm`, `LITEllM_DailyTagSpend` spend
   writer still works, and go through a noteably controlled dev spend-archive cycle.
   Check one finished shell janitor job rather than delete/schedule the production
   job. Validate actual janitor permissions for pg_dump and archive detachment/restore
   at a data level on dev, not only psycopg's `has_table_privilege` result.
7. Run the prod acceptance HTTP checks through public ingress for admin and the
   existing Athena viewer from this runbook. Check four reads return 200, test key
   creation of any fixture is explicitly authorized and cleaned up, no privileged
   writes are performed, and inference remains 403 for viewer.
8. Run focused tests and actual-Postgres 16 privilege regressions, run docs build,
   and commit/push before live prod work. Do not treat a skipped fixture or a
   regular `psql` success as proof the role mismatch was fixed.

Only after dev proof, have a separate approval to invoke the same operator workflow
against prod. Capture role/membership/default/view ACL state, execute least-privilege
cleanup with no proxy rollout necessary for pure metadata changes, verify janitor
tasks and UI functionality remain healthy, and record the return status separately
from runtime code/migration evidence. If the janitor breaks on unsupported future
partition behavior, restore only the targeted previously captured privilege needed
for it, not automatic unrelated table/view admin access.

Keep the accepted tag metrics and raw-log scope restriction (`/spend/tags` remains
unmodified) outside this follow-up. It is not permission to start another raw-log
workload or proxy app release.

## Follow-up execution record, 2026-11-24 (dev complete; prod pending explicit approval)

### Implemented operator command

`scripts/cleanup_spend_view_default_grants.py` (repo root), dry-run by default:

- connects with an operator-supplied superuser URL and verifies the session is
  superuser, both named roles exist and are not superusers themselves, the grantee
  is a member of the application role, the schema exists, and every same-named
  object is a view owned by the application role;
- refuses anything unexpected: a creator default ACL narrower than the broad
  signature (a deliberate narrow grant is left for manual review), a view whose
  grantee entries do not exactly match the default-ACL signature, entries granted
  by a third party, a non-view collision, a foreign-owned view, or missing
  membership;
- prints the exact planned statements in dry-run; `--apply` executes the default
  ACL removal plus the materialized view revokes in ONE transaction with
  `lock_timeout = 10s`, `statement_timeout = 60s`,
  `idle_in_transaction_session_timeout = 30s`;
- after commit verifies on a separate connection: the default ACL entry for the
  grantee is gone, no spend view carries a grantee entry, the application role
  still has SELECT on every view, and grantee membership survived. No `CASCADE`,
  no `GRANT ALL`, no blanket owner `REVOKE`, no row writes, no view recreate.

Targeted exactly: the `litellm`-creator `oicm`-grantee relations default ACL entry
and the matching materialized entries on the seven spend views. The separate
`postgres`-creator `litellm`-grantee row (partition conversion coverage) is out of
scope and untouched.

### Fixture regressions (disposable Postgres 16, three roles: postgres, litellm, oicm MEMBER OF litellm)

`tests/unit/proxy/db/test_cleanup_spend_view_default_grants.py`, 14 tests, all
passing:

- reproduction first: with the fork's broad default ACL installed, a view the
  application role creates materializes `oicm ALL` (the reported defect), and a
  tag-view drop/recreate re-materializes it (the default ACL row is the source);
- dry-run leaves the default ACL and the view entries untouched;
- `--apply` removes both, keeps `oicm MEMBER OF litellm` and the archive-ledger
  grant, and is idempotent (a second run prints nothing-to-do);
- after cleanup, recreating `DailyTagSpend` does NOT resurrect the grant;
- an explicit `dashboard_reader` SELECT on a spend view survives;
- the janitor's real workflow survives: reading the spend-logs table and writing
  the archive ledger still work through membership and the explicit ledger grant;
- refusals: narrow default grant (must not silently revoke a deliberate grant),
  foreign-owned view, table-where-view collision, missing membership, and a
  non-superuser session.

### Manifest source fix

Both janitor bootstraps lost their broad default-grant lines
(`deploy/prod/spend-logs-janitor/sql/bootstrap.sql`:
`ALTER DEFAULT PRIVILEGES FOR ROLE litellm GRANT ALL ON TABLES TO oicm`;
`deploy/dev/spend-logs-janitor/sql/bootstrap.sql`: the analogous self-grant to
`litellm`). The `postgres`-creator `litellm`-grantee partition-coverage line stays
in both. Removing the manifest line does not remove the existing database
default ACL; the operator command does that.

### Dev proof (2026-11-24)

Dev has nothing to clean from this defect: its cluster has no `oicm` role at all
(verified: `to_regrole('oicm')` is NULL), no `litellm`-creator default ACL row
(only the `postgres`-creator row, which is out of scope), and its janitor connects
as `litellm` directly. Its six views carry benign materialized `litellm=r/litellm`
self-entries from the old dev bootstrap line, harmless and intentionally left
alone (owner rights are implicit; the entries document nothing extra). The dev
bootstrap line is removed going forward so re-running the bootstrap cannot
re-materialize them.

Dev workflow proofs under the corrected configuration:

- dev janitor: job `spend-logs-janitor-29925379` completed
  (`SuccessCriteriaMet Complete`), log `no work; exiting`, the same healthy
  no-op cycle as before; the ledger shows historical full cycles
  (detach, dump, verify, drop) from 2026-09-23 with `litellm`-owned
  partitions throughout;
- partition ownership: zero non-`litellm`-owned partitions on dev;
- rollup writer pipeline: `LiteLLM_DailyTagSpend` has 82 rows for today
  (2026-11-24), so the writer pipeline is alive;
- dev gateway endpoints as admin: `/global/spend/models?limit=5` 200,
  `/global/spend/keys?limit=5` 200, `/global/spend/all_tag_names` 200,
  `/global/spend/tags?start_date=2026-11-17&end_date=2026-11-24` 200;
- a temporary `proxy_admin_viewer` user with a test key read all four spend
  endpoints 200 and had `/key/generate` denied 403; both the key and the user
  were deleted afterward (verified zero rows remain).

No proxy application code, view definition, migration, or gateway deployment
changed in this work. The changes are the operator script, its fixture tests,
the two janitor bootstrap manifests, and this runbook.

### Prod state awaiting the explicit execution decision

The prod preflight (2026-11-24, saved under the operator evidence directory
`~/adeo-prod-ops/grant-cleanup-2026-11-24/`) found exactly the expected state:

- `pg_default_acl`: one `litellm`-creator row granting `{oicm=arwdDxtm/litellm,
  litellm=arwdDxtm/litellm}` on future relations (objtype `r`), plus the
  out-of-scope `postgres`-creator row;
- `DailyTagSpend` carries the materialized `oicm=arwdDxtm/litellm` and a
  `litellm` self-entry; `Last30dTopEndUsersSpend` carries a `litellm` self-entry
  (from the repair's superuser-issued OWNER TO); the other five views are
  NULL-ACL;
- `oicm MEMBER OF litellm` (no admin option), `litellm` non-superuser CREATEDB,
  archive ledger/meta owned by `oicm` with self-ACLs, all 552 partitions
  `litellm`-owned, zero unfinished migrations.

The prod plan, on explicit approval only: dry-run the command against prod
(port-forward `adeo-litellm-postgres-rw`, superuser secret, in-memory password),
compare to the preflight, then run with `--apply`. The self-entry on
`Last30dTopEndUsersSpend` is NOT wrong and is NOT touched by the command: it is
the documented materialization of the owner's own rights after a superuser
ownership change, and the command only targets the `oicm` grantee entries. Then
verify janitor lifecycle health from the next scheduled run's logs, the four
public-ingress checks for admin and the existing Athena viewer, and no new
timeouts or permission errors in the proxy logs.

### Prod execution record, 2026-11-24 (explicitly approved)

Approved by the operator in-session after the dev-phase record was reviewed.

Pre-execution drift check (read-only, saved as `prod-drift-check.txt` in the
operator evidence directory): the catalog matched the preflight byte for byte,
the `litellm`-creator default ACL row, `oicm MEMBER OF litellm`, and the
`DailyTagSpend` materialized `oicm` entry all unchanged. No drift.

Dry-run then apply (both logs saved as `prod-dryrun.log` / `prod-apply.log`):

- dry-run planned exactly two statements: the creator default ACL revoke and
  the `DailyTagSpend` entry revoke; nothing else.
- `--apply` committed both in one transaction and its built-in verification
  passed: default ACL entry gone, no grantee view entries, `litellm` reads all
  views, membership preserved.

Independent pod-exec post-state (`prod-post-state.txt`): only the
`postgres`-creator default ACL row remains (out of scope, partition coverage);
`DailyTagSpend` now carries only its `litellm` self-entry;
`Last30dTopEndUsersSpend` keeps its benign `litellm` self-entry; the other five
views NULL-ACL; `oicm MEMBER OF litellm` intact.

Real privilege probes after cleanup, all true: `oicm` SELECT on
`LiteLLM_SpendLogs` (via membership), SELECT/INSERT on the archive ledger and
meta, CREATE on the database; a real `SET ROLE oicm` session could read the
spend logs and the rollup (`LIMIT 0` probes, no unbounded reads).

Janitor lifecycle: the 12:00Z run (`spend-logs-janitor-prod-29925360`,
pre-cleanup) completed its normal cycle; the first post-cleanup scheduled run,
`spend-logs-janitor-prod-29925480` at 14:00Z, also completed its normal cycle:
archive tick, no work, no permission errors (log saved as
`prod-janitor-1400z.log` in the operator evidence directory). Its only warning
line is the disk-pressure notice that the pre-cleanup 12:00Z run already
emitted, so it predates the cleanup. The janitor's connect path (`oicm` with
`litellm` membership) works unchanged after cleanup.

Public ingress acceptance (12:33Z): admin `/global/spend/models?limit=5` 200,
`/global/spend/keys?limit=5` 200, `/global/spend/all_tag_names` 200,
`/global/spend/tags?start_date=2026-11-17&end_date=2026-11-24` 200; the same
four 200 as the Athena viewer with inference denied 403. Proxy logs show no
permission-denied, undefined-column, or timeout errors; both replicas at zero
restarts.

No image rebuild, proxy rollout, secret rotation, config change, or row writes.
DB metadata changed: the `litellm`-creator `oicm`-grantee `pg_default_acl`
entry removed, the `DailyTagSpend` materialized `oicm` entry removed. Runtime
code unchanged.
