#!/usr/bin/env python3
"""Bounded, unique-WAV burst for a deployed Nemotron Transcribe endpoint.

No credential is printed, accepted on the command line, or written to the
result file. Set LITELLM_API_KEY (or choose another variable name) for HTTPS;
--no-auth deliberately permits localhost reference comparisons only.
"""

import argparse
import concurrent.futures
import hashlib
import http.client
import io
import json
import os
import resource
import ssl
import struct
import threading
import time
import urllib.parse
import wave
from array import array
from pathlib import Path
from statistics import mean, median


MODEL = "nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B"
BOUNDARY = "nim-stress-part"


def clips(source: Path, count: int):
    with wave.open(str(source), "rb") as wav:
        if (wav.getframerate(), wav.getsampwidth(), wav.getnchannels()) != (16000, 2, 1):
            raise ValueError("Source must be 16 kHz, 16-bit, mono PCM WAV")
        duration = wav.getnframes() / wav.getframerate()
        raw = wav.readframes(wav.getnframes())
    originals = array("h")
    originals.frombytes(raw)
    if struct.pack("=h", 1) != struct.pack("<h", 1):
        originals.byteswap()
    result = []
    for index in range(count):
        offset = (index * 13007) % len(originals)
        gain = 0.70 + (index // 64) * 0.003 + (index % 64) / 2000.0
        samples = array(
            "h",
            [max(-32768, min(32767, round(value * gain)))
             for value in originals[offset:]] + [
                max(-32768, min(32767, round(value * gain)))
                for value in originals[:offset]
            ],
        )
        pcm = samples.tobytes()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(16000)
            out.setnframes(len(samples))
            out.writeframes(pcm)
        result.append(buf.getvalue())
    if len({hashlib.sha256(audio).digest() for audio in result}) != count:
        raise RuntimeError("Generated audio inputs are not unique")
    return duration, result


def percentile(latencies, q):
    return latencies[max(0, min(len(latencies) - 1, int(len(latencies) * q + 0.999999) - 1))]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="https://litellm.ecouncil.ae")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, default=256)
    parser.add_argument("--count", type=int, help="Requests submitted; defaults to concurrency")
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--api-key-env", default="LITELLM_API_KEY")
    parser.add_argument("--no-auth", action="store_true", help="Loopback-only reference endpoint")
    parser.add_argument("--label", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    url = urllib.parse.urlsplit(args.url)
    count = args.count or args.concurrency
    if url.scheme not in ("http", "https") or url.query or url.fragment:
        raise SystemExit("URL must be an HTTP(S) base URL without a query or fragment")
    base_path = url.path.rstrip("/")
    if base_path.endswith("/v1"):
        base_path = base_path[:-3].rstrip("/")
    route_path = base_path + "/v1/audio/transcriptions"
    if not 1 <= count <= 7680 or not 1 <= args.concurrency <= 7680:
        raise SystemExit("Bound generation/memory: count and concurrency must be 1..7680")
    if args.no_auth and url.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("--no-auth is loopback-only; use an environment variable for a remote key")
    key = "" if args.no_auth else os.environ.get(args.api_key_env, "")
    if not args.no_auth and not key:
        raise SystemExit(f"Set the credential in environment variable {args.api_key_env}")
    duration, audio = clips(args.audio, count)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency)
    barrier = threading.Barrier(count)
    shared = {"t0": None, "started_cpu": os.times()}
    ready = threading.Barrier(count + 1)

    def request_one(index):
        ready.wait()
        started = time.perf_counter()
        conn_class = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        extra = {"context": ssl.create_default_context()} if url.scheme == "https" else {}
        conn = conn_class(url.hostname, url.port, timeout=args.timeout, **extra)
        audio_body = audio[index]
        wav_hash = hashlib.sha256(audio_body).hexdigest()
        header = (
            f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{args.model}\r\n'
            f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="response_format"\r\n\r\njson\r\n'
            f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="unique-speech-{index:04d}.wav"\r\nContent-Type: audio/wav\r\n\r\n'
        ).encode()
        body = header + audio_body + f"\r\n--{BOUNDARY}--\r\n".encode()
        headers = {
            "Content-Type": f"multipart/form-data; boundary={BOUNDARY}",
            "Content-Length": str(len(body)),
            "Connection": "close",
        }
        if key:
            headers["Authorization"] = "Bearer " + key
        record = {"index": index, "status": 0, "text": None, "error": None,
                  "input_sha256": wav_hash, "upload_bytes": len(body)}
        try:
            conn.request("POST", route_path, body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            record["status"] = response.status
            record["response_duration_ms"] = response.getheader("x-litellm-response-duration-ms")
            parsed = json.loads(raw)
            if response.status == 200 and isinstance(parsed.get("text"), str) and parsed["text"].strip():
                record["text"] = parsed["text"]
            else:
                excerpt = json.dumps(parsed, ensure_ascii=False)[:800]
                record["error"] = excerpt.replace(key, "[redacted]") if key else excerpt
        except Exception as exc:
            message = type(exc).__name__ + ": " + str(exc)
            record["error"] = (message.replace(key, "[redacted]") if key else message)[:800]
        finally:
            conn.close()
        record["elapsed_s"] = time.perf_counter() - started
        audio[index] = b""  # Release this clip as soon as its upload has completed.
        return record

    try:
        futures = [pool.submit(request_one, index) for index in range(count)]
        shared["t0"] = time.perf_counter()
        ready.wait()
        results = []
        for future in concurrent.futures.as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({"status": 0, "text": None, "elapsed_s": 0,
                                "error": f"Harness: {type(exc).__name__}: {exc}"[:800]})
            if len(results) % 64 == 0 or len(results) == count:
                print(f"{args.label}: {len(results)}/{count} responses; "
                      f"{sum(r['status'] == 200 and bool(r['text']) for r in results)} non-empty transcripts", flush=True)
    finally:
        pool.shutdown()
    wall = time.perf_counter() - shared["t0"]
    latencies = sorted(r["elapsed_s"] for r in results)
    good = [r for r in results if r["status"] == 200 and r["text"]]
    errors = [r for r in results if r not in good]
    end_times = os.times()
    summary = {
        "label": args.label,
        "url_origin": f"{url.scheme}://{url.netloc}",
        "path": route_path,
        "mode": "multipart response_format=json, non-streaming",
        "requests": count,
        "concurrency": args.concurrency,
        "unique_clips": len({r.get("input_sha256") for r in results if r.get("input_sha256")} - {None}),
        "clip_seconds": duration,
        "total_audio_seconds": count * duration,
        "wall_seconds": wall,
        "successes": len(good),
        "nonempty_transcripts": len(good),
        "http_status_counts": {str(status): sum(r["status"] == status for r in results)
                                for status in sorted({r["status"] for r in results})},
        "latency_seconds": {"min": latencies[0], "mean": mean(latencies), "p50": median(latencies),
                            "p95": percentile(latencies, .95), "p99": percentile(latencies, .99),
                            "max": latencies[-1]},
        "RTF_wall_per_audio_second": wall / duration,
        "per_request_RTF": {"p50": median(latencies) / duration,
                             "p95": percentile(latencies, .95) / duration},
        "aggregate_audio_seconds_per_wall_second": duration * len(good) / wall,
        "completed_requests_per_second": len(good) / wall,
        "client": {"total_thread_cpu_seconds": sum(end_times[:2]) - sum(shared["started_cpu"][:2]),
                   "max_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024},
        "source": str(args.audio),
        "audio_variants": "Full-duration rotations plus PCM gain variants; not an independent-speaker or WER corpus",
        "errors": errors[:20],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"summary": summary, "results": results}, indent=2))
    print(json.dumps(summary, indent=2), flush=True)
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
