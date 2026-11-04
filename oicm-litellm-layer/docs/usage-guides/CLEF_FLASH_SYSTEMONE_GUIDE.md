# Cloudflare clef-flash (SystemOne decisions): Curl Guide

> **Scope**: How to call Cloudflare's `clef-flash` schema-decision model through the LiteLLM gateway's `/v1/systemone` route, the request shape for each question type, expected response fields, the endpoint matrix (what works and what doesn't), and the limits you cannot tune away
>
> **Related docs**: [GATEWAY_GUIDE.md](GATEWAY_GUIDE.md) (gateway auth and routes in general), [REASONING_CONTROL_GUIDE.md](REASONING_CONTROL_GUIDE.md) (a different "structured answer" trick, not related)

---

## What this is

`Cloudflare/clef-flash` is deployed on the `adeo` namespace as an sglang 0.5.22
pod running in **embedding / decision mode**. It is not a generation model: it
answers typed questions about an input by scoring single-token answer labels, so
you get full probability distributions back, no sampled text. Three question
kinds are supported simultaneously in a single request:

- **noul** (yes/no), returns a confidence that the statement holds
- **choice**, returns one option id plus a probability per option
- **score**, returns a weighted mean plus a probability per score level

Under the hood it is Qwen3_5 (35B MoE) with a 262,144-token context window
(Qwen3_5ForConditionalGeneration), but the chat completion pathway is disabled
on this checkpoint. The only working decision surface is `/v1/systemone`.

Base URL for every example: `https://litellm.ecouncil.ae`.

---

## Gateway endpoints

| Method | Path | Purpose | Through gateway? |
|---|---|---|---|
| `POST` | `/v1/systemone` | Decision questions on a state, multi-question per call | **200** |
| `GET`  | `/v1/models`     | Confirm the model is visible to your key | 200 |
| `GET`  | `/health`        | sglang liveness (pod-internal) | n/a, port-forward only |
| `GET`  | `/metrics`       | Prometheus metrics (pod-internal) | n/a, port-forward only |
| `POST` | `/v1/decisions`  | sglang's native decision API | **400** (pod says: requires a generation model) |
| `POST` | `/v1/chat/completions` | OpenAI chat shape | 400 (pod says: schema decisions only) |
| `POST` | `/v1/embeddings` | OpenAI embeddings shape | 400 (pod says: schema decisions only) |
| `POST` | `/v1/rerank`     | Rerank shape | 400 (pod says: schema decisions only) |
| `POST` | `/v1/score`      | sglang's scoring pipeline (low-level) | 400 (different shape; not user-facing) |

The "pod says" wording above is the actual error string the model returns when
a client tries to use a generic OpenAI-shaped route against it, e.g.
`{"object":"error","message":"This checkpoint answers schema decisions, send them
to /v1/systemone"}`. There is no LiteLLM translation layer missing; this is the
pod enforcing its own contract.

Authentication is the standard gateway key as a Bearer token:

```
Authorization: Bearer <your-api-key>
```

Send `model: "Cloudflare/clef-flash"` exactly with that case.

---

## Request shape

```json
{
  "model": "Cloudflare/clef-flash",
  "state": "<the input text to reason about>",
  "questions": {
    "<your-question-id>": { "type": "noul"   | "choice" | "score", ... },
    ...
  }
}
```

| Field | Required | Notes |
|---|---|---|
| `model` | yes | Must be `Cloudflare/clef-flash` |
| `state` | yes | Whatever you want decisions about. The schema says string; the pod also accepts a JSON object and stringifies it, but do not rely on that |
| `questions` | yes | Map of question id -> question, at least one |
| `chat_template_kwargs` | no | Forwarded to the chat template. Almost never needed |
| `temperature` | **forbidden** | Pod rejects with 400 pointing at `/v1/decisions` (which itself rejects), effectively a dead end |
| `prompt_format_version` | **forbidden** | Same as above |
| `return_prompt_token_ids` | **forbidden** | Same as above |

### `noul` question

Decide whether a single statement is true. `instructions` or `criteria.true` /
`criteria.false` is required — at least one must be non-blank.

```json
"is_refund_intent": {
  "type": "noul",
  "instructions": "Decide whether the user is asking for a refund."
}
```

or with criteria:

```json
"is_refund_intent": {
  "type": "noul",
  "criteria": {
    "true":  "user explicitly asks for money back or threatens to",
    "false": "user is asking for technical help, venting, or just commenting"
  }
}
```

### `choice` question

Pick one of N named options. Each option id is a key in `criteria`. Up to 255
options per question.

```json
"intent": {
  "type": "choice",
  "criteria": {
    "refund":   "user explicitly asks for their money back",
    "support":  "user wants technical help resolving the issue",
    "feedback": "user is sharing a comment without asking for action"
  }
}
```

### `score` question

Map to a 0..N scale. `criteria` is a plain list of level descriptions,
indexed `0..len(criteria)-1`. Up to 10 levels.

```json
"urgency": {
  "type": "score",
  "criteria": [
    "trivial",
    "low",
    "medium",
    "high",
    "critical"
  ]
}
```

---

## Sample curl

```bash
KEY="Authorization: Bearer <your-api-key>"
BASE=https://litellm.ecouncil.ae

curl -s -X POST "$BASE/v1/systemone" \
  -H "$KEY" -H "Content-Type: application/json" \
  -d '{
    "model": "Cloudflare/clef-flash",
    "state": "User says the new dashboard is confusing and they want a refund.",
    "questions": {
      "is_negative": {
        "type": "noul",
        "criteria": {
          "true":  "user is unhappy with the product",
          "false": "user is satisfied"
        }
      },
      "intent": {
        "type": "choice",
        "criteria": {
          "refund":   "user wants their money back",
          "support":  "user wants help",
          "feedback": "user is just commenting"
        }
      },
      "urgency": {
        "type": "score",
        "criteria": ["trivial", "low", "medium", "high", "critical"]
      }
    }
  }'
```

Live sample through the gateway (same body):

```json
{
    "model": "Cloudflare/clef-flash",
    "answers": {
        "is_negative": {
            "type": "noul",
            "noul": 0.9740426428022031
        },
        "intent": {
            "type": "choice",
            "choice": "refund",
            "confidence": 0.9841927009082423,
            "probabilities": {
                "refund":   0.9894618006054948,
                "support":  0.006165912628948775,
                "feedback": 0.0043722867655564095
            }
        },
        "urgency": {
            "type": "score",
            "score": 2.6188474128749317,
            "confidence": 0.5087493140197997,
            "legend": {
                "0": "trivial", "1": "low", "2": "medium",
                "3": "high",    "4": "critical"
            },
            "probabilities": {
                "0": 0.05404482046969567,
                "1": 0.07159775444649062,
                "2": 0.17999673484858603,
                "3": 0.5901865722096417,
                "4": 0.10417411802558602
            }
        }
    },
    "usage": { "input_tokens": 331, "output_tokens": 0 }
}
```

## Response shape

| Field | Always present | Notes |
|---|---|---|
| `model` | yes | Echoes the served model id |
| `answers` | yes | Keyed by the same question id you sent |
| `answers.*.type` | yes | Echoes the question type |
| `answers.<noul>.noul` | for `noul` | Probability the criteria are satisfied, 0..1 |
| `answers.<choice>.choice` | for `choice` | The winning option id |
| `answers.<choice>.confidence` | for `choice` | Probability mass on the winner |
| `answers.<choice>.probabilities` | for `choice` | Per-option probability, sums to 1.0 |
| `answers.<score>.score` | for `score` | Probability-weighted mean of the levels |
| `answers.<score>.confidence` | for `score` | Probability mass on the top level |
| `answers.<score>.legend` | for `score` | Index -> level description |
| `answers.<score>.probabilities` | for `score` | Per-level probability |
| `usage.input_tokens` | yes | Total prompt tokens (all questions, all scoring passes) |
| `usage.output_tokens` | yes | Always `0`. Nothing is generated |

The `x_label_mass` field the schema declares is **not** in current responses;
the schema is slightly ahead of the implementation.

---

## Production verification matrix

All of these were exercised against `https://litellm.ecouncil.ae` with a
`key_type=llm_api` customer key:

| Call | Result |
|---|---|
| POST `/v1/systemone` with one `noul` question | 200, `noul: 0.9740` |
| POST `/v1/systemone` with a `choice` question (3 options) | 200, `choice: refund`, `confidence: 0.9842` |
| POST `/v1/systemone` with a `score` question (5 levels) | 200, `score: 2.62` over legend `[trivial..critical]` |
| POST `/v1/systemone` with all three question types bundled | 200, all three answered in one round trip |
| GET `/v1/models` | `Cloudflare/clef-flash` listed, `mode: None` |
| POST `/v1/chat/completions` with the model | 400, pod error: "schema decisions, send them to /v1/systemone" |
| POST `/v1/embeddings` with the model | 400, same pod error |
| POST `/v1/decisions` with the model | 400, pod error: "requires a generation model" |
| POST `/v1/rerank` with the model | 400, same pod error |

---

## Limits and gotchas

- **No generation on this checkpoint.** Trying to chat, embed, or rerank
  reaches the pod's own refusal. Treat the model as a typed oracle, not a chat
  model.

- **`output_tokens` is always 0.** Cost trackers that bill by output tokens
  will report zero. Use `input_tokens` for usage accounting.

- **Caps per question.** 255 options per `choice`, 10 levels per `score`,
  any number of questions per request. The sglang backend issues one scoring
  pass per question internally, so a 10-question call is 10 forward passes
  bundled into one HTTP round trip.

- **`temperature` / `prompt_format_version` / `return_prompt_token_ids` are
  rejected by name.** The pod's error says "use /v1/decisions for it", but
  `/v1/decisions` itself refuses this checkpoint. These knobs are effectively
  unreachable on this deployment.

- **No fallback model group.** The gateway registers `Cloudflare/clef-flash`
  with `mode: None`, so LiteLLM's mode-based routing (and router fallbacks)
  cannot target it. If you need failover, register a second deployment of the
  same checkpoint and configure fallbacks explicitly at the model group.

- **`state` accepts an object but the schema says string.** The pod tolerated
  `state: {"ticket":"..."}` in probes; the published contract does not.
  Stringify before sending (`json.dumps(state)`) for portability across
  sglang versions.

- **No deterministic replay:** there is no `seed` or reproducibility knob.
  The same input can yield slightly different probabilities between calls.

- **The model is not generative, so do not send prompts expecting completion
  text.** A free-form "what do you think about X?" will be interpreted through
  the question schema and produce no useful answer. Always phrase inputs as
  a state + structured questions.

---

## When to use what

| Need | Use |
|---|---|
| Classify one user message into a fixed intent set | `choice` with 5-10 options |
| Get a "how bad is this?" 0-5 rating | `score` with 6 level names |
| Get a yes/no confidence for a gating decision | `noul` with explicit `criteria` |
| Multi-label each ticket with several independent signals at once | bundle them into one call |
| Open-ended text generation | **do not**; pick a generation model (Qwen / DeepSeek / GLM) |
| Generic OpenAI-style embeddings | **do not**; pick a dedicated embedding model |
