# Gateway defect: vision capability not declared (`supports_vision` is null)

**Gateway:** `https://litellm.ecouncil.ae`
**Discovered:** 2026-11-03
**Severity:** medium — images are silently discarded by compliant clients, even on models that
demonstrably process them
**Fix location:** gateway `/model/info` metadata (per-deployment `model_info`)

---

## 1. Correction of an earlier claim

Earlier in this investigation I said "no model declares vision support" and then, separately,
that *my own* model in the chat session had no vision. Those two statements got conflated. To be
precise:

- **The gateway models DO have vision** — the sglang/vLLM deployments genuinely process images,
  which is proven in §3.
- **What is missing is the `supports_vision` declaration** in `/model/info`. The key is *present
  but `null`*.
- My own assistant model in that session was a different model with no vision; that observation
  applied to me, not to your gateway. Sorry for the confusion.

So yes — **this is a gateway-side fix**, and your instinct was right.

---

## 2. What the gateway reports

`GET /model/info` for every chat deployment:

```jsonc
"model_info": {
  "supports_vision":                null,      // ← present, but null
  "supports_image_size":            null,
  "supports_embedding_image_input": null,
  "supports_pdf_input":             null,
  "supports_audio_input":           null,
  "supports_reasoning":             null,
  "input_cost_per_image_token":     null
  // no "architecture" block at all
}
```

`supports_vision` is **`null`, not `false`** — but both extensions test for an explicit `true`,
so both treat null as "no vision".

### How each extension reads it

| Extension | Source | Test | Result when null |
| --- | --- | --- | --- |
| `vivswan.litellm-vscode-chat` | `model_info.supports_vision` | `=== true` (`discovery.ts:215`) | `false` |
| `johnny-zhao.oai-compatible-copilot` | `architecture.input_modalities` | `.includes("image")` (`provideModel.js:112`) | `false` (block absent) |

### Why that silently drops images

The litellm extension gates the wire form on `imageInput`
(`src/shared/conversion/dataPartForm.ts:34-64`):

```
user message, vision gate on  -> image
user message, vision gate off -> text      ← image is discarded
tool result, any gate         -> text
```

So with `supports_vision: null`, an attached screenshot is **silently replaced with text** before
the request is sent. The server never sees an image — which also looks like a second, unrelated
BUG if you are watching token counts. Nothing is logged as an error; the image just disappears.

The OAI extension behaves the same way: `imageInput: vision` where `vision` is derived from
`architecture.input_modalities`, so with no `architecture` block it registers `imageInput: false`.

---

## 3. Proof the deployments really do have vision

Direct test: send a solid-colour PNG and ask for the colour. The ground truth is exact, so a
correct answer cannot be luck.

| Model | `image_tokens` | Score | Verdict |
| --- | ---: | ---: | --- |
| `deepseek-ai/DeepSeek-V4.1-Flash` | 184 | 3/3 | **SEES IMAGES** |
| `zai-org/GLM-5.3-Flash` | 64 | 3/3 | **SEES IMAGES** |
| `moonshotai/Kimi-K3` | 64 | 3/3 | **SEES IMAGES** |
| `Qwen3.6-35B-A3B-FP8` | 64 | 3/3 | **SEES IMAGES** |
| `Qwen/Qwen3.8-Flash-Next-FP8` | 64 | 3/3 | **SEES IMAGES** |
| `deepseek-ai/DeepSeek-V4-Flash-0731` | — | — | **TEXT ONLY** — 400 `Model only supports text input` |
| `zai-org/GLM-5.3` | — | — | **TEXT ONLY** — 400 `Model only supports text input` |
| `microsoft/VibeVoice-ASR` | — | — | **TEXT ONLY** — 400 |

Six colours in sequence (red / blue / green / yellow / black / white) were named correctly on
`DeepSeek-V4.1-Flash` — 6/6. A blind model could not do that.

A further 20 real photographs (two batches downloaded from the web, JPEG/PNG/WebP, 2 KB–462 KB,
128×128–3000×2000) were each described correctly, with `image_tokens` tracking pixel dimensions.

**So: 5 of 7 chat deployments have working vision that the metadata fails to advertise.**

---

## 4. The fix

### 4.1 Declare `supports_vision` per deployment

**Only for the deployments that actually accept images.** Setting it on a text-only model would
make clients send images and get a 400.

| Model | `supports_vision` | Why |
| --- | --- | --- |
| `deepseek-ai/DeepSeek-V4.1-Flash` | **`true`** | 3/3 colour test, `image_tokens` 184 |
| `zai-org/GLM-5.3-Flash` | **`true`** | 3/3, `image_tokens` 64 |
| `moonshotai/Kimi-K3` | **`true`** | 3/3, `image_tokens` 64 |
| `Qwen3.6-35B-A3B-FP8` | **`true`** | 3/3, `image_tokens` 64 |
| `Qwen/Qwen3.8-Flash-Next-FP8` | **`true`** | 3/3, `image_tokens` 64 |
| `deepseek-ai/DeepSeek-V4-Flash-0731` | `false` | 400: only supports text input |
| `zai-org/GLM-5.3` | `false` | 400: only supports text input |
| `microsoft/VibeVoice-ASR` | `false` | 400 (ASR deployment) |

```yaml
model_list:
  - model_name: deepseek-ai/DeepSeek-V4.1-Flash
    litellm_params:
      model: hosted_vllm/deepseek-ai/DeepSeek-V4.1-Flash
      api_base: http://s-c4299f34-....adeo.svc.cluster.local:8080/v1
    model_info:
      context_length:    524288
      max_output_tokens:  64000
      max_input_tokens:  460224
      supports_vision:   true          # ← add
```

### 4.2 Also add `architecture.input_modalities`

The litellm extension only reads `supports_vision`, but the OAI extension reads
`architecture.input_modalities`. For both to work without per-extension configuration, report both:

```yaml
    model_info:
      supports_vision: true
      architecture:
        input_modalities:  ["text", "image"]
        output_modalities: ["text"]
```

> `architecture` is an undocumented-in-`model_info` extension field. If your registration code
> refuses unknown keys, `supports_vision` alone fixes the litellm extension; the OAI extension
> then needs its model configured with `"vision": true` in its own `oaicopilot.models` array, or
> a `models.capabilities` override in the litellm extension.

### 4.3 Do not guess — derive it

Hard-coding per model drifts. Derive the flag from the deployment itself, e.g. probe the
vLLM `/v1/models` or the model's own config for an image tower / multimodal architecture
(`Qwen2VLForConditionalGeneration`, `LlavaForConditionalGeneration`, `…LlavaNext…`, etc.), or
make a single synthetic image request at registration and set `supports_vision` from whether it
returns 200.

---

## 5. Verification after the fix

```bash
KEY=sk-...; BASE=https://litellm.ecouncil.ae

curl -s -H "Authorization: Bearer $KEY" $BASE/model/info | python3 -c '
import json,sys
d=json.load(sys.stdin); items=d.get("data",d)
for it in items:
    mi=it.get("model_info",{})
    if mi.get("mode")!="chat": continue
    arch=(mi.get("architecture") or {}).get("input_modalities")
    print(f"{it[\"model_name\"]:<36} supports_vision={str(mi.get(\"supports_vision\")):<5} architecture={arch}")
'
```

Expected: `true` for the five vision-capable deployments, `false` (not `null`) for the rest.

Then confirm end-to-end that the extension actually sends an image — the request body should
contain `"type":"image_url"` and the response `usage` should carry `image_tokens > 0`.

---

## 6. Why this matters even though "it works for me"

Two separate failure modes were being conflated in this investigation:

| Symptom | Cause | Status |
| --- | --- | --- |
| Every request 400s, even "hi" | `max_output_tokens == context_length` | ✅ fixed by the gateway patch |
| Attached screenshots vanish | `supports_vision: null` | **this defect** |
| Huge token counts on tool-returned images | OAI extension JSON-serialized the bytes | ✅ fixed by the client patch |
| Images work but say "I can't see an image" | model truly lacked vision in that session | not a defect |

The metadata bug hid the vision capability, so the correct client behaviour (drop images when not
advertised) looked like an extension bug. Fixing `supports_vision` is what makes the images
actually arrive.

---

## Appendix — probe results

`/tmp/vision_capability_probe.json`:

```json
{
  "deepseek-ai/DeepSeek-V4.1-Flash":    { "vision": true,  "score": 3, "image_tokens": 184 },
  "zai-org/GLM-5.3-Flash":              { "vision": true,  "score": 3, "image_tokens": 64  },
  "moonshotai/Kimi-K3":                 { "vision": true,  "score": 3, "image_tokens": 64  },
  "Qwen3.6-35B-A3B-FP8":                { "vision": true,  "score": 3, "image_tokens": 64  },
  "Qwen/Qwen3.8-Flash-Next-FP8":        { "vision": true,  "score": 3, "image_tokens": 64  },
  "deepseek-ai/DeepSeek-V4-Flash-0731": { "vision": false, "error": "400 Model only supports text input" },
  "zai-org/GLM-5.3":                    { "vision": false, "error": "400 Model only supports text input" }
}
```

Scripts: `/tmp/probe_vision_per_model.py`, `/tmp/vision_color_test.py`.
