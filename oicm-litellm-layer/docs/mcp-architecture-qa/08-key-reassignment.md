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
