# Spend Dashboard View Repair: Dev and Prod

Status: planned, dev-first implementation

Date: 2026-11-24

## Objective

Restore the LiteLLM Admin UI and Athena read-only spend analytics on both gateways without weakening authorization, duplicating spend data, changing spend rows, or introducing the large raw-log reads that previously contributed to Prisma-engine OOMs.

The production gateway currently returns HTTP 500 for:

```text
GET /global/spend/models
GET /global/spend/keys
GET /global/spend/tags?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD
```

The first two fail because the application DB role cannot read the `postgres`-owned views. The tag endpoint fails because the legacy `DailyTagSpend` view has three columns while the current code expects four current request-tag columns.

Both prod and dev run `jya0-v1.108.5` code for these queries. The defect is external database state left unrepaired by LiteLLM's existence-only view bootstrap, not an RBAC configuration change and not a fork lookup implementation deleted by the recent merge.

## Decisions

Use existing Prisma migrations as the recurring, versioned mechanism for definition and ownership. Add a small operator repair path for legacy databases because Prisma runs as the non-superuser `litellm` role and cannot borrow or grant the database owner or superuser privileges required to change an existing `postgres`-owned view.

Do not silently `ALTER VIEW ... OWNER TO litellm` merely because a role happens to be superuser. DB superusers can assume arbitrary roles: broad migrations that run at every boot must not create or repair persistent objects under an unexpected or opportunistically borrowed role. A wrong database owner is worse than a missing dashboard view.

The permanent contract is: the configured application role owns the seven ordinary spend aggregation views, with default ACL for that owner and no additional WRITE/DROP grants. `LiteLLM_VerificationTokenView` remains owned by existing application bootstrap, because it is also referenced during authentication. This migration touches only the following seven objects:

| View | Current purpose |
|------|-----------------|
| `Last30dModelsBySpend` | `/global/spend/models` |
| `Last30dKeysBySpend` | `/global/spend/keys` |
| `DailyTagSpend` | `/global/spend/tags` |
| `MonthlyGlobalSpend` | global daily spend |
| `MonthlyGlobalSpendPerKey` | key/date aggregation |
| `MonthlyGlobalSpendPerUserPerKey` | user/key/date aggregation |
| `Last30dTopEndUsersSpend` | top end-user spend |

Keep the model/key definitions and their existing 30-day time predicates. Query testing with daily partitions on dev confirms pruning of older partitions for these two views. They are not the unbounded 15M-row read described in earlier exploratory notes and should not be rewritten merely because their old definition differs from the table-based model-performance rollup.

Repair the tag view's contract in the recurring migration to restore HTTP 200:

```text
individual_request_tag
spend_date
log_count
total_spend
```

Do not make that ordinary view the final long-term tag lookup. Move both tag reads to the existing pre-aggregated `LiteLLM_DailyTagSpend` table:

- `/global/spend/tags` aggregates by `tag`, within `date >= start_date` and `date <= end_date`, returning log count from `api_requests` and spend from `spend`.
- `/global/spend/all_tag_names` obtains distinct `tag` values from this small rollup table rather than expanding `request_tags` across all retained spend partitions.

The writer confirms the table's `tag` rows originate from the raw spend row's `request_tags`, through `_common_add_spend_log_transaction_to_daily_transaction(..., "request_tags")` and `daily_spend_entity_ids(payload, "tag")`. This is the same tag domain as the legacy view. Grouping can collapse the table's key/model/provider/endpoint dimensions without changing data.

This eliminates the request-path scan problem on new tag reads and removes the ordinary `DailyTagSpend` view's only read consumer. Once that migration is confirmed on both gateways, drop this view in a subsequent named migration, and remove it from the fork bootstrap. The other six views still have consumers and are not deleted casually.

## Hard safety constraints

- No row rewrite, `UPDATE`, `DELETE`, `MERGE`, or `INSERT ... SELECT` in a Prisma migration. View definitions and DDL metadata only.
- No new rollup table or backfill for this bug. All necessary tag history was already accumulated by the existing writer.
- Avoid ACL mutation unless an abnormal grant must be removed intentionally. Reassigning view ownership with `ALTER VIEW` clears implicit access for the previous owner. If an arbitrary explicit ACL requires cleanup, test it in the fixture: `GRANT` then `REVOKE` can leave a non-NULL `{}` ACL, so a new owner may receive `permission denied` unless its own privilege is explicit. Prefer preserving the desired default ACL rather than inventing blanket revocation logic.
- On the repaired dev instance, `has_view_privilege('litellm', 'public."Last30dModelsBySpend"', 'SELECT')` was true while the older table-oriented convenience check returned false. Verify actual access as the application role (an authenticated role connection or `SET ROLE`) as well as inspecting relacl. Do not treat a single privilege helper as sufficient evidence.
- Never print credentials, tokens, connection strings, or JWTs. Read their values only in process memory. Do not modify secrets or rotate any key.
- Do not change startup to run a delete of `DailyTagSpend` at every replica startup. Recreate only on an actual, detected need.
- No automatic prod changes by a dev worker. The live prod cluster and database are read-only until explicit human approval after dev evidence.
- No speculating about 'before the version was fine': old branches contain the same lookup SQL. The latent DB state predates the v1.108.5 merge; no merge overwrite was identified for these paths.

## Design in detail

### 1. Versioned, non-superuser migration

Add a timestamp after the fork's newest Prisma migration in `litellm-proxy-extras/litellm_proxy_extras/migrations/`. Keep the exact other-six-view SQL aligned to `litellm/proxy/db/create_views.py` and `db_scripts/create_views.py`, including all WHERE, GROUP BY, ORDER BY, LIMIT, and alias details.

Use a PostgreSQL `DO` block to inspect `pg_namespace`, `pg_class`, and `pg_get_userbyid()`. For each of the seven names:

1. If the object exists as a view in the configured schema and is owned by `current_user`, correct it with `CREATE OR REPLACE VIEW` when the SQL form permits it.
2. For an incompatible `DailyTagSpend` owned by `current_user`, drop and recreate it inside the guarded block. No other views need drop/recreate to change aliases.
3. If an object has a different owner or an unexpected relkind, raise a clear, actionable exception identifying the object and requested owner. Do not try to borrow the role via `SET ROLE` from a superuser and do not hide the error in warnings.
4. Report the required operator repair path. Operator-supplied settings must be passed at that repair's explicit invocation, not read at migration execution from the DB connection string.

For an app role that owns public schema, a fresh install this gives owner = that role, relkind `v`, and default ACL. It must remain usable when belong_to role is CNPG owner but not a superuser. The migration should not assume the literal username `litellm`; the repair tool gets the exact role and database from the same mechanism the gateway uses.

A real positive test must demonstrate current-user ownership when connecting through Prisma's own migration tooling. Passing locally inside `psql` as `postgres` is not a valid ownership pass.

### 2. Explicit legacy-state repair job

Add a small admin runbook/command in the OICM layer for explicitly repairing a legacy environment once, as the CNPG superuser. Because it must run as `postgres` while the resulting views must go to the `litellm` application role, and because this may involve both dropping `DailyTagSpend` and OR REPLACEing other definitions, scope it carefully:

- input: the literal desired owner from a protected channel, or an in-memory extraction from the gateway's existing DATABASE_URL; never hard-code a password and never echo a connection string
- command-run identity: the CNPG superuser referenced by its own mechanism
- precheck: actual role exists and matches configured gateway credentials; expected schema and seven objects inspected
- `lock_timeout` short, `idle_in_transaction_session_timeout` bounded, and bounded global statement timeout. DDL should not scan a large table during its transaction, and lock wait must fail before interfering with request traffic
- plan: transaction `BEGIN`, verify desired owner again inside transaction, `DROP VIEW IF EXISTS "DailyTagSpend"`, recreate all seven using the same SQL source as future migrations, `ALTER VIEW ... OWNER TO <quoted desired role>`, default ACL preferred and checked, `COMMIT`
- do not `GRANT ALL` and do not execute a blanket `REVOKE ... FROM <owner>`; correcting ownership is the business requirement. Capture odd ACLs and handle them explicitly, with real role SELECT tests on fixtures
- save before facts outside the database: owner, relkind, SQL definitions, ACLs; never dump proxy master keys or full connection strings.

Keep one authoritative SQL definition for each view used by both the migration and this explicit repair. The regular migration must exercise its guarded DDL directly under the application DB role, without `SET ROLE`. The operator workflow is the only point allowed to use superuser capability to deal with foreign-owned views. Once it establishes the expected application owner, run the exact guarded migration SQL under an explicit `SET ROLE` to that owner within the same controlled transaction; if PostgreSQL does not permit the role membership or settings needed for this, fail rather than substitute a different owner. Record the `current_user` at each stage and use it for role ownership assertions. No ad hoc second copy of view SQL, no role chosen just because it happens to be available.

Because the script must **not** silently become an elevated migration, keep the guarded `DO` block logic non-superuser safe: it must reject mismatched ownership even when superuser, not decide to repair a wrong object because it can. Only the separate operator workflow should resolve legacy foreign ownership.

### 3. One authoritative definition set

`litellm/proxy/db/create_views.py`, `db_scripts/create_views.py`, and the migration should not retain three differently-maintained copies of SQL for the seven objects. Prefer generating legacy bootstrap SQL from the same module or rendering helper used to prepare the migration, with a test that compares view SQL through database introspection or normalized exact definitions.

Do not delete the whole boot-time bootstrap as part of this narrowly-scoped fix. Future fresh installs and unusual permission states still need correct handling and wrong-owner errors must not be swallowed as mere absence. The bootstrap's existence probe should fail fast or request operator repair on wrong ownership/columns, instead of treating view presence as health.

Leave global row-count gate `should_create_missing_views()` alone unless testing proves migration-based bootstrapping makes it redundant on every supported platform; removing that unrelated guard increases the blast radius of the fix.

### 4. Performance follow-up in the same controlled change

Change the UI/global tag backend to aggregate `LiteLLM_DailyTagSpend` within the caller date range. Preserve the endpoint's documented tag filter behavior:

- no `tags`, `all-tags` included: day-level rows or aggregate per tag, grouped as expected by the current response builder
- explicit CSV tag list: only those exact tag names
- return existing JSON contract `{"spend_per_tag":[{"name":..., "spend":..., "log_count":...}]}`
- no privileged role-specific data divergence unless the endpoint already enforces the same read scope on table rows. Preserve current viewer read access

Keep the legacy view migration as a compatibility bridge until its function/query call sites are removed, so older gateway replicas, older SDK users, and ongoing dashboards do not break during rolling availability window.

The term end-date is inclusive in the current tags endpoint; the existing table stores date as TEXT matched to `YYYY-MM-DD` on older strings. Do not compare lexicographically if this could be extended timestamps. Inspect and use the actual production values and representation; if needed compare only the date-field portion and index or constrain accordingly without an unbounded rewrite.

`/global/spend/all_tag_names` can deduplicate from the daily tag table. For correctness at bounded cost, iterate a projected list of tag/date keys or use an appropriate existing index; no unbounded raw-log JSONB expansion. Compare deduped output over a real populated window to the truth SQL used for the old response on an older prod copy. This does not introduce the raw scan to prod as part of ordinary smoking tests; run comparisons once in dev where the data and ops style are appropriate.

Do not make broad claims like "every legacy view scan scans 15M rows." Model/key use 30-day bounded filtering; DailyTagSpend and all_tag_names are the full-history problem.

## Dev execution plan

### A. Baseline, rollback data, and fixture

1. Fetch and confirm `jya0-v1.108.5` is up to date. A controlled docs move must not be overwritten. Record any concurrent edits before implementation.
2. Capture live dev view owner/relkind/ACL/definition for all seven plus `LiteLLM_VerificationTokenView`, migration status, and baseline route statuses with admin master-key identity.
3. Request/provide a dev `proxy_admin_viewer` key of its own, generated by a proxy admin (not another secret's master-key value). The prod Athena key returns 401 on dev; reuse is neither proof nor expected dev behavior. Use it only after setup, never commit it.
4. Restore prod-like fixtures in a **separate test schema** on dev (not the public live schema): the old tag view definition, foreign-owned/read-denied Last30* views, and populated base/daily-rollup data. This must prove the negative operator and migration behavior, not only verify already-fixed dev public views.

### B. Implement and test

1. Add the guarded Prisma migration with mismatched-owner refusal behavior. Include `DailyTagSpend` shape as a guarded drop/recreate when needed.
2. Add the explicit legacy-repair workflow with safe inputs and bounded timeouts.
3. Unify view SQL source and remove old duplicated/generated snippets only after their remaining consumers have been identified.
4. Add regression tests that show old schema shape and foreign ownership produce the same SQL failures, then repair converges them to correct definitions. Count assertions around mock call count are not enough: integration with actual Postgres privileges and view shapes is needed for a schema/ownership bug.
5. Verify both normal fresh-schema migration (actual application DB role) and guarded repair-lacking legacy case. The latter must fail clearly before changing external objects; the explicit repair must succeed only with delegated DB-admin authority.
6. Run existing mapped view-bootstrap tests and the mapped spend endpoint unit tests. Actual dev end-to-end probe must not substitute for meaningful regression tests, and unit mocks must not substitute for proof of owner access.
7. Build docs with `make docs` in `oicm-litellm-layer/` if docs/nav updated.
8. Run the focused mapped suites first, then `make check` once (machine-slot queuing is documented; read the saved .git log instead of re-running heavy checks for a snippet). Do not raise budgets or caps to silence failures.

### C. Runtime performance change and validation

1. Aggregate tag names and tag spend from `LiteLLM_DailyTagSpend`.
2. Re-run the exact three failing routes through a genuine dev gateway port-forward, with an admin key and dev viewer key. Because dev public views are currently already repaired, success here proves the endpoint behavior but not by itself the foreign-owner repair. Keep fixture proof separate.
3. Compare tag outputs before code switch (corrected legacy view) with table-based implementation for representative windows and explicit tag lists. For any period whose writer already produced historic data, totals/names/count must agree, including deci-precision considerations.
4. Inspect `EXPLAIN` with an IDENTICAL predicate window for the new queries. Must read the small daily table and prune appropriately, not scan `LiteLLM_SpendLogs`. Record actual query duration and response sizes.
5. Verify `EXPLAIN` for `Last30dModelsBySpend` and `Last30dKeysBySpend` retains pruning across the 30 days retention table. All output must use present user-safe paths, no secrets.
6. Restart/roll-out dev in a controlled way if code or migration shipped in image, then reissue forward and proofs.
7. Named cleanup migration for removing `DailyTagSpend` is prepared after the new tag query has proved itself, not executed on live DB before knowing there are no other external DB clients. The compatibility view may stay through prod until this sign-off.

### D. Dev acceptance criteria

- Reproducible legacy-state test fails before fix with old tag columns and foreign-owner permission errors, passes after authorized fix.
- Migration under application role succeeds on clean test schema with same requested owner/default ACL each of seven views.
- No foreign-owned repair is attempted by ordinary startup migration; the distinct operator tool completes it correctly after precheck.
- Exact three routes return 200 for both admin and correct read-only dev identity.
- Tag JSON preserves names, spend, log counts and filtering, within rounding tolerance expected by floating-point aggregation.
- `/global/spend/models` and `/global/spend/keys` stay 30-day bounded and return expected model/api_key rows.
- `all_tag_names` avoids the raw spend log entirely and returns present tag values from rollup.
- No prod secret, DB, deployment, or key was touched.
- Test/lint/results and committed branch/PR state recorded before requesting prod.

## Prod plan, executed only after dev sign-off

### 1. Freeze and assess

Explicitly authorize the environment and exact change. Schedule an off-peak maintenance window, confirm only intended dev/prod selected through current context, name the prod CNPG cluster and DB role and verify secret identities are not printed.

Capture actual owner, relkind, SQL definition, ACL, migration ledger, DB-side lock/transaction activity, and live route errors before touching anything. Store this evidence in an access-controlled location, not in chat or a PR. Save dev-proven branch/commit/image digest separately.

Validate that no tables or partitions depend on these views (`pg_depend`) before any drop, and abort on external DB dependencies requiring review. Ordinary views hold no data, but their dependencies and names must not be guessed.

### 2. Controlled backup, dry-run and repair

No table or spend-row backup is necessary solely for view DDL, but backup the complete view definitions, owner and ACL. No data rewrite is allowed.

Run only schema-level repair as `postgres` (CNPG superuser), after a read-only preflight including a rollback/version rehearsal. Use separate script-generated commands with desired role validated from current prod DATABASE_URL in memory and proper `%I` quoting in DO block or psql variable substitution. Do not interpolate credentials or role names unsafely.

Use a short `lock_timeout` and bounded `idle_in_transaction_session_timeout`. DDL changes should be metadata-only; a timeout fails closed without waiting indefinitely behind dashboard/inference transactions. Require an explicit gate if lock cannot be obtained fast enough; no patient retry loop over live traffic.

In one transaction:

- require expected DB namespace, expected schema, target-owner role and current state
- **legacy only** drop/recreate the three-column `DailyTagSpend`
- `CREATE OR REPLACE` the other six actual definitions, no data scans for correctness proof needed in this transaction
- `ALTER VIEW ... OWNER TO` actual proxy application DB role for all seven spend views
- inspect roles/grants prior to commit (no need for extra GRANT ALL). Do not revoke owner rights
- commit and probe with the app DB role (`SET ROLE` and supported query/privilege checks) before exposing routes as validated.

### 3. Deploy definition and tag read path

Deploy the dev-tested migration and image. Confirm migration is successful under prod `litellm` role and does no foreign-owner action. Confirm pending migrations and deployed digest.

Verify tag read uses `LiteLLM_DailyTagSpend`, returns the same representative window values as dev, and plan no raw-spend scans. If current tag code not yet switched when prod needs a hotfix, separate "restore endpoints by view ownership + shape now" from "tag optimization later" in deployment notes. Do not silently combine.

Check pod logs for `permission denied for view`, `column individual_request_tag does not exist`, statement timeouts, replica crash/migration errors, and request-path degradation. Verify not only DB direct SELECTs but actual gateway routes and UI representation.

### 4. Human reproduction steps

Use the existing proxy admin secret through approved channels, no value in artifacts:

```bash
K="$(kubectl -n adeo-litellm get secret litellm-master-key -o jsonpath='{.data.master-key}' | base64 -d)"
B="https://litellm.ecouncil.ae"
for p in \
  '/global/spend/models?limit=5' \
  '/global/spend/keys?limit=5' \
  '/global/spend/tags?start_date=2026-11-01&end_date=2026-11-24'; do
  curl -s -o /dev/null -w "$p -> %{http_code}\n" -H "Authorization: Bearer $K" "$B$p"
done
```

Use Athena key only via secure shell/environment injection; never commit it:

```bash
curl -s -o /dev/null -w 'Athena models -> %{http_code}\n' \
  -H "Authorization: Bearer $ATHENA_KEY" \
  'https://litellm.ecouncil.ae/global/spend/models?limit=5'
```

Check the Admin UI Usage/Tag pages with its real limit parameters and date range. Athena should see the same 200 and result shape as any authorized read parity user and still receive 403 on writes and inference.

### 5. Rollback

If only view DDL failed or read results wrong, stop at failing operation if possible and restore the captured seven view definitions, original ownership and ACL. Before restoring foreign ownership make sure owner role still exists. No table data touched so there is no row rollback. On timeout, identify blocking queries through `pg_stat_activity` before deciding any change; never bluntly cancel live statements solely to make a patch apply.

For code read regression, roll back to previously-running image with same DB-compatible compatibility views preserved until rollback complete. If new migration has viewpoint deletion staged in a later commit, do not run it in initial repair rollout. Schema rollback must explicitly restore the tag compatibility view before any old-image traffic needs it.

Any unresolved migration permissions failure stays an opaque startup error rather than choosing a fallback owner. Operations handles it with the pre-authorised legacy repair runbook.

## Dev execution record, 2026-11-24

Branch `jya0-v1.108.5`, plan executed per the worker prompt. All evidence below was gathered on dev or on a disposable local fixture; no prod connection, write, or key was used.

### What shipped

- `litellm/proxy/db/spend_view_definitions.py`: single authoritative DDL for the seven views. `create_views.py` bootstrap and `db_scripts/create_views.py` render from it, and `scripts/render_spend_view_migration.py` generates the migration from the same module with a `--check` drift gate, so migration, bootstrap, and script cannot diverge
- `litellm-proxy-extras/litellm_proxy_extras/migrations/20261124120000_spend_view_definitions_and_ownership/migration.sql`: guarded migration. Per view, a DO block resolves the catalog owner and relkind; a foreign owner raises with a pointer to the operator runbook, a non-view relkind raises, and only then CREATE OR REPLACE runs. `DailyTagSpend` is drop/recreated because CREATE OR REPLACE cannot add the `spend_date`/`log_count` columns
- `scripts/repair_legacy_spend_views.py`: operator-only repair for postgres-owned legacy views. Superuser precheck, local JSON snapshot of pre-repair state (no credentials), bounded lock/idle/statement timeouts, drop/recreate each view, ALTER VIEW OWNER TO the app role, then re-runs the shipped guarded migration under SET ROLE and SELECTs all seven views as the app role
- `litellm/proxy/spend_tracking/spend_management_endpoints.py`: `/global/spend/tags` and `/global/spend/all_tag_names` now read `LiteLLM_DailyTagSpend` with `date::date` bounds. Response JSON contracts unchanged. `/spend/tags` (the read-scope `get_spend_by_tags` path) is a different endpoint and was deliberately not changed
- `oicm-litellm-layer/docs/runbooks/spend-view-legacy-repair.md` plus nav/docs-map entries; `make docs` builds
- Tests: `tests/unit/proxy/db/test_view_definitions.py` (10, includes migration-matches-renderer drift gate and no-row-rewrite scan), `tests/unit/proxy/db/test_spend_view_ownership_repair.py` (9, real Postgres 16 fixture, skips without `SPEND_VIEW_TEST_PG_URL`/`SPEND_VIEW_TEST_PG_SUPERUSER_URL`), 3 additive endpoint SQL-shape tests in the mapped spend suite. Mapped suites: 283 passed across the four touched files

### Fixture proof (real Postgres, disposable container)

Five migration behaviors proven against a postgres:16 container with a non-superuser app role: fresh-schema create of all seven with app-role ownership and NULL ACL; idempotent re-run; foreign-owner refusal leaving catalog state untouched; superuser-with-SET-ROLE refusal (the guard checks `current_user`, not `session_user`); same-named-table refusal (relkind check). The operator repair script was then run end to end from a legacy state (postgres-owned views, 3-column tag view): exit 0, snapshot records the foreign owner, all seven views end app-role owned with relkind v, the empty-ACL trap (`relacl = '{}'`) is covered by a test proving owner SELECT fails there. The six pre-existing dev views carry `{litellm=r/litellm}` from the bootstrap GRANT; `DailyTagSpend` shows the migration's own NULL-ACL outcome. Both are owner-app-role and readable, which is what the guard enforces.

### Live dev proof (port-forward 14001, image `jya0-v1.108.5`)

`make litellm-src-release-dev` built, pushed, pinned, and rolled out dev. Boot logs show `Applying migration 20261124120000_spend_view_definitions_and_ownership ... All migrations have been successfully applied`. Catalog state after boot: all seven views owner `litellm`, relkind `v`. `DailyTagSpend` columns are `(individual_request_tag text, spend_date date, log_count bigint, total_spend double precision)`.

With the dev master key: `/global/spend/models?limit=5` 200 in 0.17s (372 bytes), `/global/spend/keys?limit=5` 200 in 0.16s (680 bytes), `/global/spend/tags?start_date=2026-11-01&end_date=2026-11-24` 200 in 0.17s (9720 bytes, top tag `User-Agent: opencode` spend 96.1 over 818 requests), `/global/spend/all_tag_names` 200 in 0.09s (23104 bytes, 370 names). A first viewer key attempt 401'd with `role=unknown` because the role lives on the user record, not key metadata; after creating a dev user with `user_role=proxy_admin_viewer` and issuing a key for it, all four routes returned 200 as that read-only identity. Both proof keys and the proof user were deleted afterwards and the deleted key re-checked as 401.

EXPLAIN ANALYZE, dev DB: the new tag queries Seq Scan `LiteLLM_DailyTagSpend` only (30ms, 3353 buffers, 2010 rows, no `LiteLLM_SpendLogs` access). `Last30dModelsBySpend` and `Last30dKeysBySpend` each scan 38 of 577 daily partitions, range `20261025 -> 20261201`, matching the 30-day predicate, in 20ms/15ms. Rollup-vs-raw spend totals agree exactly on the shared window; raw-log row counts differ only where the writer deliberately excludes internal calls and billed retry attempts, and the rollup extends further back (2026-06-25 vs 2026-09-22) because the dev janitor drops old raw partitions, which is why the rollup is the correct source.

### Test and lint state

`make check` runs end to end after fixing a local DNS sinkhole (`codeload.github.com` resolves to `6.6.6.6` here; `uv sync` needed the documented `20.233.83.150` pin, fetched via a temporary local CONNECT proxy with the tarball SHA-256 matching the lockfile pin). Its failures are all pre-existing branch-wide deltas against `origin/main` (Go toolchain at `/tmp/sa-build/bin/go` missing, 112 unformatted files in audio/openrouter_compat modules, TQ/OA/pyright rule ceilings from the 1272-commit branch history) and none name the files this change touches: my files pass ruff check, ruff format, basedpyright 0 errors on non-test files (the 261 pre-existing errors in `spend_management_endpoints.py` are identical before and after), and check-test-quality clean. The full log is `.git/pre_commit_lint.log`.

## Explicit ownership of remaining work

- Dev worker: implement/test guarded migration, explicit repair workflow, query source de-dup, bounded tag reads, local/live dev proof, docs, and commit/push. Do not touch prod.
- Reviewer/operator: inspect dev evidence, approve prod window, perform controlled backup/repair as DB admin, promote same validated code/image/migration without rebuilding, verify rollback and routes.
- Follow-up after runtime validation: compatibility `DailyTagSpend` view removal once no code/runtime client needs it and operation accepts external client risk; no need to block initial hotfix on removal.

## End worker prompt (copy verbatim)

Implement only the **dev phase** of `oicm-litellm-layer/docs/performance/spend-view-repair-plan.md` in `/home/jyao/ADEO/service/litellm`, branch `jya0-v1.108.5`.

Everything you need is in that plan and it is the controlling spec. Read it completely before editing, then read `oicm-litellm-layer/mkdocs.yml`, `docs/docs-map.md`, the owning deployment/performance docs, `litellm/proxy/db/create_views.py`, `litellm/proxy/spend_tracking/spend_management_endpoints.py`, `litellm/proxy/db/db_spend_update_writer.py`, and the existing mapped tests. Do not invent an independent design from earlier conversation summaries; if this spec conflicts with a remembered finding, re-check code and DB and surface the issue.

Your scope:

- Add a non-superuser Prisma migration covering the seven spend views in the plan, designed to fail explicitly on an existing foreign owner or non-view object rather than silently borrowing a different owner or overriding arbitrary objects.
- Add a separate explicitly invoked operator repair path for legacy `postgres`-owned views, using correct validated application role and bounded lock/statement/transaction behavior. It must not print credentials and must not include row writes.
- Prevent replication of divergent view SQL between migration and fork bootstrap, preserving the exact current model/key 30-day definitions.
- Change `/global/spend/tags` and `/global/spend/all_tag_names` to read the pre-aggregated `LiteLLM_DailyTagSpend` table, not expand raw `LiteLLM_SpendLogs`. Validate outputs against the corrected tag view for a real populated window, including names/spend/count and explicit filtered tags. Maintain the UI JSON contract and include `date` bound. No new table/backfill.
- Preserve the compatibility `DailyTagSpend` view for rolling upgrade; do not drop it on public dev DB simply because current endpoint no longer uses it.
- Add substantial regression coverage using real DB shape/privilege behavior for legacy view columns and ownership, not just mocked call counts. Tests should fail before your fix and after a deliberate mutation of owner/shape/query source. Include the tag path where it can regress back to raw scans. No external-vendor/date constants pinned as assertions.
- Test existing spend/view suites, docs nav/maps with `make docs` when changed, then run `make check` once if feasible and inspect its .git log. Do not silence gates or change budgets.
- Publish clear dev commands and evidence in the plan's new execution record or linked runbook: owner/relkind/ACL, migration behavior, exact endpoint result status/shape, EXPLAIN plans showing no raw spend scans for tag queries and 30-day pruning for the existing model/key views, query duration/result comparisons (without caller secrets).
- Ensure any dev viewer key used for proof is explicitly issued by an admin for dev, never copied prod credentials. The prod Athena key returns 401 against dev; don't call it a failure of this fix.
- Do **not** access prod for writes, do **not** execute the operator repair job against prod, do not rotate any key or secret, do not create/drop public objects outside the planned dev test schema. Read-only dev/ordinary local inspection is allowed. Do not advance prod rollout.
- Commit and push completed dev work when done following conventional commits. Summarize what is and isn't proven and ask for explicit prod approval separately; don't treat plan creation or endpoint-200 alone as prod signoff.

One known trap: `REVOKE ALL ... FROM <owner>` on a view creates an empty ACL `{}` and revokes implicit owner access, while intended state is default `relacl IS NULL` with app role ownership. Test actual role SELECTs, not only a superuser `has_table_privilege()` result. Also no assumption that all six Last30/Monthly views scan whole history: the new tag path is specifically intended to eliminate the full-history raw scans; models/keys already contain a 30-day bound.

Treat a disagreement with this plan about whether a Prisma migration can run privileged owner repair as an implementation question to resolve in code, not as permission to broaden startup DB privileges. Test real execution roles. Surface any **actual** current-user migration conflict honestly rather than inventing a fallback owner.

When finished, return changed files, test commands/results, live dev reproduction evidence without credentials, exact prod commands you deliberately did **not** run, and blocking issues.