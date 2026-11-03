# LiteLLM Gateway Token-Limit Misreporting — Complete Diagnosis

**Gateway:** `https://litellm.ecouncil.ae` (LiteLLM proxy, cluster `alain`, namespace `adeo`)
**Deployments affected:** all 8 `mode: chat` models (all `hosted_vllm`), all `db_model: true`,
`oicm_source: local`
**Discovered:** 2026-11-03
**Verified end-to-end against the live gateway.**

---

## 1. Summary for the gateway team

Each deployment's `model_info` reports **all three token fields equal to `max_model_len`**:

```jsonc
"max_tokens":        524288,
"max_input_tokens":  524288,   // == window
"max_output_tokens": 524288,   // == window  ← the defect
"context_length":    null      // empty
```

vLLM's hard invariant is:

```
prompt_tokens + max_tokens ≤ max_model_len
```

With `max_tokens` already equal to the window, the sum can never fit, so **every request 400s — even a
one-word prompt.** Any client that trusts `max_output_tokens` as "how many completion tokens I may request"
is broken by this metadata. The numbers are platform-authored (they live only in `model_info`, never in
`litellm_params`), identical across models of the same window (5×524288, 2×262144, 1×65536), and in one
case **larger than the served model's own cap** — so they come from a single template variable, not from
the vLLM server.

**Fix (one line per deployment, §4.1):** set `max_output_tokens` to a real completion budget, derive
`max_input_tokens = context_length − max_output_tokens`, report `context_length = max_model_len`, and
stop writing `max_tokens`.

---

## 2. What is wrong, precisely

The three fields are three different quantities and must not be equal:

| Field | Meaning | Correct value |
| --- | --- | --- |
| `context_length` | The window (`max_model_len`) | `max_model_len` |
| `max_input_tokens` | Prompt budget | `max_model_len − max_output_tokens` |
| `max_output_tokens` | Completion budget | a real completion cap (e.g. 64000) |
| `max_tokens` | Legacy duplicate of the output limit | omit it |

The gateway instead writes `max_model_len` into all three and leaves `context_length` null.

### 2.1 The constraint, proven to the token

`deepseek-ai/DeepSeek-V4.1-Flash` (window 524288), a 31-token prompt:

| `max_tokens` | `prompt + max_tokens` | Result |
| --- | --- | --- |
| 524257 | 524288 (== window) | **200** |
| 524258 | 524289 | **400** |

Server-confirmed arithmetic:

```
Requested token count exceeds the model's maximum context length of 524288 tokens.
You requested a total of 524289 tokens: 31 tokens from the input messages
and 524258 tokens for the completion.
```

**Input tokens are genuinely counted.** Same `max_tokens`, larger prompt flips 200 → 400:

| Prompt | `max_tokens` | Result |
| --- | --- | --- |
| 31 tokens | 524257 | 200 |
| 2031 tokens | 524257 | **400** (total 526288) |
| 2031 tokens | 520000 | 200 |

**Input alone is also bounded by the window.** A 533,361-token prompt with `max_tokens = 64`:

```
The input (533361 tokens) is longer than the model's context length (524288 tokens).
```

**Chat-template overhead ≈ 32 tokens.** With a 31-token prompt the largest passing `max_tokens` is
524257 (not 524256), i.e. `prompt + max_tokens + 32 = max_model_len`. The safest reading for clients is
`prompt + max_tokens ≤ max_model_len`; the gateway's error text already folds the template overhead into
its reported `prompt_tokens` count.

### 2.2 Every chat model fails when sent its own reported value

Sent `max_tokens = <what the gateway reports as max_output_tokens>`:

| Model | Reported | Sent | Result |
| --- | --- | --- | --- |
| `deepseek-ai/DeepSeek-V4.1-Flash` | 524288 | 524288 | **400** |
| `deepseek-ai/DeepSeek-V4-Flash-0731` | 524288 | 524288 | **400** |
| `zai-org/GLM-5.3` | 524288 | 524288 | **400** |
| `zai-org/GLM-5.3-Flash` | 524288 | 524288 | **400** |
| `moonshotai/Kimi-K3` | 524288 | 524288 | **400** |
| `Qwen3.6-35B-A3B-FP8` | 262144 | 262144 | **400** |
| `Qwen/Qwen3.8-Flash-Next-FP8` | 262144 | 262144 | **400** |
| `microsoft/VibeVoice-ASR` | 65536 | 65536 | **400** |

**8 of 8 fail.** The advertised value is unusable as a request parameter for every single model.

### 2.3 Three distinct vLLM error shapes, one root cause

Because the reported value equals `max_model_len`, different vLLM versions report the violation
differently — but all three trace to the same bad metadata.

**A. Context-window arithmetic** (most models):

```
Requested token count exceeds the model's maximum context length of 524288 tokens.
You requested a total of 524319 tokens: 31 tokens from the input messages and
524288 tokens for the completion.
```

**B. Output-cap check** (`Qwen3.6-35B-A3B-FP8`):

```
max_completion_tokens is too large: 524288. This model supports at most 262144 completion tokens.
```

The gateway advertises **524288** but the served model's own completion cap is **262144** — so the
metadata is not merely conflating fields, it is **overstating** this deployment. The number is not read
from the model at all.

**C. `max_model_len` check** (`microsoft/VibeVoice-ASR`):

```
This model's maximum context length is 65536 tokens. However, you requested 65536 output tokens
and your prompt contains 52 characters (more than 0 characters, which is the upper bound for
0 input tokens).
```

vLLM computed an input budget of **0 tokens** — `window − max_tokens = 65536 − 65536 = 0`.

---

## 3. Why clients send the whole window (expected behavior)

This is correct client behavior given the metadata, not a client bug. Trace through
`vivswan/litellm-vscode-chat` v0.6.12 (the same logic any well-behaved OpenAI client implements):

1. **Discovery maps the wire fields** — `src/provider/catalog/discovery.ts:191-203`

   ```ts
   const maxOutputTokens =
       normalizePositiveNumber(item.model_info?.max_output_tokens)
       ?? normalizePositiveNumber(item.model_info?.max_tokens);
   const maxTokens =
       normalizePositiveNumber(item.model_info?.max_tokens)
       ?? normalizePositiveNumber(item.model_info?.max_output_tokens);
   const provider: LiteLLMProvider = {
       context_length: maxInputTokens ?? maxTokens,   // 524288, because max_input_tokens was reported
       max_input_tokens: maxInputTokens,              // 524288
       max_output_tokens: maxOutputTokens,            // 524288
       ...
   ```

   Because the gateway reports `max_input_tokens` explicitly, `context_length` is taken from it rather
   than derived. The client never computes `window − output`.

2. **Constraints are derived** — `src/provider/catalog/modelCatalog.ts:25-38`

   ```ts
   const declaredOutputTokens = provider?.max_output_tokens ?? provider?.max_tokens;
   const maxOutputTokens  = declaredOutputTokens ?? FLOOR_MAX_OUTPUT_TOKENS;
   const defaultMaxTokens = declaredOutputTokens ?? guessedMaxTokensDefault(FLOOR_MAX_OUTPUT_TOKENS);
   const maxInputTokens   = provider?.max_input_tokens ?? Math.max(1, contextLength - maxOutputTokens);
   ```

   A **declared** output limit is adopted whole as the request default. The 4096 safety cap applies only
   to *guessed* values (catalog-derived or the built-in floor), never to a server-declared one. So
   `defaultMaxTokens = 524288`.

3. **The request default is resolved** — `src/shared/config/parameterResolution.ts:328-342`

   ```ts
   export function resolveMaxTokens(input: ResolveMaxTokensInput) {
       if (typeof input.forcedMaxTokens === "number")     return { value: input.forcedMaxTokens, source: "forced" };
       if (typeof input.runtimeMaxTokens === "number")    return { value: input.runtimeMaxTokens, source: "runtime" };
       if (typeof input.configuredMaxTokens === "number") return { value: input.configuredMaxTokens, source: "configured" };
       return { value: input.defaultMaxTokens, source: input.defaultMaxTokens < input.maxOutputTokens ? "capped" : "limit" };
   }
   ```

   Nothing configured ⇒ `max_tokens: 524288` goes on the wire.

4. **The pre-send guard cannot catch it** — `src/provider/transport/chatClient.ts:254-255`

   ```ts
   const tokenLimit = Math.max(1, metadata.maxInputTokens);  // 524288
   if (inputTokenCount + toolTokenCount > tokenLimit) { ... } // 31 > 524288 ? no
   ```

   The guard compares **input alone** against `max_input_tokens`. It never checks `input + max_tokens`
   against the window, because the client has no separate window field once `max_input_tokens` claims to
   be the whole thing.

**Conclusion:** the client does exactly what the metadata tells it to. Fix the metadata and every client
recovers at once.

---

## 4. The gateway fix

### 4.1 Correct the metadata (the actual fix)

Per deployment, set a real completion budget and derive the input budget from it. **Both
`max_input_tokens` and `max_output_tokens` must change** — the gateway reports `max_input_tokens`
explicitly, and that suppresses the client's own `window − output` derivation.

```yaml
# config.yaml — one entry per deployment
model_list:
  - model_name: deepseek-ai/DeepSeek-V4.1-Flash
    litellm_params:
      model: hosted_vllm/deepseek-ai/DeepSeek-V4.1-Flash
      api_base: http://s-c4299f34-....adeo.svc.cluster.local:8080/v1
    model_info:
      context_length:    524288    # the window (max_model_len)
      max_output_tokens:  64000    # a real completion budget
      max_input_tokens:  460288    # = context_length - max_output_tokens
      # max_tokens: leave it OUT (see 4.2)
```

**Recommended values** (verified HTTP 200 against the live gateway with a short prompt and a prompt at
10 % of the input budget; the OAI-compatible extension on this gateway already uses the 512000/64000
split successfully):

| Model | `context_length` | `max_output_tokens` | `max_input_tokens` |
| --- | ---: | ---: | ---: |
| `deepseek-ai/DeepSeek-V4.1-Flash` | 512000 | 64000 | 448000 |
| `deepseek-ai/DeepSeek-V4-Flash-0731` | 512000 | 64000 | 448000 |
| `zai-org/GLM-5.3` | 512000 | 64000 | 448000 |
| `zai-org/GLM-5.3-Flash` | 512000 | 64000 | 448000 |
| `moonshotai/Kimi-K3` | 512000 | 64000 | 448000 |
| `Qwen3.6-35B-A3B-FP8` | 262144 | 32768 | 229376 |
| `Qwen/Qwen3.8-Flash-Next-FP8` | 262144 | 32768 | 229376 |
| `microsoft/VibeVoice-ASR` | 65536 | 8192 | 57344 |

> Note the 512k models are set to **512000, not 524288.** The OAI-compatible extension on this gateway
> ships 512000/64000 for them and works; 512000 also leaves ~2k of headroom under the vLLM
> `max_model_len` of 524288. If you prefer to advertise the true window, use `context_length = 524288`,
> `max_output_tokens = 65536`, `max_input_tokens = 458752` — both splits were verified to pass.
>
> `Qwen/Qwen3-ASR-1.7B` is `mode: audio_transcription` (out of scope for chat) but carries the same
> defect (`max_output_tokens` = its full window = 65536) and should be corrected for consistency.

### 4.2 Drop the redundant `max_tokens` field

`model_info.max_tokens` is LiteLLM's legacy *output* limit. When present and equal to the window it
**shadows** the corrected values in some client code paths (this client falls back to it whenever
`max_output_tokens` is absent — `discovery.ts:191-195`). Either remove it or set it to the same budget
as `max_output_tokens`. Never leave it equal to the window.

### 4.3 Alternative — omit `max_input_tokens` entirely

Omitting `max_input_tokens` also works: clients then derive `context_length − max_output_tokens`
themselves (this client at `modelCatalog.ts:30`). The only requirement is that `context_length` be
reported **correctly** — today it is `null`. Do **not** omit both `context_length` and
`max_input_tokens`: the client then falls back to `max_tokens` for `context_length` (`discovery.ts:200`),
and if `max_tokens` is still the window the bug is recreated.

### 4.4 Do not set `max_output_tokens = context_length − 1`

The exact-sum boundary technically works (`524257 + 31 = 524288` → 200) but leaves **zero headroom** for
client-side token-estimate error. Most clients estimate tokens locally (~4 chars/token) and can
underestimate by a few percent; the request then 400s on a long prompt the client thought fit. Reserve a
real completion budget instead.

### 4.5 Where the fix belongs

All 8 deployments are `db_model: true`, `oicm_source: local`, cluster `alain`. They are registered by the
platform's model-registration path, not hand-written `config.yaml`. **The fix belongs in the registration
code** so it survives re-registration. The current logic is equivalent to:

```python
model_info["max_tokens"]        = max_model_len
model_info["max_input_tokens"]  = max_model_len
model_info["max_output_tokens"] = max_model_len
```

Replace with:

```python
model_info["context_length"]    = max_model_len
model_info["max_output_tokens"] = COMPLETION_BUDGET              # e.g. min(64000, max_model_len // 8)
model_info["max_input_tokens"]  = max_model_len - model_info["max_output_tokens"]
# do not write model_info["max_tokens"] at all
```

---

## 5. Client-side workaround (VS Code `litellm-vscode-chat`)

**Two records, read at different times.** Both are `scope: "window"` → they belong in **User** settings,
not `Machine/settings.json` (which holds only `servers`, the one `scope: "machine"` key).

| Setting | Read when | Effect |
| --- | --- | --- |
| `models.parameters` | **every request**, inside `ChatClient.send()` (`chatClient.ts:283`) | sets the wire `max_tokens` via `resolveMaxTokens` (source `configured`) |
| `models.capabilities` | **serve time only**, stamped into the model object the host caches | sets `maxInputTokens` (pre-send guard) and `defaultMaxTokens` (the fallback) |

> ⚠️ **`models.capabilities` alone does not fix an already-open chat.** Its result is baked into the
> model object (`applyCapabilityOverrides` in `ServedModelDecorator.decorate`, `groupDiscovery.ts:478/516`)
> and the host hands that cached object back to `provideLanguageModelChatResponse`
> (`provider/index.ts:467`). An existing conversation keeps the stale `defaultMaxTokens` stamp.
> `models.parameters` is read fresh on every send. Both records are declared so the picker, the pre-send
> guard, and the wire agree, and so a **new** chat is correct from its first message.

`~/.vscode-server/data/User/settings.json`:

```jsonc
"litellm-vscode-chat.models.parameters": {
    "deepseek-ai/DeepSeek-V4.1-Flash":    { "max_tokens": 64000 },
    "deepseek-ai/DeepSeek-V4-Flash-0731": { "max_tokens": 64000 },
    "zai-org/GLM-5.3":                    { "max_tokens": 64000 },
    "zai-org/GLM-5.3-Flash":              { "max_tokens": 64000 },
    "moonshotai/Kimi-K3":                 { "max_tokens": 64000 },
    "Qwen3.6-35B-A3B-FP8":                { "max_tokens": 32768 },
    "Qwen/Qwen3.8-Flash-Next-FP8":        { "max_tokens": 32768 },
    "microsoft/VibeVoice-ASR":            { "max_tokens": 8192  }
},
"litellm-vscode-chat.models.capabilities": {
    "deepseek-ai/DeepSeek-V4.1-Flash":    { "context_length": 512000, "max_input_tokens": 448000, "max_output_tokens": 64000 },
    "deepseek-ai/DeepSeek-V4-Flash-0731": { "context_length": 512000, "max_input_tokens": 448000, "max_output_tokens": 64000 },
    "zai-org/GLM-5.3":                    { "context_length": 512000, "max_input_tokens": 448000, "max_output_tokens": 64000 },
    "zai-org/GLM-5.3-Flash":              { "context_length": 512000, "max_input_tokens": 448000, "max_output_tokens": 64000 },
    "moonshotai/Kimi-K3":                 { "context_length": 512000, "max_input_tokens": 448000, "max_output_tokens": 64000 },
    "Qwen3.6-35B-A3B-FP8":                { "context_length": 262144, "max_input_tokens": 230144, "max_output_tokens": 32000 },
    "Qwen/Qwen3.8-Flash-Next-FP8":        { "context_length": 262144, "max_input_tokens": 230144, "max_output_tokens": 32000 },
    "microsoft/VibeVoice-ASR":            { "context_length": 65536,  "max_input_tokens": 57344,  "max_output_tokens": 8192  }
}
```

A configured `max_tokens` counts as *configured* and is sent whole (no 4096 clamp). This is a
**workaround** — every other client on the gateway remains broken until §4.1 is applied.

### 5.1 A long conversation still fails even with the workaround

The workaround caps the *completion* budget; it cannot make an oversized conversation fit. Reproduced
with `deepseek-ai/DeepSeek-V4.1-Flash` at the field-reported input size:

| Input | `max_tokens` | `input + max_tokens` | Window | Result |
| --- | --- | --- | --- | --- |
| ~132,750 | 524288 | 657,038 | 524288 | **400** |
| ~132,750 | 64000 | 196,750 | 524288 | **200** |

After the workaround a chat carrying ~130k tokens works again — but a chat that grows past
`max_input_tokens` (448000) is refused by the extension's own pre-send guard, and past the real window it
is refused by vLLM. At that point the answer is a new chat, not a settings change.

---

## 6. Verification (run after the gateway change)

All three checks must pass.

```bash
KEY=sk-...; BASE=https://litellm.ecouncil.ae

# 1. Metadata describes a real budget split.
curl -s -H "Authorization: Bearer $KEY" $BASE/model/info | python3 -c '
import json,sys
d=json.load(sys.stdin); items=d.get("data",d); bad=0
for it in items:
    mi=it.get("model_info",{})
    if mi.get("mode")!="chat": continue
    ctx=mi.get("context_length") or mi.get("max_input_tokens") or mi.get("max_tokens")
    out=mi.get("max_output_tokens"); inp=mi.get("max_input_tokens")
    p=[]
    if out is None: p.append("max_output_tokens missing")
    if ctx is None: p.append("window unknown")
    if out is not None and ctx is not None and out>=ctx: p.append(f"out {out} >= window {ctx}")
    if out is not None and inp is not None and inp+out>ctx: p.append(f"in+out {inp+out} > window {ctx}")
    bad+=bool(p)
    print(f"{it[\"model_name\"]:<36} ctx={ctx} in={inp} out={out} "+("OK" if not p else "STILL BROKEN: "+"; ".join(p)))
print(f"\n{bad} chat model(s) still misreporting" if bad else "\nall chat models report a usable budget split")
'

# 2. A short prompt succeeds at the reported max_output_tokens.
curl -s -o /dev/null -w '%{http_code}\n' -X POST $BASE/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-ai/DeepSeek-V4.1-Flash","messages":[{"role":"user","content":"hi"}],"max_tokens":64000,"stream":false}'

# 3. A LARGE prompt succeeds at the same max_output_tokens (the input budget is real).
python3 - <<'EOF'
import json, urllib.request, os
key=os.environ["KEY"]; base=os.environ["BASE"]
body=json.dumps({"model":"deepseek-ai/DeepSeek-V4.1-Flash",
                 "messages":[{"role":"user","content":"word "*40000}],
                 "max_tokens":64000,"stream":False}).encode()
r=urllib.request.Request(base+"/v1/chat/completions", data=body,
    headers={"Authorization":"Bearer "+key,"Content-Type":"application/json"})
try: print(urllib.request.urlopen(r, timeout=300).status)
except urllib.error.HTTPError as e: print(e.code, e.read()[:300])
EOF
```

Expected: every row `OK`, then `200`, then `200`.

---

## 7. Root-cause classification

| # | Observation | Assessment |
| --- | --- | --- |
| 1 | `max_output_tokens == max_input_tokens == max_tokens == max_model_len` | All three populated from `max_model_len` instead of derived from it |
| 2 | `context_length` is `null` | The field that *should* hold the window is empty; the window leaked into the budget fields |
| 3 | `Qwen3.6-35B-A3B-FP8` advertises 524288 but vLLM caps completion at 262144 | The value is not read from the served model — copied from a platform-side default |
| 4 | Identical values across same-window models (5×524288, 2×262144, 1×65536) | A single `{max_model_len}` template variable written into all three keys |
| 5 | All `db_model: true`, `oicm_source: local`, cluster `alain` | Platform-registered, not hand-written `config.yaml` — fix in the registration code |
| 6 | Numbers exist only in `model_info`, never in `litellm_params` | Platform-authored metadata, not derived from the vLLM server |

---

## 8. Impact

- **Severity: total outage of all chat models** for any client that honours `max_output_tokens` as a
  request default. Every request 400s, including a one-word prompt.
- **Silent for clients that ignore the field** — they pass their own `max_tokens` and work fine, which is
  why the defect can go unnoticed at the gateway.
- **Misleading user-facing error.** Clients surface vLLM's text as "the conversation is too long — trim
  it, remove attachments, or start a new chat", sending users chasing a nonexistent long-conversation
  problem. The conversation is not too long; the advertised completion budget is the whole window.

---

## Appendix A — Raw `/model/info` evidence

`GET https://litellm.ecouncil.ae/model/info` (2026-11-03):

```jsonc
{
  "model_name": "deepseek-ai/DeepSeek-V4.1-Flash",
  "litellm_params": {
    "input_cost_per_token": 1.5e-07,
    "output_cost_per_token": 6e-07,
    "api_base": "http://s-c4299f34-4837-4698-b738-db7ef7b100e7.adeo.svc.cluster.local:8080/v1",
    "model": "hosted_vllm/deepseek-ai/DeepSeek-V4.1-Flash"
  },
  "model_info": {
    "mode": "chat",
    "db_model": true,
    "oicm_source": "local",
    "oicm_cluster": "alain",
    "oicm_namespace": "adeo",
    "max_tokens": 524288,
    "max_input_tokens": 524288,
    "max_output_tokens": 524288,
    "context_window": null,
    "base_model": null
  }
}
```

`litellm_params` carries **no** token-limit keys — the numbers exist only in `model_info`, i.e.
platform-authored metadata, not derived from the vLLM server.

## Appendix B — Reproduction with plain `curl`

```bash
KEY=sk-...; BASE=https://litellm.ecouncil.ae

# Fails — max_tokens equals the window
curl -s -X POST $BASE/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-ai/DeepSeek-V4.1-Flash","messages":[{"role":"user","content":"hi"}],"max_tokens":524288,"stream":false}'

# Succeeds — a real completion budget
curl -s -X POST $BASE/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-ai/DeepSeek-V4.1-Flash","messages":[{"role":"user","content":"hi"}],"max_tokens":64000,"stream":false}'
```

## Appendix C — Code references

Extension: `vivswan/litellm-vscode-chat` v0.6.12 (local checkout
`/home/jyao/ADEO/online-search/litellm-vscode-chat`).

| File | Lines | Role |
| --- | --- | --- |
| `src/provider/catalog/discovery.ts` | 191–203 | Maps `max_output_tokens` / `max_input_tokens` from `/model/info`; `context_length` taken from `max_input_tokens` when reported |
| `src/provider/catalog/modelCatalog.ts` | 25–38 | A **declared** output limit becomes the request default whole; only guesses are capped at 4096 |
| `src/shared/config/parameterResolution.ts` | 328–342 | `resolveMaxTokens` fallback chain; a configured `max_tokens` (source `configured`) beats the declared default |
| `src/shared/config/capabilityResolution.ts` | 623–637, 683–687 | `LEVEL_IS_USER_SET` — the 4096 cap applies to `catalog`/`floor`/`derived` only, never `server` |
| `src/provider/transport/chatClient.ts` | 254–255 | Pre-send guard checks **input only** vs `max_input_tokens`; never `input + max_tokens` vs the window |
| `src/provider/transport/chatClient.ts` | 283 | `models.parameters` is read **fresh per request** — the reliable override point |
| `src/provider/index.ts` | 467 | `parseModelMetadata(model)` — the request reads the **serve-time** stamp for `maxInputTokens` / `defaultMaxTokens` |
| `src/provider/catalog/groupDiscovery.ts` | 478, 516 | `decorator.decorate(...)` applies `models.capabilities` at **serve time only** |
| `src/shared/config/parameterResolution.ts` | 31 | `DEFAULT_MAX_TOKENS_CAP = 4096` |
