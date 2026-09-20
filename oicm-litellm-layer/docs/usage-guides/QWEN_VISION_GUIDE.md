# Qwen 3.5 122B Vision Usage Guide

This guide covers using `Qwen/Qwen3.5-122B-A10B-GPTQ-Int4` for image (vision) understanding through the LiteLLM gateway.

**Gateway Base URL**: `https://litellm.ecouncil.ae/v1`

**API Key**: replace `<YOUR_API_KEY>` everywhere below with your LiteLLM gateway API key (e.g. `sk-...`). All requests use the `Authorization: Bearer <YOUR_API_KEY>` header.

The gateway is SSL-verified, so the curl examples below use standard HTTPS verification (no `-k`/`--insecure` flag).

---

## Model Overview

| Property | Value |
|---|---|
| Model ID | `Qwen/Qwen3.5-122B-A10B-GPTQ-Int4` |
| Type | Chat completions + vision (multimodal) |
| Reasoning | Yes by default (emits `reasoning_content`), can be disabled |
| Served via | vLLM |

This model accepts images through the standard OpenAI `image_url` content type, sent either as a base64 data URL or a public HTTP URL.

> **Important**: Qwen3.5-122B is a **reasoning model**. By default it emits chain-of-thought in `reasoning_content` before the final `content` answer. Use a high `max_tokens` (1000+), otherwise it can exhaust the budget on reasoning alone and return `content: null`.

---

## 1. Vision (image) understanding

### Endpoint

```
POST /v1/chat/completions
```

### Base64 data URL (local image)

```bash
# Encode a local image to base64
B64=$(base64 -w0 /path/to/image.png)

curl -s -X POST "https://litellm.ecouncil.ae/v1/chat/completions" \
  -H "Authorization: Bearer <YOUR_API_KEY>" \
  -H "Content-Type: application/json" \
  -d "{
    \"model\": \"Qwen/Qwen3.5-122B-A10B-GPTQ-Int4\",
    \"messages\": [{
      \"role\": \"user\",
      \"content\": [
        {\"type\": \"text\", \"text\": \"What do you see in this image? Describe it.\"},
        {\"type\": \"image_url\", \"image_url\": {\"url\": \"data:image/png;base64,${B64}\"}}
      ]
    }],
    \"max_tokens\": 1000
  }"
```

### Public image URL

```bash
curl -s -X POST "https://litellm.ecouncil.ae/v1/chat/completions" \
  -H "Authorization: Bearer <YOUR_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen3.5-122B-A10B-GPTQ-Int4",
    "messages": [{
      "role": "user",
      "content": [
        {"type": "text", "text": "What color is this image? Answer in one word."},
        {"type": "image_url", "image_url": {"url": "https://example.com/photo.jpg"}}
      ]
    }],
    "max_tokens": 1000
  }'
```

### Response

```json
{
  "id": "chatcmpl-...",
  "model": "Qwen/Qwen3.5-122B-A10B-GPTQ-Int4",
  "choices": [{
    "message": {
      "role": "assistant",
      "reasoning_content": "Thinking Process: ...",
      "content": "The image is a solid red square.",
      "provider_specific_fields": {"reasoning": "...", "refusal": null}
    }
  }],
  "usage": {"prompt_tokens": 345, "completion_tokens": 18, "total_tokens": 363}
}
```

---

## 2. Multiple images in one request

The `content` array can contain multiple `image_url` items, each with a distinct label:

```bash
curl -s -X POST "https://litellm.ecouncil.ae/v1/chat/completions" \
  -H "Authorization: Bearer <YOUR_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen3.5-122B-A10B-GPTQ-Int4",
    "messages": [{
      "role": "user",
      "content": [
        {"type": "text", "text": "Are these two images the same object?"},
        {"type": "image_url", "image_url": {"url": "https://example.com/a.jpg"}},
        {"type": "image_url", "image_url": {"url": "https://example.com/b.jpg"}}
      ]
    }],
    "max_tokens": 1000
  }'
```

---

## 3. Disable reasoning (non-thinking mode)

For faster, lower-cost responses you can turn off chain-of-thought. The parameter is **vLLM `chat_template_kwargs`**, specific to Qwen:

```bash
curl -s -X POST "https://litellm.ecouncil.ae/v1/chat/completions" \
  -H "Authorization: Bearer <YOUR_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen3.5-122B-A10B-GPTQ-Int4",
    "messages": [
      {"role": "user", "content": "Say hello in one word."}
    ],
    "chat_template_kwargs": {"enable_thinking": false},
    "max_tokens": 500
  }'
```

Verified live: this returns `content: "Hello"` with `reasoning_content: null`.

> **Provider-specific note:** The parameter that disables reasoning differs by provider. For **Qwen** (vLLM) use `chat_template_kwargs: {"enable_thinking": false}`. For **DeepSeek**, use `thinking: false` instead. Do not mix them up.

---

## 4. Streaming vision

```bash
curl -s -N -X POST "https://litellm.ecouncil.ae/v1/chat/completions" \
  -H "Authorization: Bearer <YOUR_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen3.5-122B-A10B-GPTQ-Int4",
    "messages": [{
      "role": "user",
      "content": [
        {"type": "text", "text": "Describe this image briefly."},
        {"type": "image_url", "image_url": {"url": "https://example.com/photo.jpg"}}
      ]
    }],
    "stream": true,
    "max_tokens": 1000
  }'
```

---

## 5. Python example (urllib, SSL-verified)

```python
import base64
import json
import ssl
import urllib.request

b64 = base64.b64encode(open("/path/to/image.png", "rb").read()).decode()
data_url = "data:image/png;base64," + b64

payload = {
    "model": "Qwen/Qwen3.5-122B-A10B-GPTQ-Int4",
    "messages": [{
        "role": "user",
        "content": [
            {"type": "text", "text": "What is in this image?"},
            {"type": "image_url", "image_url": {"url": data_url}},
        ],
    }],
    "max_tokens": 1000,
}

req = urllib.request.Request(
    "https://litellm.ecouncil.ae/v1/chat/completions",
    data=json.dumps(payload).encode(),
    headers={"Authorization": "Bearer <YOUR_API_KEY>", "Content-Type": "application/json"},
)
resp = urllib.request.urlopen(req, context=ssl.create_default_context())
data = json.load(resp)
print(data["choices"][0]["message"]["content"])
```

---

## Quick reference

| Feature | Supported | Notes |
|---|---|---|
| Text chat | Yes | reasoning model, use high `max_tokens` |
| **Images (vision)** | **Yes** | base64 data URL or public URL via `image_url` |
| Multiple images | Yes | multiple `image_url` items |
| Streaming | Yes | `"stream": true` |
| Disable reasoning | Yes | Qwen: `chat_template_kwargs: {"enable_thinking": false}` |
| DeepSeek disable reasoning | Yes | DeepSeek: `thinking: false` |