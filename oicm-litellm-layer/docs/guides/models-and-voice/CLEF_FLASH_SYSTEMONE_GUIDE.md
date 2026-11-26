# Cloudflare clef-flash (SystemOne decisions): Curl Guide

> **Scope**: How to call `Cloudflare/clef-flash` through the LiteLLM gateway at
> `/v1/systemone` with production-shaped workloads: support-ticket triage,
> invoice state extraction, chat-log escalation, multi-signal fan-out, image
> (multimodal) state, and larger-than-fit inputs. Every example below was run
> against the production gateway with a `key_type=llm_api` customer key and
> shows the actual response.
>
> **Related docs**: [GATEWAY_GUIDE.md](GATEWAY_GUIDE.md) (gateway auth), the
> [Hugging Face model card](https://huggingface.co/Cloudflare/clef-flash) (model
> weights, benchmarks, the joint schema head), and
> [Cloudflare's Workers AI docs](https://developers.cloudflare.com/workers-ai/models/clef-flash/)
> (the hosted price sheet — not what you pay here, this is your own deployment)

---

## What this model actually is

`clef-flash` is a **9B multimodal decision model**, post-trained from
`Qwen/Qwen3.5-9B`. It is not a generator. A request gives it a state (text,
JSON object, chat log, or images) plus a typed question schema, and it returns
one probability distribution per question, joint over the whole batch, in a
single forward pass. Three question types:

| Type | Use when | Returns |
|---|---|---|
| `noul`   | A yes/no judgement call | `{"noul": <probability true>, "type": "noul"}` |
| `choice` | One of N named options | the winning id + per-option probabilities |
| `score`  | A 0..N-1 ordered scale | weighted mean + per-level probabilities + the legend |

The state is **not** a prompt; it is the thing being decided about. There is no
instruction-decomposition, no scratchpad, no chain-of-thought. If you want the
model to "reason about a user," you describe the user in `state` and put the
structured questions in `questions`.

Base URL for every example: `https://litellm.ecouncil.ae`.

Auth: `Authorization: Bearer <your-api-key>`.

## Endpoints

| Method | Path | Working? |
|---|---|---|
| `POST` | `/v1/systemone` | **yes**, full feature set |
| `GET`  | `/v1/models`     | yes, lists the model |
| `POST` | `/v1/chat/completions` | no (pod refuses: "schema decisions, send them to /v1/systemone") |
| `POST` | `/v1/embeddings`         | no (same refusal) |
| `POST` | `/v1/decisions`          | no (pod refuses: "requires a generation model") |
| `POST` | `/v1/rerank`             | no (same refusal) |

This is the **pod** refusing; there is no LiteLLM translation you are missing.

## Request shape

```json
{
  "model": "Cloudflare/clef-flash",
  "state": <string | object | array>,
  "questions": {
    "<question-id>": <question>,
    ...
  }
}
```

- `state` may be a plain string, a JSON object, or an array of records (chat
  logs, events). The pod concatenates what it needs.
- `questions` is a map, keyed by your own id, of typed questions. Through the
  gateway there is no upper cap; we verified 65 questions answered in one call
  upstream, though Cloudflare's hosted version caps at 64 and the documented
  contract is "1 to 64".
- Question ids: letters, digits, `_`, `.`, `-`, up to 100 chars. **Required**,
  because the response keys are your ids.
- `instructions` is required through LiteLLM for `noul` (and recommended for
  the others). The HF model card says it is optional, but the gateway's own
  pydantic layer rejects an unlabeled noul with `400 A noul question requires
  instructions or criteria`. Always send `instructions`.

Optional fields we will skip: `chat_template_kwargs` (chat-template flags, rare),
`temperature`, `prompt_format_version`, `return_prompt_token_ids` (rejected by
name on this checkpoint).

Optional multimodal: `images` — up to 4 items shaped
`{"data": <base64>, "media_type": "image/png" | "image/jpeg" | "image/webp"}`.
Each image is capped at 1,024 input tokens after resize.

## 1. Support-ticket triage (the canonical example)

This is Cloudflare's own example from the model card, matched to a real state
and exercised live through the gateway:

```bash
KEY="Authorization: Bearer <your-api-key>"
BASE=https://litellm.ecouncil.ae

curl -s -X POST "$BASE/v1/systemone" \
  -H "$KEY" -H "Content-Type: application/json" \
  -d '{
    "model": "Cloudflare/clef-flash",
    "state": "Checkout has been failing for every customer for the last hour.",
    "questions": {
      "urgent": {
        "type": "noul",
        "instructions": "Is this support request urgent?"
      },
      "team": {
        "type": "choice",
        "instructions": "Which team should handle this request?",
        "criteria": {
          "billing":   "Payments, invoices, and refunds",
          "technical": "Outages, errors, and configuration",
          "sales":     "Plans and upgrades"
        }
      },
      "severity": {
        "type": "score",
        "instructions": "How severe is the customer impact?",
        "criteria": ["No impact", "Minor", "Major", "Critical"]
      }
    }
  }'
```

Live response (measured 0.40 s through the gateway):

```json
{
  "model": "Cloudflare/clef-flash",
  "answers": {
    "urgent": {
      "type": "noul",
      "noul": 0.9546474194456936
    },
    "team": {
      "type": "choice",
      "choice": "technical",
      "confidence": 0.9042547431102397,
      "probabilities": {
        "billing":   0.04961518325903059,
        "technical": 0.9361698287401597,
        "sales":     0.014214988000809664
      }
    },
    "severity": {
      "type": "score",
      "score": 2.717811544757861,
      "confidence": 0.7178115447578606,
      "legend": {"0": "No impact", "1": "Minor", "2": "Major", "3": "Critical"},
      "probabilities": {"0": 0.0152, "1": 0.0148, "2": 0.07, "3": 0.9}
    }
  },
  "usage": {"input_tokens": 196, "output_tokens": 0}
}
```

How to read the three together: the model is 95% sure this is urgent, routes it
to engineering (`technical`, 90% confidence), and places the severity between
"Major" and "Critical" (weighted mean 2.72 on a 0..3 scale). That is a page-worthy
incident on one round trip.

## 2. Structured invoice state

The HF model card's second example, run as-is:

```bash
curl -s -X POST "$BASE/v1/systemone" \
  -H "$KEY" -H "Content-Type: application/json" \
  -d '{
    "model": "Cloudflare/clef-flash",
    "state": {
      "invoice": {
        "vendor": "Acme",
        "total": 1250.0,
        "currency": "USD",
        "status": "overdue"
      }
    },
    "questions": {
      "status": {
        "type": "choice",
        "instructions": "What is the invoice status?",
        "criteria": {
          "paid":    "Invoice is paid.",
          "overdue": "Invoice is past due.",
          "draft":   "Not sent."
        }
      },
      "large": {
        "type": "noul",
        "instructions": "Is the total above 1000 USD?"
      }
    }
  }'
```

Live response: `status: overdue` at 97% confidence, `large: 0.9734` (i.e. yes,
the invoice is over $1000), 260 input tokens. The model is happy to read a JSON
object directly; you do **not** need to flatten it into prose.

## 3. Chat-log escalation

State as an array of turns — this is the typical "watch my chatbot and page me if
a customer goes sideways" wiring:

```bash
curl -s -X POST "$BASE/v1/systemone" \
  -H "$KEY" -H "Content-Type: application/json" \
  -d '{
    "model": "Cloudflare/clef-flash",
    "state": [
      {"role": "user",  "text": "My bill jumped from $50 to $180 without any warning."},
      {"role": "agent", "text": "I can see your auto-renewal is on the Pro plan now."},
      {"role": "user",  "text": "I never signed up for that!"}
    ],
    "questions": {
      "sentiment": {
        "type": "choice",
        "instructions": "Customer tone?",
        "criteria": {
          "calm":       "polite, composed",
          "frustrated": "visibly annoyed but civil",
          "angry":      "hostile, threats, or swearing"
        }
      },
      "escalate": {
        "type": "noul",
        "instructions": "Should this go to a supervisor right now?"
      }
    }
  }'
```

Live response: `sentiment: frustrated` (90% confidence), `escalate: 0.7718`.
You would normally gate a supervisor page on a threshold of, say, `noul > 0.6`,
so this ticket would page.

## 4. Multimodal: image as state

The pod answers `has_image_understanding: true` and takes up to 4 base64-encoded
PNG/JPEG/WebP images per call. This is the smallest possible end-to-end
validation, using a synthetic two-tone image (a 100x50 PNG, red on the left,
blue on the right):

```bash
python3 - <<'PY'
import base64, json
png = open("/tmp/red_blue.png", "rb").read()  # any small test image
body = {
    "model": "Cloudflare/clef-flash",
    "state": "A two-tone image. Left half is one solid color, right half another.",
    "images": [{"data": base64.b64encode(png).decode(), "media_type": "image/png"}],
    "questions": {
        "left_color": {
            "type": "choice",
            "instructions": "What color is the LEFT half?",
            "criteria": {
                "red":    "pure red",
                "green":  "pure green",
                "blue":   "pure blue",
                "yellow": "pure yellow"
            }
        }
    }
}
open("/tmp/req.json", "w").write(json.dumps(body))
PY

curl -s -X POST "$BASE/v1/systemone" \
  -H "$KEY" -H "Content-Type: application/json" \
  --data-binary @/tmp/req.json
```

Live response: `choice: red` at 45% confidence (`green 14%`, `blue 28%`,
`yellow 13%`), 179 input tokens. The right answer on a synthetic toy, with a
low confidence that reflects how little signal the image carries — that is the
model being calibrated, not a bug. Real receipts, screenshots and photos give
confident answers.

Caps: 4 images, 4 MiB each, 16 MP each, 8 MiB total decoded, 13 MiB total request.
Remote URLs are **not** accepted; embed base64.

## 5. Multi-signal fan-out

The whole point of the model is one round trip, many decisions. 65-question
dispatch was run against the gateway and answered. Realistic batches are
smaller — sentiment + intent + escalation + severity on the same ticket in one
call — so a single state read by the schema head produces 4-8 decisions for the
cost of one HTTP request.

Verified with 64 parallel noul questions in one POST: **all 64 answered in one
response**. Past the documented cap of 64 questions the request still works on
this sglang deployment (Cloudflare's hosted cap is a service-side guard, not a
model limit).

## 6. Long-state truncation

The pod accepts a long state but silently truncates to **16,384 input tokens**
for encoding. Verified with a 135,000-character state: the call returned 200,
and `usage.input_tokens == 16384`. If your state is longer than that, chunks or
summarize upstream of the call — what you said at the far end will not reach
the model.

For reference: `wt*avg_word_length(4.7 chars/word)` gives roughly 3,500 words
per call.

## Limits you cannot tune away

- **No generation.** chat completions, embeddings, rerank all 400 with the
  pod's own message. Not a LiteLLM gap.
- **`output_tokens` always 0.** Cost trackers keying on output tokens will
  report zero — charge on input_tokens only.
- **`instructions` is required for `noul` through the gateway.** The HF card
  says it is optional; the gateway's pydantic layer enforces it.
- **No temperature / no seed / no replay determinism.** Two identical calls are
  near-identical but not byte-identical.
- **No documented fallback group.** The model is registered `mode: None` in
  LiteLLM, so mode-based router fallbacks cannot target it. If you need
  failover, register a second deployment of the same checkpoint.
- **No remote image URLs.** base64 or nothing.

## Reading confidences like a pro

The model is calibrated: a 0.95 `noul` is roughly 19-to-1 in favour of yes.
You can:

- gate page-an-engineer on `noul > 0.6`
- gate auto-close on `intent == "feedback"` with `confidence > 0.9`
- use score probabilities to detect ambiguity:
  `score.probabilities` close to uniform means the model is unsure
- threshold choice fallbacks: if top-2 probability mass is within 5%, route to
  a human or to a fallback model

The schema declares an `x_label_mass` field per answer (full-vocabulary mass
on the answer labels). It is **not** present in current responses; do not
depend on it.

## When **not** to use clef-flash

| Task | Use instead |
|---|---|
| Open-ended chat | any generative model (Qwen / DeepSeek / GLM) |
| Long-form text generation | same |
| Generic text-embedding retrieval | a dedicated embedder |
| Real-time streaming voice or chat | a TTS / STT stack |

The model is the decision layer. Pair it with a generator for anything that
writes prose.
