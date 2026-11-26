# Nemotron Transcribe Gateway Latency & Burst Connection Drops — Problem & Solution

**Gateway:** `https://litellm.ecouncil.ae` (LiteLLM proxy, 2 replicas × 4 Granian ASGI workers)
**Model:** `nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B` (`hosted_vllm` → OICM NIM service, 1× H200)
**Endpoint:** `POST /v1/audio/transcriptions` (multipart)
**Discovered:** 2026-11-18 (during authorized gateway burst validation)
**Status:** Root cause of latency **diagnosed** (ingress annotation bug — one-word fix, not yet applied). Root cause of the 12 dropped connections **not conclusively identified** (evidence rotated).

---

## 1. Summary for the gateway team

Through the public gateway, burst audio-transcription latency is **~14–160× worse than the same service reached directly**, and a 768-way burst dropped 12 of 768 connections without any HTTP response. Both symptoms concentrate in the **delivery tier in front of the LiteLLM application**, not in LiteLLM CPU work and not in NIM inference:

- A **one-word ingress annotation bug** makes nginx buffer every request body in full before forwarding (§3.1). The annotation `proxy-request-buffering: "false"` is **invalid** — ingress-nginx accepts only `on`/`off` — so it is silently ignored and the default `on` applies. All three controller pods verified.
- The LiteLLM transcription route then **re-buffered the fully-received body three more times** before NIM sees it (§3.2): `request.form()` → unbounded `await file.read()` into `BytesIO` → a second full `.read()` in `process_audio_file()` → a rebuilt non-streaming upstream multipart POST.
- Measured with the gateway's own receive-anchored timing header (`x-litellm-response-duration-ms`), **≥90% of client-observed latency at high concurrency elapsed before the LiteLLM app began processing the request** (§4): at 768-way, client p50 was **73.8 s** while the app-side window (auth + parse + routing + NIM round-trip + response transform) was p50 **0.170 s**.
- The 12 failures were bare `RemoteDisconnected` (socket closed, no response bytes) at **67.2–84.3 s** into the request. No nginx access-log evidence remains (logs rotated), no 429 was ever issued by any rate limiter, no NIM/GPU fault occurred. §5 separates what is and is not proven.

**Fix (§6):** change the annotation value to `"off"` in `deploy/prod/litellm-ingress.yaml`, apply, verify the rendered nginx.conf, then re-burst in stages (64→128→256) before anything larger. Optionally stream the `UploadFile` in the proxy route instead of double-reading it. Neither change is applied yet — both touch production and need explicit authorization.

---

## 2. Observed problem (measured, 2026-11-18)

Client harness: 10.885 s unique WAV clips (~349 KB multipart each), no retries, success = HTTP 200 + non-empty transcript. Raw per-request evidence in `/tmp/nim-gateway-validation-20261118/burst-gateway-{64,128,256,768}.json` (volatile, not committed; summaries in `docs/usage-guides/NEMOTRON_TRANSCRIBE_GUIDE.md` §5).

| Burst | Successes | Wall | Client p50 | Client p95 | `x-litellm-response-duration-ms` p50 | Completions/s |
|---:|---:|---:|---:|---:|---:|---:|
| 64 | 64/64 | 8.0 s | 7.04 s | 7.43 s | 0.433 s | 8.00 |
| 128 | 128/128 | 10.0 s | 8.51 s | 9.89 s | 0.095 s | 12.80 |
| 256 | 256/256 | 34.0 s | 25.85 s | 30.05 s | 0.332 s | 7.52 |
| 768 | **756/768** | 95.0 s | 73.81 s | 90.76 s | 0.170 s | 7.95 |

Reference baselines (same NIM pod, same clips, in-pod, bypassing gateway): direct 768/768 in **5.4 s**; in-pod streaming alias 768/768 in **4.5 s**.

Key shapes:

1. **Throughput plateaus at ~8/s** from 256→768 while p50 nearly triples (25.9→73.8 s). That is queueing behavior, not inference saturation — NIM demonstrably completes 768 in-pod requests in ~5 s when the delivery tier is absent.
2. **The app-window header stays flat** (0.09–0.43 s p50) across all burst sizes even as client latency explodes. Once a request reaches the app, everything downstream — including the NIM round-trip — completes in a few hundred milliseconds.
3. **12 hard drops, zero HTTP errors.** All 756 successes carry the timing header; all 12 failures have `response_duration_ms: null` — the requests never got far enough to be timed, and no response bytes ever arrived.

---

## 3. Root cause analysis

### 3.1 Ingress buffers every request body — annotation value is invalid (CONFIRMED)

The production Ingress for `litellm.ecouncil.ae` carries:

```yaml
nginx.ingress.kubernetes.io/proxy-request-buffering: "false"   # ← INVALID
```

ingress-nginx accepts only **`on`** or **`off`** for this annotation. The string `"false"` is neither, so the annotation is **silently ignored** and the default `on` applies. Verified in the generated config on **all three** controller pods (`nginx-ingress-controller-759649f8cc-{82ffq,h6wgt,wdp5z}`, image `bitnamilegacy/nginx-ingress-controller:1.12.1`), `location /` block for `adeo-litellm/litellm-proxy`:

```nginx
location / {
    ...
    client_max_body_size                    0;
    proxy_read_timeout                      3600s;
    proxy_buffering                         off;      # responses: fine
    proxy_max_temp_file_size                1024m;
    proxy_request_buffering                 on;       # ← THE PROBLEM
    proxy_http_version                      1.1;
    proxy_pass http://upstream_balancer;
}
```

Proof that the parser handles the correct value fine: the MCP ingress (`litellm-mcp-reverse-h2-dev`) on the same controllers annotates `"off"` (bare) and renders `proxy_request_buffering off;` correctly.

Consequences at burst scale, with `client_body_buffer_size 8k` at http level:

- nginx must receive **each request body in full** before opening/forwarding to upstream. A 768-way burst is ~267 MB of bodies that must land first, with anything over 8 KiB per request **spilled to temp files** and read back.
- The LiteLLM app cannot begin auth, parsing, or routing until nginx finishes receiving that request's body — so the app stays effectively idle (which is exactly what the flat timing header shows) while queues build in the delivery tier.
- Effective concurrency toward the app is throttled by nginx accept/body-read throughput, matching the observed ~8 completions/s plateau.

This is a **delivery-tier serialization defect**. It does not by itself explain the 12 socket drops (§5).

### 3.2 The LiteLLM app re-buffers the body three more times (CONFIRMED, source-audited)

Even after nginx delivers a body, the request path materializes the full audio repeatedly before NIM sees a byte:

1. **Route parses the whole form** — `get_form_data` (`await request.form()`), `litellm/proxy/common_utils/http_parsing_utils.py`, called at `litellm/proxy/proxy_server.py` transcription route (~L13132).
2. **Unbounded full read** — the route does `file_content = await file.read()` (no size cap) and wraps it in `io.BytesIO` (~L13167-13168).
3. **Second full read** — the hosted-vllm transformation calls `process_audio_file`, which does another complete `.read()` of the same object (`litellm/llms/hosted_vllm/transcriptions/transformation.py`, `litellm/litellm_core_utils/audio_utils/utils.py` ~L118-125).
4. **Rebuilt non-streaming upstream POST** — `async_audio_transcriptions` in `litellm/llms/custom_httpx/llm_http_handler.py` assembles a fresh multipart `files=` request and awaits the complete response (`aiohttp` connector limits: 1000 total / 500 per host per process, `litellm/constants.py` ~L359-360).

At the tested 349 KB clips this costs RAM and churn but only **milliseconds** of CPU — which is why the timing header stays small and why this is a **secondary** contributor to burst latency (it matters for memory pressure and larger files, not for the tens-of-seconds waits). The in-pod streaming alias, by contrast, relays bounded chunks and never materializes a body (`oicm_health_alias.py::_body_stream`/`_forward`).

### 3.3 What is NOT the cause (ruled out with evidence)

- **Not NIM/GPU:** NIM pod stayed Ready, 0 restarts; direct/alias in-pod baselines at the same burst size complete in ~5 s; app-side window (which contains the NIM round trip) is p50 ≤ 0.43 s in every run.
- **Not any rate limiter:** both the standard parallel-request limiter and the production HTB limiter (`dynamic_rate_limiter_v3_htb`) enforce by raising an explicit **429** — they never silently queue or drop. The 768-run recorded zero 429s. Key-scope was inference-routes only, but no RPM/concurrency cap fired.
- **Not a fixed gateway/ingress timeout:** effective `proxy_read/send_timeout` are 3600 s; LiteLLM's transcription timeout default is 600 s; drops at 67–84 s match none of them, and a bare close without response bytes is not how these layers time out (they would emit 408/502/504).
- **Not LiteLLM worker saturation:** 8 ASGI workers sat nearly idle per the flat timing header; pod fd limit 1,048,576; no OOM (memory limit 16 Gi with headroom, no restarts).
- **Not client GIL death:** the harness used ~0.9 core at 768-way with per-request CPU ~0.11 s — not the shape of a 90 s queueing collapse; and the direct baseline differs only in delivery path.

---

## 4. Why we can localize the wait to the delivery tier

The gateway stamps `x-litellm-response-duration-ms` from `litellm_received_at` — set at the **top of auth** (`litellm/proxy/auth/user_api_key_auth.py` ~L1921), propagated through routing into the logging object, and computed in `litellm/litellm_core_utils/llm_response_utils/response_metadata.py::response_timing_metrics` as end-to-receive. So the header spans **auth + form parse + routing + NIM round-trip + response transform** — everything the app does after nginx hands the request over. It excludes only nginx and the network before the app.

That makes the gap decisive:

| Burst | Client p50 | App window p50 | Implied pre-app wait p50 |
|---:|---:|---:|---:|
| 64 | 7.04 s | 0.43 s | ~6.6 s |
| 128 | 8.51 s | 0.10 s | ~8.4 s |
| 256 | 25.85 s | 0.33 s | ~25.2 s |
| 768 | 73.81 s | 0.17 s | ~73.8 s |

At 768-way the app finished its entire job for the median successful request in **0.17 s** while the client waited **73.8 s**. The wait is upstream of the app — in the tier where §3.1 showed bodies are fully buffered before forwarding.

---

## 5. The 12 dropped connections — proven vs. unproven

**Observed:** 12/768 requests failed with `RemoteDisconnected: Remote end closed connection without response` at 67.18, 67.83, 69.21, 69.24, 71.48, 71.53, 72.13, 77.65, 77.95, 79.30, 80.94, 84.28 s. All 12 have no timing header — the app never saw them complete.

**Proven:**
- They are transport-level terminations (FIN/RST with zero response bytes), not HTTP errors, not 429s, not retried calls.
- No configured timeout in any inspected layer (nginx 3600 s, LiteLLM 600 s, connect 5 s) equals 67–84 s.
- No NIM fault, no OOM kill, no proxy restart, no rate-limiter engagement coincided.
- nginx access logs have **rotated** since the burst (retention ≈ minutes; only ~1.5 k lines present, none covering the window) — server-side forensic evidence no longer exists.

**Not proven (do not claim any of these):**
- That request buffering caused the drops (it is the strongest *latency* cause; drop causation is separate).
- Which hop closed the socket: the client-side intermediary network, the load path in front of ingress, nginx itself, or the upstream. A bare close is consistent with an intermediary idle/flow timeout (~60 s + queueing overhead fits the 67–84 s spread) but this is hypothesis, not evidence.

If drops recur, capture within minutes: nginx access + error logs from all three controllers (`kubectl logs`, plus `/var/log/nginx` if configured), LiteLLM proxy logs from both replicas, and client-side `strace`/packet timing on a few connections.

---

## 6. Solution

### 6.1 Primary fix — make ingress request buffering actually `off` (one word)

**Where to change — every location carrying the invalid value.** The Ingress is managed as a plain manifest applied with `kubectl apply` (no kustomize overlay owns it, no GitOps controller rewrites it), so the manifest in this repo is the source of truth — the live object must be changed *through the manifest*, never hand-edited, or the next `kubectl apply` of any prod manifest silently reverts it:

| File | Line | Object | Current value | Change to |
|---|---:|---|---|---|
| `deploy/prod/litellm-ingress.yaml` | 11 | Ingress `litellm-proxy` (ns `adeo-litellm`) | `"false"` | `"off"` |
| `deploy/rollback/access.yaml` | 10 | rollback-path Ingress copy | `"false"` | `"off"` |

Applied 2026-11-19: both files now carry `"off"`, and the live Ingress renders
`proxy_request_buffering off;` on all three controllers. The same change also
added `nginx.ingress.kubernetes.io/proxy-max-temp-file-size: "0"` to both, so an
unbuffered response can never spill to a temp file when the far end stops
reading. Before that, the rendered block still carried ingress-nginx's default
`proxy_max_temp_file_size 1024m`, which is real disk buffering even with
`proxy_buffering off`.

Two more files carry the annotation and are **already correct** (verify only, do not touch): `deploy/overlays/dev/mcp-source-broker/tunnel-ingress.yaml` uses `"off"` (bare), and the live MCP ingress renders `proxy_request_buffering off;` — this pair is the proof the parser honors the correct spelling.

**How to change — manifest edit, then apply:**

```yaml
# deploy/prod/litellm-ingress.yaml, metadata.annotations
# BEFORE (invalid — silently ignored by ingress-nginx, default "on" applies)
nginx.ingress.kubernetes.io/proxy-request-buffering: "false"

# AFTER (valid values are exactly: "on" | "off")
nginx.ingress.kubernetes.io/proxy-request-buffering: "off"
```

Do **not** fix this with `kubectl annotate`/`kubectl edit` on the live object alone — without the manifest change the value silently reverts on the next apply. The supported procedure:

1. Edit both manifests above in a commit (prod + rollback copy together, so a rollback cannot reintroduce the bug).
2. Dry-run first: `kubectl apply --dry-run=server -f deploy/prod/litellm-ingress.yaml`.
3. Apply: `kubectl apply -f deploy/prod/litellm-ingress.yaml`.
4. Confirm the controller accepted the annotation: `kubectl -n adeo-litellm get ingress litellm-proxy -o jsonpath='{.metadata.annotations.nginx\.ingress\.kubernetes\.io/proxy-request-buffering}'` → must print `off`.
5. Wait for the controller reload, then verify the **rendered** config on each of the three controller pods — the generated nginx.conf is ground truth, and it is exactly where the bug was found. Extract the `litellm-proxy` server block first, then read the three directives from it; matching `ingress_name "litellm-proxy"` alone is not enough, because that string appears in more than one place and a naive `awk` walks into unrelated server blocks that legitimately leave buffering `on`:
   ```bash
   P=<controller-pod>
   L=$(kubectl -n nginx-ingress-controller exec $P -c controller -- sh -c \
     'grep -n "server_name litellm.ecouncil.ae" /etc/nginx/nginx.conf | head -1 | cut -d: -f1')
   kubectl -n nginx-ingress-controller exec $P -c controller -- sh -c \
     "sed -n '${L},$((L+120))p' /etc/nginx/nginx.conf | grep -E 'proxy_request_buffering|proxy_buffering|proxy_max_temp_file_size'"
   ```
   Must print all three:
   ```nginx
   proxy_buffering                         off;
   proxy_max_temp_file_size                0;
   proxy_request_buffering                 off;
   ```
6. Re-run the staged bursts below.

### 6.2 Secondary fix — stream the audio in the LiteLLM route (optional, code change)

For the transcription route, pass the `UploadFile` through to httpx without the intermediate `BytesIO` + second `read()` (or stream via `UploadFile`'s async iterator), so the app forwards chunks as they arrive instead of holding two full copies per request. This reduces memory pressure and helps large files; it does **not** fix delivery-tier queueing by itself. Upstream change — needs its own review and tests.

### 6.3 Validation plan (staged — do NOT jump straight to 768)

1. 64-way → expect p50 to drop materially below 7 s; confirm zero errors.
2. 128-way → compare against the recorded 8.51 s p50.
3. 256-way → the canary that most cleanly exposes the plateau: before the fix it ran 25.85 s p50 / 7.52 completions/s.
4. Only after 256 shows the delivery tier no longer plateaus, consider 768 with drop-capture (§5) armed.

### 6.4 Monitoring to add while re-testing

- nginx controller access logs with `$request_time`, `$upstream_response_time`, `$body_bytes_sent` — watch for 499/502/504 during bursts.
- Prometheus: LiteLLM request-duration vs. the client-side numbers (the divergence between them is the direct measure of delivery-tier queueing).

### 6.5 Rollback

The change is a single annotation value with no application-side counterpart; reverting is `kubectl apply` of the previous manifest revision (`git revert` of the fix commit). Because `deploy/rollback/access.yaml` is fixed in the same commit, the rollback path cannot silently reintroduce the buffering bug. Rollback is only warranted if `"off"` exposes an unexpected incompatibility (e.g. a client that relied on nginx absorbing slow uploads); expect slower ingress I/O under burst in that case, not errors.

---

## 7. Related documents

- Usage guide with full benchmark context: `docs/usage-guides/NEMOTRON_TRANSCRIBE_GUIDE.md`
- Burst harness (env-var auth, no secrets): `examples/usage/nemotron-transcribe/benchmark_gateway.py`
- Streaming alias (per-request chunked relay, the behavior the delivery tier lacks): `oicm-custom-model-server-guide/examples/NIM-podman-nemotron-transcribe-1.0.0-amd64/oicm_health_alias.py`
- Session scratch pad with the full verification transcript state: `/memories/session/litellm-gateway-verification.md`

---

## 8. Revision history

| Date | Author | Change |
|---|---|---|
| 2026-11-18 | Gateway validation session | Initial report: root cause of latency (invalid `proxy-request-buffering` value + app-side re-buffering), measured localization via timing header, drop analysis with proven/unproven separation, staged fix & validation plan. |
| 2026-11-18 | Solution-section audit | §6.1 hardened: pinned both manifest locations carrying the bug (prod + rollback copy), manifest-not-live-object procedure with server dry-run, per-controller rendered-config verification commands, and a §6.5 rollback note. |
| 2026-11-19 | Ingress applied | Both manifests carried `"off"` already; added `proxy-max-temp-file-size: "0"` to both, because the rendered block still had the `1024m` default (disk spill even with `proxy_buffering off`). Applied to the live Ingress and verified all three rendered blocks now read `proxy_buffering off` / `proxy_max_temp_file_size 0` / `proxy_request_buffering off`. Also fixed the step-4 jsonpath typo (`ngress`) and replaced the step-5 `awk` with a server-block extraction, because the old one matched unrelated blocks that legitimately leave buffering `on`. |
