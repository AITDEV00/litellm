# Session Identity Resolver - Implementation Plan

Goal: when a client sends no stable session id, infer one from the conversation
so that `DeploymentAffinityCheck` can pin the conversation to the replica that
already holds its vLLM prefix KV cache. LiteLLM stays the only router; the
resolver only stamps `metadata["session_id"]`.

Upstream references (fetched, pinned in SOURCE_MANIFEST.json):
- vLLM Router PR #217: exact-session map, full-history fallback, teaching, TTL
- llm-d #1980 / fork PR #14: framed content stream, 512-byte chunk chain,
  alias keys, longest-prefix index, LRU lifecycle (Go, to port to Python)
- SGLang #34513: agent-aware RFC only (no public implementation); public
  tree.rs / cache_aware.rs are mechanics references for the discriminator

Local copies: `oicm-litellm-layer/downloaded_sources/` (regenerate with
`download_sources.sh` from the bundle zip).

## Architecture

```
request
  |
  v
SessionIdentityResolver(CustomLogger).async_pre_call_hook     [proxy layer]
  | 1. client metadata.session_id present?        -> pass through, no-op
  | 2. SESSION_ID_GENERATED_METADATA_KEY set?     -> pass through (policy already ran)
  | 3. recognized session/vendor header?          -> pass through (affinity uses it)
  | 4. prompt_cache_key / conversation body field -> alias id (llm-d declaredID)
  | 5. hash-chain lineage match (Redis)           -> inferred id
  | 6. nothing found                              -> no-op (normal routing)
  |
  v  stamp metadata["session_id"] (+ SESSION_ID_INFERRED_METADATA_KEY)
DeploymentAffinityCheck (existing, unchanged)
  |  pin hit  -> same replica
  |  miss     -> usage-based-routing-v2 -> pin created (existing code)
```

## Verified integration facts

- `async_pre_call_hook` runs at `common_request_processing.py:2004`, before
  `route_request` -> `Router.acompletion` -> deployment filtering
  (`router.py:12934`). Stamped metadata is visible to the affinity filter.
- The hook receives `cache: DualCache` which in prod is
  `user_api_key_cache` with Redis attached
  (`proxy_server.py:4531`, gated on `enable_redis_auth_cache: true` -
  already set in prod yaml line 60). Inferred-lineage state in this cache is
  shared across LiteLLM pods with zero extra wiring.
- The hook runs once per proxy request, not on router retry loops.
- `apply_missing_session_id_policy` (`litellm_pre_call_utils.py:758`, called
  :2043) runs before the hook, so generated-id detection is already done.
- Header extraction (`get_chain_id_from_headers`, litellm_pre_call_utils.py:697)
  runs inside `add_litellm_data_to_request` and lands in
  `data["litellm_session_id"]` + metadata - the resolver reads that rather
  than re-parsing headers.

## New package (all additive, no conflicts)

```
litellm/router_utils/session_identity/
├── __init__.py
├── hash_chain.py        # port of llm-d chunk.go/alias.go (~250 lines)
├── canonicalizer.py     # contentStream framing for OpenAI messages/tools
├── history_matcher.py   # longest-prefix lineage lookup + teaching (~200 lines)
├── prefix_discriminator.py  # SGLang #34513 common-prefix exclusion (~150 lines)
├── store.py             # DualCache wrapper, clone of prompt_caching_cache.py
├── resolver.py          # SessionIdentityResolver(CustomLogger)
└── config.py            # env-tunable knobs (chunk size, TTL, caps)
```

### hash_chain.py (from llm-d chunk.go)
- `root_seed(model, salt) -> uint64` - xxhash over NUL-delimited fields
- `chunk_chain(stream, seed, chunk_size=512, max_chunks) -> list[int]`
  - UTF-8-safe boundary: extend chunk end past continuation bytes
    (`0b10xxxxxx`), drop trailing partial chunk (same reserve logic as Go)
  - `prev_hash = hash(le_bytes(prev_hash) + chunk)` per chunk
  - matching hash at i proves byte-prefix identity through chunk i
- NUL-delimited field framing (`writeSeeded` equivalent) so "ab"+"c" != "a"+"bc"

### canonicalizer.py (from llm-d contentStream, adapted to LiteLLM data)
Frame order must match what the engine sees:
1. `seg_json("chat", "tools", tools)` - sorted-key stable JSON of tool schemas
2. per message: `seg(surface, role, content_text)` + `seg_json(role/tool_calls)`
3. system prompt included (Anthropic top-level system maps to its own seg)
- Use `sha256` (hashlib) instead of xxhash: Python stdlib, no C dep, and
  cross-language hash compatibility does not matter because only this service
  reads these keys. Keep the 8-byte le encoding of the previous hash inside
  the digest input so the chain shape matches llm-d's design.
- Handles `data["messages"]` (OpenAI surface) + `data["tools"]`;
  Responses/Anthropic surfaces can be added later - the resolver skips
  call types it cannot frame.

### history_matcher.py
Redis-backed lineage, adapted from llm-d index.go + vLLM PR #217 teaching:
- Key: `session_identity:v1:{model_group}:{caller_scope}:{chain_hash}`
  (caller_scope = hashed api key, same scoping as DeploymentAffinityCheck:249)
- Value: `{"session_id": <inferred id>, "chain_len": n, "ts": <epoch>}`
- `lookup(chain)`: walk from deepest hash backwards; first hit wins. One
  Redis MGET over the chain hashes (bounded by max_chunks, default 64).
- `teach(chain, session_id)` after deployment selection: write every chain
  hash -> session mapping with TTL. Idempotent; refresh on rewrite.
- Lifecycle: TTL only (no in-process LRU needed - Redis IS the LRU here).
  In-memory tier of the DualCache absorbs the hot-path read cost.

### prefix_discriminator.py (SGLang #34513 design, no upstream code exists)
The 30k shared system prefix makes early chain hashes identical across all
conversations. Without discrimination, hash at position 0 would "match" for
every session.
- Maintain a per-(model_group) set of "common hashes": chain positions whose
  key maps to > N distinct session_ids (config, default 3). Learned lazily
  during teach(): when a hash key already holds a different session id,
  increment a counter in a separate `session_identity:common:v1:{hash}` key.
- `lookup` ignores common hashes: match depth = deepest NON-common position.
- Sessions whose entire chain is common get no pin (correct: no distinguishing
  content means no affinity signal).
- This is the piece to validate hardest against opencode/Copilot traffic where
  the shared prefix dominates.

### store.py
Clone `prompt_caching_cache.py` shape: hold the DualCache passed into the
hook, `async_get/async_set` with TTL, JSON values. No new cache object, no
Router changes, no `_update_redis_cache` changes.

### resolver.py
```python
class SessionIdentityResolver(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        # chat completions only at first
        if call_type not in ("completion", "acompletion"): return data
        md = data.get("litellm_metadata") or data.get("metadata") or {}
        if md.get("session_id"): return data                      # explicit
        if md.get(SESSION_ID_GENERATED_METADATA_KEY): return data # policy ran
        if data.get("litellm_session_id"): return data            # header id
        declared = _declared_id(data)        # prompt_cache_key / conversation
        chain = build_chain(data)            # canonicalize + chunk_chain
        sid = declared or await store.lookup(chain, model_group, caller_scope)
        if sid is None and chain: sid = synthesized stable id from deepest match
        if sid:
            md["session_id"] = sid
            md[SESSION_ID_INFERRED_METADATA_KEY] = True
        return data
```
- `caller_scope` = sha256 of `user_api_key_dict.api_key` (same hashing rule
  as `DeploymentAffinityCheck._hash_user_key`).
- Never set `SESSION_ID_GENERATED_METADATA_KEY` (that marker makes the
  affinity check ignore the id).
- Teaching (chain hash -> sid writes) must NOT happen in the hook (deployment
  not chosen yet). Teaching point: the resolver also implements
  `async_log_success_event` - at that point
  `kwargs["litellm_params"]["deployment"]` (or standard_logging payload) gives
  the deployment; write chain->sid + sid->deployment-side-channel there.
  The affinity pin itself is still written by DeploymentAffinityCheck
  (`deployment_affinity_check.py:566` async_pre_call_deployment_hook) on the
  NEXT request, after the resolver has stamped the id. Sequence:
  turn N teaches lineage in success event; turn N+1 hook resolves sid ->
  affinity filter reads pin (created by affinity check at end of turn N+1) ->
  turn N+2 onward pinned. First two turns of a brand-new inferred conversation
  are unpinned; acceptable.

## Constants (litellm/constants.py, next to :1482-1489)
- `SESSION_ID_INFERRED_METADATA_KEY = "_session_id_inferred"`
- `SESSION_IDENTITY_CACHE_KEY_PREFIX = "session_identity:v1"`
- `SESSION_IDENTITY_COMMON_HASH_PREFIX = "session_identity:common:v1"`

## Config (no yaml schema changes required)
Env knobs read in config.py, defaults in parens:
- `SESSION_IDENTITY_ENABLED` (false - opt-in per deployment)
- `SESSION_IDENTITY_CHUNK_SIZE_BYTES` (512)
- `SESSION_IDENTITY_MAX_CHAIN_HASHES` (64)
- `SESSION_IDENTITY_TTL_SECONDS` (86400, align with affinity TTL)
- `SESSION_IDENTITY_COMMON_PREFIX_THRESHOLD` (3)
Enable in prod/dev yaml via existing callback mechanism:
`litellm_settings.callbacks: [litellm_hooks..., litellm.router_utils.session_identity.resolver.SessionIdentityResolver]`
(or a wrapper module in litellm_hooks/ to keep the import short and to allow
per-env construction args).

## Tests (mirror the source-module convention)
- `tests/test_litellm/router_utils/test_session_identity_hash_chain.py`
  - boundary/UTF-8 cases ported from llm-d producer_test.go
  - chain stability: appending a turn does not move earlier chunk hashes
  - fork detection: divergent suffix keeps prefix hits
- `tests/test_litellm/router_utils/test_session_identity_matcher.py`
  - lookup miss -> infer from deepest hash; teach then lookup roundtrip
  - TTL expiry
- `tests/test_litellm/router_utils/test_session_identity_prefix_discriminator.py`
  - 2 sessions sharing a 30k system prefix: common hash excluded, distinguishing
    suffix still resolves each session to its own id
- `tests/test_litellm/router_utils/test_session_identity_resolver.py`
  - explicit metadata.session_id wins; generated marker skips; header id skips
  - stamps inferred id + marker; leaves request untouched when nothing matches
- `tests/test_litellm/router_utils/pre_call_checks/test_session_id_affinity.py`
  - extend: inferred id (with marker, without generated marker) creates a pin
- `tests/test_litellm/proxy/proxy_server/` integration: hook ordering vs
  apply_missing_session_id_policy

## Rollout
1. Dev first: enable resolver callback in litellm-config-dev.yaml, deploy,
   replay opencode-shaped traffic (ses_ ids arrive via x-session-id and skip
   the resolver - verify no interference), then synthetic no-id conversations
   - verify spend logs show session_id + inferred marker, and redis gains
   session_identity:* keys, and DeploymentAffinityCheck pins on turn 3+.
2. Watch: pin churn rate, redis memory delta, p50 overhead added by hook
   (expect < 5ms: one canonicalize + MGET).
3. Prod later: same yaml change. The resolver is a normal callback, so the
   LiteLLM_Config DB-overlay trap from the router_settings rollout does not
   apply to litellm_settings.callbacks (it merges the same way - verify the
   DB row after rollout, same as before).

## Explicitly out of scope
- No changes to DeploymentAffinityCheck, Router, routing strategy ordering.
- No port of vLLM worker selection / prefix trees (that is the router's job).
- No Go/Rust runtime: algorithms ported to pure Python (hash + MGET path is
  network-bound; a native ext saves microseconds and costs a toolchain).
- No synthetic ids for requests with no lineage: they route normally until a
  distinguishing chain exists.
