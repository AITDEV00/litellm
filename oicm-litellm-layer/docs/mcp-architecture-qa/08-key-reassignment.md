# Can an existing API key be reassigned to another user account?

Short answer: **yes for a proxy administrator within request validation.** Team/organization administrators may reach scoped key updates, but they are still subject to the ownership-rebind guard and route checks. Do not infer transfer permission from membership alone, and do not use retyping a key as a normal offboarding operation. Deleting and reissuing a scoped key with an explicit destination owner/team is clearer

The source guard shown below means changing `user_id` between different users requires `proxy_admin`. It is not correct to generalize "non-admin" into every organization or team administrator without considering that exact guard. Conversely, passing this guard alone does not authorize arbitrary key changes.

## The mechanism

`POST /key/update` accepts `user_id` (and `team_id`, `agent_id`, `organization_id`) as merge-patch fields. Send the new `user_id` and the key is rebound.

- Handler: `update_key_fn` in `litellm/proxy/management_endpoints/key_management_endpoints.py` (route declared at line 3623).
- Model: `UpdateKeyRequest` in `litellm/types/proxy/management_endpoints/key_requests.py`, extending `KeyRequestBase` -> `GenerateRequestBase`, which carries `user_id: str | None = None`, `team_id`, `agent_id` (`request_base.py` lines 34-36).
- Persistence: `prepare_key_update_data` calls `data.model_dump(exclude_unset=True)`, drops only `key` / `new_key` / `grace_period`, and everything else including `user_id` flows into the `non_default_values` update on the token row. So `user_id` **is** written to the database on update.

Request shape:

```json
POST /key/update
{"key": "<the key or its hash>", "user_id": "<new user id>"}
```

or identify by alias: `{"key_alias": "...", "user_id": "..."}`.

## The guard: non-admins cannot rebind

`_validate_caller_can_change_key_ownership` in the same file exists solely to stop this. Its docstring explains the exact attack:

> a non-admin rebinding their own key's `user_id` to a `PROXY_ADMIN` row promotes themselves.

So:

| Caller | Rebind `user_id` | Detach `user_id` (explicit null) |
|---|---|---|
| `proxy_admin` | allowed | allowed |
| anyone else | `403 Non-admin caller is not allowed to rebind the key from user=A to user=B` | `403 Non-admin users cannot remove the user_id from a key` |

Omitted `user_id` is a no-op, because the check distinguishes "not in `model_fields_set`" from "explicit null". This was added to `/key/update` and shared with `/key/regenerate` so a future regenerate-style endpoint cannot miss it.

## What actually changes when you reassign

`user_id` is not just a label. From the same docstring, `_return_user_api_key_auth_obj` resolves the token's `user_id` against `litellm_usertable` to derive the request's role. So rebinding changes:

- the effective role on every subsequent request (which is why the guard exists),
- team-derived grants if `team_id` also changes, since team object permission is resolved from `team_id`,
- BYOM creator visibility, which keys off the owner user id,
- spend attribution going forward (historical spend on the token row is not rewritten).

Rebinding does **not** automatically copy the new owner's object permissions onto the key. The key carries its own `object_permission_id`; if you want the new user's MCP/team grants to apply, either move the key into the new user's team with `team_id`, or attach the desired access groups explicitly. Reassignment moves ownership, not entitlements.

## What it does not do

- **It does not regenerate the secret.** The bearer string stays the same, so whoever held the old key still holds it. If the point is to hand access to a different person, you want `/key/regenerate` (new secret, optional grace period) rather than a rebind, or rebind plus regenerate.
- **It does not move spend history.** `spend` on the token row is a running total; rebinding does not split it across owners in past logs.
- **It does not re-validate team membership.** Messages and other team scoped features will follow the new `team_id`, but that the new user is actually a member of that team is your responsibility to keep consistent.

## Can historical usage be reattributed deliberately?

**Yes, but only as a separately authorized, audited accounting correction.** Spend rows retain the `user_api_key_user_id` snapshot from request time. Reassigning ownership changes identity for new traffic but does not modify those existing rows. For keys whose earlier shared-admin ownership was an unintentional arrangement, each historical row's `api_key` hash can be joined to a verified original-key-to-personal-UUID mapping.

The default remains to preserve historical attribution. Changing earlier accounting is a decision the operator must make deliberately, with a documented reason, evidence that the affected key belonged to the intended person throughout the selected period, and preserved recovery data. It must not be inferred merely because a key was recently transferred.

A complete correction must handle more than `"LiteLLM_SpendLogs"."user"`:

| Area | Required treatment |
| --- | --- |
| Selected request rows | Reassign only rows belonging to an approved original token hash and time range. Preserve the original user snapshot, corrected owner, correction identifier, and rollback snapshot as auditable provenance; do not convert these into new requests |
| User counters | Apply an attributable delta to the destination user. Do not overwrite genuine use on their other keys. Reconcile any decrement to the former counter, budget-duration resets, and manual adjustments before making it |
| Team-membership usage | Move matching member deltas to the correct `(user_id, team_id)` row, with the same reset and other-key caveats |
| Organization-member usage | Reconcile the matching `(user_id, organization_id)` delta only where an organization attribute existed; do not change the organization itself |
| Daily per-user aggregates | Move spend, token counts, API/success/failure counts, response times, and other user-attributed dimensions into the recipient's correct date/key/model/provider/endpoint bucket. Respect the existing unique key and merge already-present buckets |
| Other dimensions | Preserve `team_id`, `organization_id`, end-user, tag, agent, token, model, timestamp, response, and immutable total provider spend. A key ownership correction does not justify changing these unrelated dimensions |
| Runtime caches | Refresh or invalidate cached user and budget-window spend after the correction, since reports and enforcement otherwise see stale balances |

A Prisma schema migration is not the place for a spend-log bulk rewrite. Migrations run synchronously at proxy boot, and only schema changes belong there. Use a separately operated, transactional job using the gateway's established database connection pattern. It should be idempotent and understand that admin fallback and previously personal rows can coexist on one migrated key.

The rollout sequence is:

1. Freeze an exact key-hash-to-owner manifest and cutoff. Restrict scope to those original hashes; never reassign a shared service key or all `default_user_id` rows globally.
2. Produce a read-only impact report of affected rows, spend, tokens, request counts, already-personal records, existing destination counters, and budget resets. A large-range or ORM client aggregate can exceed the Prisma HTTP engine timeout. Use direct PostgreSQL execution with bounded timeouts and key-indexed batches, rather than treating an HTTP timeout as absence of data.
3. Create protected snapshots sufficient to restore user attribution and affected counters/rollups exactly. Do not log plaintext bearer tokens or dump sensitive request bodies into the manifest.
4. Test the dry-run and recovery on a staging copy, then execute the approved batches in one explicit maintenance operation. Do not blindly retry unknown transaction outcomes.
5. Reconcile log users, user/member counters, daily buckets, caches, budget windows, and new request attribution. Keep the correction audit trail even if the old request-time identity is replaced in normal reports.

For a report-only correction, an alternative is an audited attribution map in the reporting layer, leaving accounting history and budget counters intact. This is preferable when historical assignment should not affect current enforcement. It must not be confused with rebuilding the stored attribution itself.

## Practical guidance for this deployment

For reassigning one of the loose keys found in the audit, the safe order is:

1. Decide the destination owner and team.
2. `POST /key/update` with the new `user_id` (and `team_id` if it should move teams).
3. If the old holder should lose access, `POST /key/regenerate` so the old secret stops working, or delete and mint a fresh scoped key.
4. Confirm with `GET /key/info?key=<hash>` that `user_id`, `team_id`, and `allowed_routes` are what you intended.

For the two unlabelled bare keys specifically, minting a new properly scoped key and deleting the old one is cleaner than rebinding, because rebinding would carry the empty `allowed_routes` and empty `models` along with it.

## Source files

- `litellm/proxy/management_endpoints/key_management_endpoints.py` `update_key_fn`, `_validate_caller_can_change_key_ownership`, `prepare_key_update_data`
- `litellm/types/proxy/management_endpoints/key_requests.py` `UpdateKeyRequest`, `KeyRequestBase`
- `litellm/types/proxy/management_endpoints/request_base.py` `GenerateRequestBase.user_id/team_id/agent_id`
