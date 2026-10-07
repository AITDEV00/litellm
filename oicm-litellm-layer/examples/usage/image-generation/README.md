# Qwen-Image-2.1 Usage Guide

This guide covers text-to-image generation and image editing through the LiteLLM
gateway using `Qwen/Qwen-Image-2.1`, a vLLM-served diffusion model.

**Gateway base URL**: `https://litellm.ecouncil.ae/v1`

**API key**: replace `<YOUR_API_KEY>` everywhere below with your LiteLLM gateway
key (e.g. `sk-...`). Every request uses the
`Authorization: Bearer <YOUR_API_KEY>` header. The gateway certificate is valid
and publicly trusted, so no `-k` flag is needed.

| Property | Value |
|---|---|
| Model | `Qwen/Qwen-Image-2.1` |
| Endpoints | `POST /v1/images/generations`, `POST /v1/images/edits` |
| Content types | JSON for generations, `multipart/form-data` for edits |
| Default size | 1024x1024 |
| Default steps | 40 |
| Default format | `png` (RGBA, keeps alpha) |
| Divisible-by-32 rule | height and width must both be multiples of 32 |
| Batching | generations only, up to 4 concurrent same-shape requests |

A runnable harness lives beside this guide as `test_image_endpoints.sh`. It
exercises every parameter documented here and saves each request, response, and
output image. Run it from this directory:

```bash
cd oicm-litellm-layer/examples/usage/image-generation
export PROXY_BASE_URL="https://litellm.ecouncil.ae"
export LITELLM_API_KEY="sk-..."
./test_image_endpoints.sh                 # all cases
./test_image_endpoints.sh gen-size-512    # one case
```

Results land in `out/generations/<case>/` and `out/edits/<case>/`. For a single
ad-hoc prompt, `./gen_image.sh "<prompt>"` writes one PNG under
`out/generations/`.

---

## 1. Text-to-image generation

`POST /v1/images/generations`, JSON body.

### Minimal request

```bash
curl -sS --fail-with-body "https://litellm.ecouncil.ae/v1/images/generations" \
  -H "Authorization: Bearer <YOUR_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen-Image-2.1",
    "prompt": "A capybara reading a book by candlelight",
    "response_format": "b64_json"
  }'
```

The response carries the image in `data[0].b64_json`:

```json
{
  "created": 1791358052,
  "data": [{ "b64_json": "<base64 png>" }],
  "output_format": null,
  "quality": null,
  "size": null,
  "background": null,
  "usage": { "total_tokens": 0, "input_tokens": 0, "output_tokens": 0 }
}
```

Note that `size`, `background`, `quality`, and `output_format` come back `null`
even when you set them, and `usage` is all zeros. Do not use the response
metadata to confirm what the server did; check the decoded image instead.

Decode and save:

```bash
curl -sS --fail-with-body "https://litellm.ecouncil.ae/v1/images/generations" \
  -H "Authorization: Bearer <YOUR_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen-Image-2.1",
    "prompt": "A capybara reading a book by candlelight",
    "response_format": "b64_json"
  }' | python3 -c "import base64,json,sys; open('out.png','wb').write(base64.b64decode(json.load(sys.stdin)['data'][0]['b64_json']))"
```

### Parameters

| Parameter | Type | Default | Effect |
|---|---|---|---|
| `prompt` | string | required | The image description |
| `size` | string | `1024x1024` | `WIDTHxHEIGHT`, both multiples of 32 |
| `n` | int | 1 | Number of images |
| `response_format` | string | `url` | Use `b64_json` to get the image inline |
| `output_format` | string | `png` | `png` or `webp`. **`jpeg` returns HTTP 500** |
| `num_inference_steps` | int | 40 | Denoising steps |
| `seed` | int or list[int] | 42 | Reproducibility; a list must match `n` |
| `guidance_scale` | float | 1.0 | Classifier-free guidance; needs `negative_prompt` |
| `negative_prompt` | string | none | Only meaningful with `guidance_scale > 1` |
| `background` | string | `auto` | **No-op on generations**; use edits for alpha |
| `task_type` | string | none | This pipeline accepts only `TI2I` |
| `generator_device` | string | `cuda` | Changes the output; `cpu` is accepted |
| `enhance_prompt` | bool | false | **Returns HTTP 400** without server config |
| `flow_shift`, `enable_teacache`, `max_sequence_length` | | | Accepted but no effect here |

### Verified working

**Resolution.** Any size whose dimensions are multiples of 32 works. Tested
512x512, 768x768, 1024x768 (non-square), and the 1024x1024 default. The size is
honored exactly.

```bash
-d '{"model":"Qwen/Qwen-Image-2.1","prompt":"A capybara","size":"768x768","response_format":"b64_json"}'
```

**Multiple images.** `n=2` returns two images in one response.

```bash
-d '{"model":"Qwen/Qwen-Image-2.1","prompt":"A capybara","n":2,"size":"512x512","response_format":"b64_json"}'
```

**Reproducibility.** A fixed `seed` makes output deterministic: identical
requests return byte-identical images, so a seed plus a hash is a reliable way
to tell whether a parameter took effect.

```bash
-d '{"model":"Qwen/Qwen-Image-2.1","prompt":"A capybara","seed":1234,"size":"512x512","response_format":"b64_json"}'
```

**A list of seeds** needs `n` to match the list length:

```bash
-d '{"model":"Qwen/Qwen-Image-2.1","prompt":"A capybara","n":2,"seed":[1,2],"size":"512x512","response_format":"b64_json"}'
```

**Guidance.** Classifier-free guidance needs both fields together. On its own,
`guidance_scale` above 1 has nothing to steer away from.

```bash
-d '{"model":"Qwen/Qwen-Image-2.1","prompt":"A capybara","guidance_scale":7,"negative_prompt":"blurry, low quality, watermark","size":"512x512","response_format":"b64_json"}'
```

**WebP** is the alternative to PNG and keeps the alpha channel.

```bash
-d '{"model":"Qwen/Qwen-Image-2.1","prompt":"A capybara","output_format":"webp","size":"512x512","response_format":"b64_json"}'
```

### Passing parameters LiteLLM does not forward

LiteLLM's generation path accepts only the OpenAI image params
(`background`, `moderation`, `n`, `output_compression`, `output_format`,
`quality`, `size`, `user`). Other parameters still reach the backend, so send
them as usual. `extra_body` is the explicit passthrough route if a parameter is
ever filtered:

```bash
-d '{"model":"Qwen/Qwen-Image-2.1","prompt":"A capybara","size":"512x512","seed":42,"response_format":"b64_json","extra_body":{"num_inference_steps":20,"flow_shift":3}}'
```

### Known failures

`output_format: "jpeg"` returns HTTP 500 every time. The model emits RGBA and
JPEG cannot store an alpha channel, and the encoder failure surfaces as an
internal server error rather than a 400. Use `png` or `webp`.

`enhance_prompt: true` returns HTTP 400 with
`Prompt enhancement requires --prompt-enhancer-config`. The server does not have
a prompt enhancer configured.

`task_type` accepts only `TI2I` on this pipeline. `T2I` and `text_to_image` are
rejected with `Unsupported task_type ...; this pipeline supports ['TI2I']`.

`background: "transparent"` is silently ignored on generations. No alpha is
produced, not even when the parameter is routed through `extra_body`. Use the
edits endpoint for transparency.

---

## 2. Image editing

`POST /v1/images/edits`, `multipart/form-data`.

### Minimal request

```bash
curl -sS --fail-with-body "https://litellm.ecouncil.ae/v1/images/edits" \
  -H "Authorization: Bearer <YOUR_API_KEY>" \
  -F "model=Qwen/Qwen-Image-2.1" \
  --form-string 'prompt=Change the red teapot to blue, keeping everything else unchanged.' \
  --form-string 'response_format=b64_json' \
  -F "image[]=@/path/to/input.png;type=image/png"
```

Use `--form-string` for the text fields so curl does not try to treat the value
as a file path, and `-F` for the image uploads.

### Parameters

| Parameter | Type | Default | Effect |
|---|---|---|---|
| `image[]` | file | required | One or more input images |
| `prompt` | string | required | The edit instruction |
| `size` | string | model default | Output size; **`width`/`height` are not exposed here** |
| `n` | int | 1 | Number of outputs |
| `response_format` | string | `url` | Use `b64_json` |
| `output_format` | string | `png` | `png` or `webp`; `jpeg` returns HTTP 500 |
| `background` | string | `auto` | `transparent` produces a real alpha channel |
| `seed` | int | 42 | Reproducibility |
| `num_inference_steps` | int | 40 | Denoising steps |
| `guidance_scale` | float | 1.0 | Needs `negative_prompt` |
| `negative_prompt` | string | none | Only with `guidance_scale > 1` |
| `mask` | file | none | **Stripped by LiteLLM for this provider** |
| `enhance_prompt` | bool | false | **Returns HTTP 400** without server config |
| `enable_teacache` | bool | false | Accepted but no effect here |
| `url` | string | none | **Returns HTTP 500**; use the `image[]` file field |

### Verified working

**Single image edit.**

```bash
curl -sS --fail-with-body "https://litellm.ecouncil.ae/v1/images/edits" \
  -H "Authorization: Bearer <YOUR_API_KEY>" \
  -F "model=Qwen/Qwen-Image-2.1" \
  --form-string 'prompt=Change the red teapot to blue.' \
  --form-string 'size=512x512' \
  --form-string 'response_format=b64_json' \
  -F "image[]=@/path/to/input.png;type=image/png"
```

**Two images in one request.** Repeat the `image[]` field for each input. The
model combines them according to the prompt.

```bash
curl -sS --fail-with-body "https://litellm.ecouncil.ae/v1/images/edits" \
  -H "Authorization: Bearer <YOUR_API_KEY>" \
  -F "model=Qwen/Qwen-Image-2.1" \
  --form-string 'prompt=Combine the subjects from Picture 1 and Picture 2 into one coherent scene, preserving their appearance.' \
  --form-string 'size=512x512' \
  --form-string 'response_format=b64_json' \
  -F "image[]=@/path/to/input.png;type=image/png" \
  -F "image[]=@/path/to/reference.png;type=image/png"
```

**Transparent background.** This is the one place transparency works. Add
`background=transparent` and keep `output_format=png`.

```bash
curl -sS --fail-with-body "https://litellm.ecouncil.ae/v1/images/edits" \
  -H "Authorization: Bearer <YOUR_API_KEY>" \
  -F "model=Qwen/Qwen-Image-2.1" \
  --form-string 'prompt=Combine the subjects into one composition on a transparent background. Preserve an alpha channel outside the subjects.' \
  --form-string 'size=512x512' \
  --form-string 'background=transparent' \
  --form-string 'output_format=png' \
  --form-string 'response_format=b64_json' \
  -F "image[]=@/path/to/input.png;type=image/png" \
  -F "image[]=@/path/to/reference.png;type=image/png"
```

Verified: with `background=transparent` the output has roughly 15 percent fully
transparent pixels; without it the output is fully opaque.

**Multiple outputs** with `n=2`.

**Mixed input resolutions.** Inputs of different sizes are accepted; the output
size follows `size` or the model default, not the inputs.

**WebP output** via `output_format=webp`.

### Known failures

`url=` instead of an image file returns HTTP 500 with
`aimage_edit() missing 1 required positional argument: 'image'`. LiteLLM's edits
endpoint requires the `image` file field.

`mask=` is stripped before the request reaches the backend. This is deliberate:
LiteLLM's `HostedVLLMImageEditConfig` removes `mask`, `quality`, and
`input_fidelity` because the vLLM omni server does not accept them. A mask you
send has no effect.

`output_format=jpeg` fails the same way as on generations.

`enhance_prompt=true` returns HTTP 400 for the same missing server config.

---

## 3. Batching

The backend batches requests to improve throughput. Two settings govern it: a
maximum batch size of 4 and a 100 ms delay window. Batching applies to
**generations only**.

Measured behavior:

| Scenario | Result |
|---|---|
| 4 concurrent 1024x1024 generations, identical params | Merged 4/4, 61 s wall vs 244 s serial |
| 4 concurrent generations with different sizes | Split 2+1+1, no full merge |
| Concurrent edits | Never merge |

Two conditions must hold to merge. The requests have to be queued at the same
time, so sequential calls never batch no matter how fast you send them. And they
must share a batch signature, which includes every sampling field: `size`,
`num_inference_steps`, `guidance_scale`, `seed`, and the rest. Different sizes
are the most common reason a merge fails, logged as
`stop_reason=sampling_params.height`.

Edits can never merge. The scheduler rejects any request that carries an input
image, logged as `stop_reason=head:image_conditioning`.

Practical consequence: if your traffic is sequential, the 100 ms window is pure
added latency. Concurrent same-shape generation traffic is what benefits.

---

## 4. Performance and limits

Denoising time scales quadratically with resolution, because latent tokens go as
`(H/16) * (W/16)`.

| Resolution | Time per step | Relative |
|---|---:|---:|
| 512x512 | ~0.072 s | 0.25x |
| 768x768 | ~0.20 s | 0.56x |
| 1024x1024 | ~0.40 s | 1.00x |

There is no hard maximum resolution in the server. The only validation is that
height and width are multiples of 32. In practice the limit is GPU memory: the
serving slice is about 35 GB, a single 1024x1024 request peaks near 24 GB, and
with batching enabled that rose to about 30 GB in testing. Treat 1024x1024 as
the working maximum, and lower the batch size before pushing past it.

The pipeline advertises only `TI2I`, so generations and edits both go through an
image-conditioned path even for pure text-to-image prompts.

---

## 5. Quick reference

Working generation request:

```bash
curl -sS --fail-with-body "https://litellm.ecouncil.ae/v1/images/generations" \
  -H "Authorization: Bearer <YOUR_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen-Image-2.1","prompt":"A capybara reading a book by candlelight","size":"512x512","seed":42,"response_format":"b64_json"}'
```

Working edit request with transparency:

```bash
curl -sS --fail-with-body "https://litellm.ecouncil.ae/v1/images/edits" \
  -H "Authorization: Bearer <YOUR_API_KEY>" \
  -F "model=Qwen/Qwen-Image-2.1" \
  --form-string 'prompt=Change the red teapot to blue on a transparent background.' \
  --form-string 'size=512x512' \
  --form-string 'background=transparent' \
  --form-string 'output_format=png' \
  --form-string 'response_format=b64_json' \
  -F "image[]=@/path/to/input.png;type=image/png"
```

Avoid: `output_format=jpeg` (500), `background=transparent` on generations
(no-op), `mask` on edits (stripped), `url=` on edits (500), `enhance_prompt`
(400), `task_type` other than `TI2I` (400), and a `seed` list whose length is
not `n` (500).
