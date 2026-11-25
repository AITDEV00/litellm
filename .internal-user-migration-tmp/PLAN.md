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
