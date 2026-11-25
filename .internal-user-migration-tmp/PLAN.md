# Internal user identity and key ownership migration

Temporary workspace implementation. Only this plan and `migrate_users.py` are committed because commit-all was already requested, while the folder is temporary and must be removed later. Local evidence, handoff password files, generated keys, and snapshots are excluded through this folder's `.gitignore`. Remove the whole folder after the full migration and secure password handoff. Do not commit pilot plaintext password, UI session key, or full user-key snapshots.

## Pilot acceptance evidence (2026-11-25)

- Created exactly one new user, `vietth@ecouncil.ae`, ID `d6b12306-48e3-4532-8312-4efcb6782502`, global role `internal_user`, lowercase email, `auto_create_key:false`.
- Persisted `password_reset_required=true` independently verified with a read-only SELECT using the proxy's existing Prisma client. The chosen 13-character temporary password is in mode-0600 untracked `pilot-initial-password.txt`.
- Verified all three required team memberships and DEVELOPER member role `user`, and no initial generated keys. Original hashed-key identity, alias with trailing space, models and other key non-owner fields are identical to the precreation snapshot. Original key owner `default_user_id` is retained.
- Underlying database-backed password session correctly denies general `/v2/user/info` (HTTP 403), rejects wrong current password at `/user/password/change` (HTTP 400), and logs out via `/session/logout` (200 Session revoked). The reset flag remains true.
- **Browser acceptance blocker:** raw `/v2/login` returns a locally signed HS256 wrapper JWT with reset-required true; using that wrapper at standard Bearer authentication routes routes to Entra JWKS and fails HTTP 401 (`No matching public key found`, kid None) before route auth. Using the underlying generated password-session key directly works and is reset-limited. This existing auth-family bug prevents a standard dashboard user from completing the required reset. Do not elevate to proxy_admin, move the key, or clear reset-required as a workaround. Fix UI wrapper validation separately before full rollout.
- `--pilot` does not transfer keys; it verifies existing pilot state idempotently or creates the only new account, if new. `--migrate-pilot-key` is available but was not run. `--migrate-all-keys` is refused until explicit post-remediation approval.

## Migration state 2026-11-25

Production team `DEVELOPER` has 54 keys: 53 email aliases (49 new identities and 4 existing users), plus `V2V-DEV-SHARED` which remains untouched. All target accounts require `internal_user` role, default ADEOGPT-ENDUSERS and ADEOGPT-DESKTOP memberships, and explicit DEVELOPER user membership. Membership is reconciled, other memberships and roles are preserved, and existing accounts are never duplicated or assigned a new password.

Option 3 means normalize account emails with strip and lowercase. The existing key alias, including aliased case and whitespace, is not changed by this migration.

## Phases

1. Review the completed flight using `python3 .internal-user-migration-tmp/migrate_users.py --pilot`. This validates exactly one pilot user, zero unexpected extra keys, three teams, and password flag. It never moves existing pilot key ownership.
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
