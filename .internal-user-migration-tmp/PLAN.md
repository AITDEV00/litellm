# Internal user identity and key ownership migration

Temporary workspace implementation includes `migrate_users.py`, the account-only `rollout_accounts.py`, and this handoff record. Local evidence, journal, handoff password files, generated keys, and snapshots are excluded through this folder's `.gitignore`. Remove the whole folder after the full migration and secure password handoff. Do not commit plaintext passwords, UI session keys, or full user-key snapshots.

## Pilot acceptance evidence (2026-11-25)

- Created exactly one new user, `vietth@ecouncil.ae`, ID `d6b12306-48e3-4532-8312-4efcb6782502`, global role `internal_user`, lowercase email, `auto_create_key:false`.
- Persisted `password_reset_required=true` independently verified with a read-only SELECT using the proxy's existing Prisma client. The chosen 13-character temporary password is in mode-0600 untracked `pilot-initial-password.txt`.
- Verified all three required team memberships and DEVELOPER member role `user`, and no initial generated keys. Original hashed-key identity, alias with trailing space, models and other key non-owner fields are identical to the precreation snapshot. Original key owner `default_user_id` is retained.
- Underlying database-backed password session correctly denies general `/key/info` (HTTP 403), rejects wrong current password at `/user/password/change` (HTTP 400), identifies its intended owner via `/management/v1/users/me`, and logs out via `/session/logout` (200 Session revoked). The reset flag remains true.
- The raw `/v2/login` HS256 wrapper JWT is **not** the Bearer credential for ordinary gateway management calls; sending it directly to Entra-backed authentication fails with 401 (`No matching public key found`) as expected. The browser unwraps its `key` claim in `AuthContext.tsx` and sends that inner password session to self-service password-change routes. The initial browser-blocker conclusion was wrong; the observed token family difference does not block the dashboard's intended reset flow. Password changing was intentionally not completed during this reset-required handoff phase.
- The pilot did not and must not transfer key ownership. `--pilot` only checks the pre-existing account/state and related defaults; remaining rollout uses the account-only `rollout_accounts.py`, which has no key-reassignment option.

## Migration state 2026-11-25

Production team `DEVELOPER` has 54 keys: 53 email aliases (49 new identities and 4 existing users), plus `V2V-DEV-SHARED` which remains untouched. All target accounts require `internal_user` role, default ADEOGPT-ENDUSERS and ADEOGPT-DESKTOP memberships, and explicit DEVELOPER user membership. Membership is reconciled, other memberships and roles are preserved, and existing accounts are never duplicated or assigned a new password.

Option 3 means normalize account emails with strip and lowercase. The existing key alias, including aliased case and whitespace, is not changed by this migration.

## Completed production account rollout (2026-11-25)

- All **53** normalized email identities have a unique matching production account with global role `internal_user`. All three required teams (`ADEOGPT-ENDUSERS`, `ADEOGPT-DESKTOP`, and `DEVELOPER`) contain the account with member role `user` and matching team-membership rows.
- **49** accounts are newly created, including the pilot. Each has the same shared initial password stored only in the protected local `shared-initial-password.txt`; each still has `password_reset_required=true`. No extra automatic keys were created.
- Exactly the four originally matching accounts `mohamed.hamouda@ecouncil.ae`, `jyao@ecouncil.ae`, `dqvu@ecouncil.ae`, and `ffkhan@ecouncil.ae` were reused. No password was set or reset for them by this migration. `ffkhan` retains its pre-existing forced-reset flag.
- A complete read-only `rollout_accounts.py --verify` passed for all 53 accounts, actual team roles and rows, key owner, original alias/casing/whitespace, hashed token identity, models, permissions, policies, budgets, team, and other non-runtime key configuration.
- Independent raw Prisma owner comparison of the original hashes matched all 53 original owners: 49 `default_user_id` and 4 `null`. `/key/list` may represent a null owner as `default_user_id`; raw persisted values are authoritative and were unchanged. Normal `last_active`, `spend`, and `total_spend` counters can advance with real concurrent traffic and are not static identity/configuration.
- **No key transfer or API-key rotation took place. Key ownership reassignment remains a separately authorized later phase.** `V2V-DEV-SHARED` remains untouched.
- The temporary shared password file is deliberately retained until human handoff. Hand it off through the approved out-of-band channel: each of the 49 new users uses the same initial password and must replace it at first login. Do not print it into chat, commits, tickets, or shared logs. Delete local credential files and this temporary directory only after all recipients have changed passwords and the later authorized key-transfer phase is complete.

## Key ownership transfer plan; planning only as of 2026-11-25

**Status: no `/key/update` ownership mutation has run.** This phase requires explicit pilot approval and a second explicit remaining-batch approval before production writes. Do not treat a planned test as authorization to execute it.

### Exact scope and retained state

- Target exactly the 53 original email-alias keys recorded in ignored mode-0600 `initial-key-snapshot.json`, one per normalized identity. Bind each to the already-verified `internal_user` account UUID, derived from the normalized identity and separately re-read at execution.
- Current protected rollout journal has **49** raw owners `default_user_id` and **4** raw owners `null`; an independent raw Prisma comparison matched those original owners for all 53. `/key/list` presents null as `default_user_id`; retain the exact raw original owner in the rollback manifest. `V2V-DEV-SHARED` remains excluded.
- All 53 keys remain on `DEVELOPER`, team ID `05cc86a3-37e0-4cd2-a25c-51a66c6d61a7`. 52 advertise `llm_api_routes`; `maalmarri@ecouncil.ae` has an empty route list. Source `RouteChecks.is_virtual_key_allowed_to_call_route` treats an empty list as unrestricted route access. Test its actual non-admin authorization behavior, not an assumed denial or unconditional inference success.
- Owners' current auth-derived parameters were captured. 52 user records have an empty global `models` field; `ffkhan@ecouncil.ae` retains its pre-existing `no-default-models` restriction. Do not weaken or overwrite it to make a test pass. No target global budgets or model-specific budget restrictions were found in the preflight.
- Key model grants are heterogeneous explicit lists in 41 keys, `all-team-models` in 12. There are embedding, OCR, image, speech/TTS, and ASR grants; a chat completion cannot prove every capability. Plan per-key static-grant verification for all 53 plus representative actual bearer tests by capability surface.
- Protected ignored mode-0600 `key-transfer-baseline.json` maps each original hash to exact normalized identity, target UUID, original raw owner, original key configuration, and original owner-user auth ceilings. It is the required before/after and rollback snapshot. Do not mount it, disclose its hashes, or commit it.

### Mutation contract

1. One record at a time, `POST /key/update`, proxy-admin operator credential, body **exactly** `{"key": <original_token_hash>, "user_id": <target_account_uuid>}`. The API's `exclude_unset=True` preparation makes the meaningful DB patch only `user_id`.
2. Never submit `key_alias`, `team_id`, `models`, `metadata`, `policies`, `permissions`, `object_permission`, `object_permission_id`, budgets, spend, expiration, routes, access groups, or guardrails when omitted. Nulling metadata explicitly would clear it; do not send a full resource object back. Do not regenerate, rotate, block, or delete keys. Do not use raw SQL or `/key/bulk_update`.
3. Omitted native fields retain stored values, including team assignment, exact model-lists/order, rate limits, object permissions, custom route and budget settings. The API writes no default user model restrictions onto the key. `/key/update` invalidates the key auth object and broadcasts eviction best-effort across workers; verify convergence through two pinned per-pod read-only logs/views or synthesized token-object read checks after transient cache TTL, not direct /key/update writes. There is no supported per-pod admin REST method proving this just by a random public repeat. It does not separately invalidate owner-user, team membership, or budget caches; production Redis health and convergence are preflight gates.
4. API ownership change is permitted only to proxy admin. It does not validate destination membership, so the driver must require the immutable UUID, normalized email, global role, actual `DEVELOPER` member role and matching membership row both before and after the call.
5. Changing owner affects request-time owner role, owner metadata, and billing attribution, not the stored `team_id`. The new owner/internal-user defaults do not get granted `all-models`; key/team restrictions remain. For team-key usage, global `models` restrictions apply only if the team is absent, but model-specific budgets/rate limits and matching team member scopes still matter. Source deployment config does not set `apply_user_budget_to_team_keys` (normal default false). Verify effective live config during execution preflight with a supported admin/settings read; GET `/config/yaml` is invalid in this version and cannot substitute for that evidence. If it is true, stop and reassess per-user limits first.

### Execution gates

1. **Read-only preflight:** re-run complete account audit; assert all 53 candidate aliases, hashes and target UUIDs still unique and exact; compare original key static fingerprint; ensure no target owns a different key; confirm every required membership row; confirm no target global role, scope, budget or rate-limit change except explicitly accepted pre-existing state; ensure current DB configs/table scans are healthy. Save the existing ignored baseline is immutable and require exact match at run start. No metadata in the manifest means none of this implies unconditional inference success.
2. **Review output without writes:** driver defaults to preflight/dry-run and only composes the 53 exact two-field calls. It must reject unexpected candidate-count/hash/user/team/config differences. It has no default "apply" behavior and no random key IDs. Choose pilot `vietth@ecouncil.ae` only by exact existing alias/hash and target immutable UUID; include the non-transfer `V2V-DEV-SHARED` negative assertion in every count.
3. **Pilot gate, only after your explicit authorization:** transfer just vietth's key; record pre/post immutable config, confirmed `updated_by`, original/new owner, original raw owner, and outcome in a local ignored 0600 journal. Require every static field except owner/audit/update timestamps to remain exact; require `/user/info` to show the one intended key with the correct owner and inspect the public two-pod routing/cache behavior with no substitution for a same-bearer authentication proof on each worker. No new automatic keys or password resets. Historical key/team/user spend is not rewritten, and ongoing spend is allowed to rise. New entries should attribute to the personal owner, not `default_user_id`; inspect the representative new request/spend view.
4. **Live auth proof:** obtain a protected local `vietth` plaintext key from an authorized existing client/secret-manager location. Its stored hash can only target reassignment; it cannot recreate the bearer token and must not be replaced by the master key. Test original bearer against `/key/info` or the supported self-service identity route, `/v1/models`, and one low-token actual `/v1/chat/completions` only when allowed by its preserved grants. Confirm intended model is accepted and an unauthorized route/model is denied. For representative affected non-chat owners, add actual low-cost embedding/image/speech compatibility requests only if the corresponding pre-existing key/capability was demonstrably usable. `ffkhan` is a separate explicit policy-sensitive acceptance case.
5. **Pause and report after one pilot.** No batch continuation without an explicit user go decision. If pilot proves loss of working model/permission surface, restore `user_id` to exact original raw `default_user_id` through the same two-field API update, invalidate again, verify original owner/config and stopped failure. Raw-null owners require the special recovery gate below. No inferential "no action needed" claim: first raw-read actual owner to see whether an update committed behind a failed caller, then reconcile or roll back once. Network unknown commit outcomes never safely mean retry.

### Raw-null ownership recovery gate

- Four references in the immutable baseline have original raw owner `null`: `dqvu@ecouncil.ae`, `jyao@ecouncil.ae`, `mhserry@ecouncil.ae`, `naalkhazraji@ecouncil.ae`. Proxy-admin `/key/update` **can** explicitly restore exact raw null with `{"key":<hash>,"user_id":null}`. Source `_validate_caller_can_change_key_ownership` permits proxy admins, while ordinary non-admin callers cannot clear ownership. Preparation `model_dump(exclude_unset=True)` retains supplied null. Do not use DELETE, regeneration, raw SQL, or a full key object for this recovery.
- After any explicit-null rollback require a read-only raw Prisma owner comparison and route-auth behavior; `/key/list` may present raw null as `default_user_id`, so a UI label/API owner is not sufficient proof. Never merely substitute `default_user_id` for a raw-null owner during rollback; that grants a different effective admin identity.
- The forward pilot's original owner is non-null, giving a directly reversible two-field rollback path. The four explicit-null rollback examples are kept grouped and handled last only with their original baseline owner and corrected null-not-default verification.

### Read-only driver and verification commands

- `python3 .internal-user-migration-tmp/key_transfer.py`: read-only full account/key/owner preflight and static-config equality check, requiring the existing mode-0600 original baseline. It never sends a mutation.
- `python3 .internal-user-migration-tmp/key_transfer.py --verify`: post-transfer strict 53-person owner/static-key invariant check and attached-key set check. It additionally requires a fresh `key-transfer-raw-owner-evidence.json` protected Prisma checkpoint; do not reuse the pre-transfer `raw-owner-readback.jsonl` as proof that a later transfer worked.
- The eventual transfer driver remains separate and gated on `--pilot` versus `--all-after-approved-pilot` with a lock/journal. Not generated or enabled before your approval. Importing or running the account/preflight components cannot update key ownership.

### Batch transfer and acceptance

1. After pilot acceptance and explicit "go for the remaining 52", process in deterministic candidate order, one writer and lock file, serially. Persist pending request before each call, then result/verification afterward. Reconcile unknown outcome read-only first; do not blindly replay a non-idempotent external call. Resume skips keys whose target is already verified without changing any other state.
2. After every assignment and final audit require:
   - 53/53 exact stored hashes have exactly their intended user UUID, remaining under same `DEVELOPER` team and exact alias/casing/whitespace;
   - zero email-shaped target keys belonging to `default_user_id` or raw null; `V2V-DEV-SHARED` still not personally assigned;
   - no extra personal keys, no raw-key regeneration, no user passwords/roles/default teams altered, no spend deleted or manufactured;
   - exact all 53 unchanged static key fingerprints (including permissions, policies/object permissions, budget IDs, resource scopes, models, route controls), ignoring only runtime `spend`, `total_spend`, `last_active`, `updated_at`, `updated_by` documented separately;
   - `V2V-DEV-SHARED` stays in 54-key team inventory alongside the 53 moved ones, new overrides only `user_id`/standard `settings_updated_at`/`updated_by`;
   - raw owner reconciliation via Prisma matches expected mappings for all 53; public reads and same-bearer authentication on both workers converge after built-in cross-worker invalidation/TTL. Do not mutate raw db for propagation or use an assumed unsupported audit REST.

### Rollback and overall safety

- Decide per key from its recorded original raw owner. If original owner is non-null, rollback uses exactly `{"key":<hash>,"user_id":<old_owner>}`; if null, proxy-admin rollback uses exactly `{"key":<hash>,"user_id":null}` then verifies persisted null. Stop the batch immediately upon failed verification/target mismatch/metadata drift/auth regression/persistent DB problems. Inventory all completed updates with exact counts and do not declare failure without stating what stayed transferred.
- A rollback restores ownership metadata and effective admin privileges but never reverses newer completed requests/spend, nor revokes existing client tokens. It is not a key-secret leak recovery. Static configuration must still compare faithful to baseline except documented updated audit fields.
- Evidence contains no bearer tokens, master keys, login tokens or readable initial passwords. After final audit and instructions, distribute the 49-user initial password securely, record owner/behavior acceptance, then remove ignored snapshots, journals and temporary workspace after successful migration/handoff and when there is no remaining need for rollback metadata. Complete deletion of raw credential material remains explicitly separate from DB retention/backup lifecycle.

## Phases

1. Review the completed pilot account using `python3 .internal-user-migration-tmp/migrate_users.py --pilot`; the approved full account state and audit use `python3 .internal-user-migration-tmp/rollout_accounts.py --verify`. Reruns never assign password to originally pre-existing accounts.
2. `/user/new` created exactly one pilot account (vietth), explicit three-team memberships, `auto_create_key:false`.
3. Use a protected local admin-generated temporary password input. `/user/update` stores the hash and automatically sets reset-required true. No raw password appears in the HTTP log, manifest, command line, or script.
4. Verify identity with `/user/info`, team with paginated `/key/list`, memberships via `/team/info`, pre-change reset-required via read-only database SQL, password session via `/v2/login`, and that the password-change session read correctly identifies reset-required. No actual change is performed; cleanup temporary UI sessions.
5. Present result before creating other accounts.
6. Following separately approved rollout, provision other missing accounts and reconcile all four existing accounts. Set password only for new accounts.
7. Only after separate authorization migrate keys using their immutable hashed token and `/key/update` with `user_id` only. Held `DEVELOPER` team, grants, budgets, aliases, spend, block status, and token identity must compare unchanged. Post-transfer real model proof goes through an allowed model. Existing history is not rewritten. Shared V2V key excluded.
8. Remove temporary directory and local credentials after owner handoff and final reconciliation. Do not delete accounts automatically.

## Reconciliation and rollback

Save context-only identity/key snapshots before the mutation, such as normalized alias, hashed token, user id, team id, and non-secret key permission fields. Never log literal passwords, UI tokens, or sk-secrets. A failed partial account provisioning iterates only supported missing membership repair and reruns account flags. Key reassignment is separate gated mutation and must not be triggered for the pilot. Rollback ownership to the recorded owner only after traffic pause and reconciliation. Do not erase historical spend from old users to stitch together rows.

## Required controls

- Exact prod team ID: `05cc86a3-37e0-4cd2-a25c-51a66c6d61a7` (`DEVELOPER`, singular)
- Lowercased valid and unique emails, no accidental user matching or alias-based update.
- Protected operator credentials using configured cluster admin source, not a connector key.
- Session-based password route only. Enterprise/browser sessions are different credentials from configurable DB-backed user passwords.
- Stop on unexpected role, candidate changes, team-add failure, mismatched key attributes, or failure of end-to-end ownership checks.
- Raw databases connections only read-only. No SQL write is necessary for this flow.
