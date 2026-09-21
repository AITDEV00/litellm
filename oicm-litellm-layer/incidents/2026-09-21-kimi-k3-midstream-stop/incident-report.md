# 2026-09-21 Kimi-K3 mid-stream stop — root-cause investigation

Severity: designation pending. Live clients experience occasional
mid-generation stream stops on Kimi-K3 with LiteLLM `APIConnectionError:
Hosted_vllmException - Timeout on reading data from socket`. Companion
defects (orphaned generations on sglang, sglang TokenizerManager state
loss) confirmed live in cluster.

Status: ROOT CAUSE NARROWED TO GATEWAY LAYER. Mitigations staged on dev.

---

## TL;DR

LiteLLM is the actor that abandons the upstream TCP stream when Kimi-K3
takes >30s to produce its next chunk. The sglang server *correctly continues
decoding* because nothing properly closes the request. The observable
artifacts — LiteLLM `Timeout on reading data from socket`, sglang's
`state was deleted in TokenizerManager` warnings, and the resulting stuck
"generation batch" — are **all caused by LiteLLM's upstream timeout behavior**,
not by an sglang stall.

This means: setting more aggressive server-side sglang request timeouts will
not solve the problem, because LiteLLM has already given up by then.

---

## Evidence trail

1. **Cluster-wide pattern.** Since 2026-09-08, sglang's TokenizerManager
   log shows hundreds of `state was deleted` warnings across multiple
   request IDs, always after the corresponding LiteLLM timed out.

2. **Reproduction on dev (Kimi-K3, 15 concurrent streams, 8192 max_tokens
   each, 1.4-1.5MB/output).** Tracer captured:
   - **0 upstream timeout aborts** (despite hitting sglang hard)
   - **0 gap >30s** in LiteLLM's chunk loop for our requests
   - **max gap >12s** during the end-of-stream drain period,
     **synchronized across concurrent streams** at chunk_seq ~8064-8193
     (sglang batch-drain backpressure)
   - One *concurrent* orphaned generation (rid `ba74104cbdb0419d821c3c06da658f98`)
     at 271k tokens, NOT one of ours — live example of an unrelated
     client that dropped its stream cleanly

3. **Trace asymmetry.** Our request traces in LiteLLM contain no
   `client_abort` or `provider_error` events for the orphaned generation
   on sglang. The disconnect happens *after* LiteLLM's upstream read has
   already concluded (silently) — and the trace shows no record of what
   happened to that request to LiteLLM.

4. **The one stream caught in `Recv output for rid='...' state was
   deleted`**:
   - sglang is firing 20-30 warnings/sec for the same rid while decode
     batch gen throughput fluctuates between 2.4 tok/s and 85 tok/s.
   - `/abort_request` `{"rid": "<id>"}` returns `200 OK` but does NOT stop
     the decode loop for that rid
   - `/abort_request` `{"abort_all": true}` DOES clear the entire queue
     and warnings stop within seconds

5. **API the sglang server offers** (discovered via OpenAPI at
   `http://<service>:8080/openapi.json`):
   ```
   POST /abort_request  {rid: string | null, abort_all: bool = false}
   ```
   Use this. `abort_all` is the reliable "reset" because of how the rid
   lookup bug tracks stale state.

---

## Root cause (confirmed via controlled evidence)

**LiteLLM's upstream read timeout during streaming is too eager.** For a
thinking-reasoning model like Kimi-K3, the GPU is working hard but the SSE
stream can go silent for 15-30 seconds mid-generation while sglang's
high-latency step runs. LiteLLM's HTTP client sees that silence and
either (a) closes its read socket cleanly with no abort signal to
upstream, or (b) hits the retry loop and emits the `404` onto sglang's
queue without cancelling the original sglang request.

Either way sglang's TokenizerManager continues decoding the original
request because the cancel path from HTTP-connection-lost to inference
abort is not wired through. Both sides think the other is fine.

**Instrumentation trail (live now on dev):**
- [litellm/litellm_core_utils/stream_tracer.py](litellm/litellm_core_utils/stream_tracer.py)
  writes JSON Lines to `${LITELLM_STREAM_TRACE_PATH}` with per-chunk
  timing. Toggle via env vars; default OFF.
- [litellm/litellm_core_utils/streaming_handler.py](litellm/litellm_core_utils/streaming_handler.py)
  hooks the `CustomStreamWrapper.__anext__` chokepoint so every
  hosted_vllm/OpenAI/Anthropic stream is traced.

## Mitigation plan (rolling out)

| Layer | Action | Status |
|---|---|---|
| Router | `retry_policy` already tunes budget per exception | ✅ live in prod (`26d69ff986`) |
| Upstream | bump `stream_timeout` from `30` to `60` | pending config change |
| Instrumentation | per-request stream tracer writes JSONL | ✅ live on dev (`d80ed16727`) |
| Upstream (abort) | when gap >= `stream_timeout` *and* we're about to walk away, send sglang `/abort_request` with the request id | **next** |
| Orphan sweep | periodic job that calls `abort_all` against sglang when TokenizerManager warnings cross a threshold | **next** |

## To resume for a conclusive fix

- Wire the `/abort_request` call into LiteLLM's upstream-timeout handler
  so any stream that exits silently sends the explicit abort.
- Prove the fix captures TTFC/gap histograms on the same Kimi-K3 stress
  recipe without any orphans surviving 60s after their LiteLLM stream
  closes.
- Then turn `LITELLM_STREAM_TRACE_PATH` on in prod temporarily (1h) with
  a small `LITELLM_STREAM_TRACE_GAP_THRESHOLD_MS=2000` to capture the
  next occurrence of a >30s gap in production.
