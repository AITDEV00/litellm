#!/usr/bin/env python3
"""Stress test for the gateway /v1/diarize route (pyannote community-1 slice).

Targets a LiteLLM gateway (dev by default: http://localhost:14000). Phases:
  1. gate        - /health/liveliness + /v1/models shows pyannote/community-1
  2. contract    - per-field 400 passthrough + auth enforcement + model-omission
                   defaulting (correctness proof, not perf)
  3. single      - 1 warm diarize request (baseline latency, speaker sanity)
  4. concurrency - N parallel clients x R rounds (60s clips) -> agg RTFx,
                   p50/p95/p99 latency, error rate, schema validation of every
                   response
  5. long        - one ~17.5min AMI meeting clip, stability check

Stdlib only (urllib + manual multipart), no external deps. Mirrors coro's
tests/local/stress_transcriptions.py conventions.

Env:
  GATEWAY_BASE   default http://localhost:14000
  GATEWAY_KEY    master/dev virtual key (required)
  DIARIZE_AUDIO  default /tmp/smoke-60s.wav (60s, 16k mono)
  DIARIZE_LONG   default ~/DGE/speakrs/test-audio/ES2004a.Mix-Headset.wav
  DIARIZE_CONCURRENCY  default 8
  DIARIZE_ROUNDS       default 5
"""

import json
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import wave
from concurrent.futures import ThreadPoolExecutor

BASE = os.environ.get("GATEWAY_BASE", "http://localhost:14000")
KEY = os.environ.get("GATEWAY_KEY", "")
AUDIO = os.environ.get("DIARIZE_AUDIO", "/tmp/smoke-60s.wav")
LONG = os.path.expanduser(os.environ.get("DIARIZE_LONG", "~/DGE/speakrs/test-audio/ES2004a.Mix-Headset.wav"))
CONCURRENCY = int(os.environ.get("DIARIZE_CONCURRENCY", "8"))
ROUNDS = int(os.environ.get("DIARIZE_ROUNDS", "5"))
MODEL = os.environ.get("DIARIZE_MODEL", "pyannote/community-1")

results = []  # (ok, elapsed, audio_dur, n_speakers, n_segments)
results_lock = threading.Lock()
failures = []
ssl_ctx = ssl.create_default_context() if BASE.startswith("https") else None


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def audio_duration(path):
    with wave.open(path) as w:
        return w.getnframes() / w.getframerate()


def multipart(file_bytes, fields):
    boundary = "----diarizeStress7d9f2a"
    body = b""
    for name, value in fields.items():
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode()
    body += (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="clip.wav"\r\n'
            f"Content-Type: audio/wav\r\n\r\n"
        ).encode()
        + file_bytes
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return body, f"multipart/form-data; boundary={boundary}"


def post_diarize(path, *, key=KEY, model=MODEL, fields=None, timeout=900.0):
    body, ctype = multipart(_read(path), {"model": model, **(fields or {})})
    req = urllib.request.Request(
        f"{BASE}/v1/diarize",
        data=body,
        headers={"Content-Type": ctype, "Authorization": f"Bearer {key}"},
        method="POST",
    )
    started = time.perf_counter()
    try:
        response = None
        if ssl_ctx:
            response = urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx)
        else:
            response = urllib.request.urlopen(req, timeout=timeout)
        data = json.loads(response.read())
        elapsed = time.perf_counter() - started
        out = data.get("output", {})
        segs = out.get("diarization", [])
        speakers = sorted({s.get("speaker") for s in segs})
        with results_lock:
            results.append((True, elapsed, audio_duration(path), speakers, len(segs)))
        return data
    except urllib.error.HTTPError as e:
        elapsed = time.perf_counter() - started
        with results_lock:
            results.append((False, elapsed, 0.0, [], 0))
        raise RuntimeError(f"HTTP {e.code}: {e.read()[:300]!r}") from e


def expect(cond, label):
    if cond:
        print(f"  PASS {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


def check_job_shape(data, label):
    expect(set(data.keys()) >= {"jobId", "status", "createdAt", "output"}, f"{label}: DiarizationJob keys")
    expect(data.get("status") == "succeeded", f"{label}: status=succeeded")
    out = data.get("output", {})
    expect("diarization" in out, f"{label}: output.diarization present")
    segs = out.get("diarization", [])
    expect(len(segs) > 0, f"{label}: segments non-empty")
    ok_seg = all({"speaker", "start", "end"} <= set(s.keys()) for s in segs[:5])
    expect(ok_seg, f"{label}: segment keys speaker/start/end")
    ok_mono = all(
        isinstance(s.get("speaker"), str)
        and isinstance(s.get("start"), (int, float))
        and isinstance(s.get("end"), (int, float))
        and s["end"] > s["start"]
        for s in segs[:5]
    )
    expect(ok_mono, f"{label}: segment types valid, end>start")


def pct(sorted_vals, p):
    if not sorted_vals:
        return float("nan")
    idx = min(len(sorted_vals) - 1, round(p / 100 * (len(sorted_vals) - 1)))
    return sorted_vals[idx]


def phase_gate():
    print("[gate] health + registration")
    with urllib.request.urlopen(f"{BASE}/health/liveliness", timeout=10) as r:
        expect(r.status == 200, "liveliness 200")
    req = urllib.request.Request(f"{BASE}/v1/models", headers={"Authorization": f"Bearer {KEY}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        models = json.loads(r.read())
    ids = [m["id"] for m in models.get("data", [])]
    expect(MODEL in ids, f"/v1/models lists {MODEL}")
    expect("exclusiveDiarization" not in json.dumps(models), "no field leakage in models list")


def phase_contract(AUDIO):
    print("[contract] error contracts")
    # 1. missing auth -> 401
    try:
        body, ctype = multipart(_read(AUDIO), {"model": MODEL})
        urllib.request.urlopen(
            urllib.request.Request(
                f"{BASE}/v1/diarize",
                data=body,
                headers={"Content-Type": ctype},
            ),
            timeout=30,
        )
        expect(False, "no-auth rejected")
    except urllib.error.HTTPError as e:
        expect(e.code == 401, f"no-auth rejected (HTTP {e.code})")
    # 2. well-typed but unsupported spec field -> pod's semantic 400 (spec
    #    ValidationErrorResponse fragment reaches the client)
    try:
        post_diarize(AUDIO, fields={"numSpeakers": "2"})
        expect(False, "unsupported numSpeakers rejected")
    except RuntimeError as e:
        expect("400" in str(e) and "numSpeakers" in str(e), f"numSpeakers=2 -> 400 spec shape ({str(e)[:60]})")
    # 3. wrong-typed value -> FastAPI's own 422 int_parsing, also passed through
    try:
        post_diarize(AUDIO, fields={"numSpeakers": "not-a-number"})
        expect(False, "mis-typed numSpeakers rejected")
    except RuntimeError as e:
        expect(
            "422" in str(e) and "int_parsing" in str(e),
            f"numSpeakers=not-a-number -> 422 type validation ({str(e)[:60]})",
        )
    # 4. model omitted -> defaults to deployment model, must succeed
    req_fields = {}
    body, ctype = multipart(_read(AUDIO), req_fields)
    req = urllib.request.Request(
        f"{BASE}/v1/diarize",
        data=body,
        headers={"Content-Type": ctype, "Authorization": f"Bearer {KEY}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            data = json.loads(r.read())
        check_job_shape(data, "model-omitted")
        expect(data.get("output") is not None, "model-omitted default resolves to diarization deployment")
    except urllib.error.HTTPError as e:
        expect(False, f"model-omitted default works (HTTP {e.code})")


def phase_single(AUDIO):
    print("[single] warm baseline")
    dur = audio_duration(AUDIO)
    print(f"  audio: {AUDIO} {dur:.0f}s")
    data = post_diarize(AUDIO, fields={"exclusive": "true"})
    check_job_shape(data, "single")
    ok, elapsed, _, speakers, segs = results[-1]
    expect(len(speakers) >= 1, f"single: speakers found {speakers}")
    print(f"  {elapsed:.2f}s wall for {dur:.0f}s audio -> {dur / elapsed:.0f}x RTFx, speakers={speakers}")


def phase_concurrency(AUDIO):
    print(f"[concurrency] {CONCURRENCY} clients x {ROUNDS} rounds")
    t0 = time.perf_counter()
    errors = 0
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = [pool.submit(post_diarize, AUDIO) for _ in range(CONCURRENCY * ROUNDS)]
        for fut in futures:
            try:
                fut.result()
            except Exception as e:
                errors += 1
                print(f"  error: {e}", file=sys.stderr)
    wall = time.perf_counter() - t0
    batch = results[-(CONCURRENCY * ROUNDS) :]
    oks = [r for r in batch if r[0]]
    lats = sorted(r[1] for r in oks)
    total_audio = sum(r[2] for r in oks)
    print(f"  done in {wall:.1f}s: {len(oks)} ok, {errors} errors")
    if lats:
        print(f"  agg throughput {total_audio / wall:.0f}x RTFx ({total_audio:.0f}s audio in {wall:.1f}s wall)")
        per_stream = sum(lats) / len(lats)
        print(
            f"  per-request: mean {per_stream:.2f}s p50 {pct(lats, 50):.2f}s p95 {pct(lats, 95):.2f}s p99 {pct(lats, 99):.2f}s max {lats[-1]:.2f}s"
        )
        all_speakers_ok = all(len(r[3]) >= 1 for r in oks)
        expect(errors == 0, f"concurrency: 0 errors ({errors} seen)")
        expect(all_speakers_ok, "concurrency: every response had >=1 speaker")
        expect(len(oks) == CONCURRENCY * ROUNDS, f"concurrency: all {CONCURRENCY * ROUNDS} requests succeeded")
    return wall


def phase_long():
    if not os.path.exists(LONG):
        print(f"[long] skipped, {LONG} not found")
        return
    print("[long] stability on a real AMI meeting clip")
    ldur = audio_duration(LONG)
    print(f"  {os.path.basename(LONG)} duration={ldur:.0f}s")
    data = post_diarize(LONG, timeout=1200)
    check_job_shape(data, "long")
    ok, elapsed, adur, speakers, segs = results[-1]
    print(
        f"  {elapsed:.1f}s wall, {adur:.0f}s audio -> {adur / elapsed:.0f}x RTFx, speakers={len(speakers)} segments={segs}"
    )
    expect(len(speakers) >= 2, f"long: multi-speaker meeting detected ({len(speakers)} speakers)")


def main():
    if not KEY:
        print("GATEWAY_KEY is required", file=sys.stderr)
        sys.exit(2)
    print(f"target={BASE} model={MODEL} audio={AUDIO} concurrency={CONCURRENCY} rounds={ROUNDS}")
    phase_gate()
    phase_contract(AUDIO)
    phase_single(AUDIO)
    phase_concurrency(AUDIO)
    phase_long()
    print(f"[summary] {'STRESS TEST PASSED' if not failures else 'FAILURES: ' + ', '.join(failures)}")
    sys.exit(0 if not failures else 1)


if __name__ == "__main__":
    main()
