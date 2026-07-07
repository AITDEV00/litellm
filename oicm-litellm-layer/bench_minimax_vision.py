"""
Benchmark: MiniMax M3 vision model latency overhead.
Tests small (256x256) and large (2000x2000) images at various concurrency levels.
Compares direct vLLM ClusterIP vs litellm proxy (2 replicas via service).
"""
import asyncio
import httpx
import time
import statistics
import json
import base64
import sys

MODEL = "MiniMaxAI/MiniMax-M3-MXFP8"
DIRECT_URL = "http://s-908d3952-1e69-40a4-95b9-db1abff27fcb.adeo.svc.cluster.local:8080/v1/chat/completions"
LITELLM_URL = "http://litellm-proxy.mlops.svc.cluster.local:4000/v1/chat/completions"
LITELLM_HEADERS = {"Content-Type": "application/json", "Authorization": "Bearer sk-1234"}
DIRECT_HEADERS = {"Content-Type": "application/json"}

SMALL_IMG_PATH = "/tmp/apple_256x256.jpg"
LARGE_IMG_PATH = "/tmp/apple_2000x2000.jpg"

LIMITS = httpx.Limits(max_connections=1000, max_keepalive_connections=200, keepalive_expiry=120)


def load_image_b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def build_payload(img_b64):
    return {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                    },
                    {
                        "type": "text",
                        "text": "Tell me what is the image.",
                    },
                ],
            }
        ],
        "max_tokens": 10,
        "temperature": 0.1,
        "stream": False,
        "extra_body": {"chat_template_kwargs": {"thinking_mode": "disabled"}},
    }


async def run_benchmark(url, headers, label, payload, concurrency, total, client):
    sem = asyncio.Semaphore(concurrency)
    times = []
    errors = 0
    err_details = []

    async def single_request():
        nonlocal errors
        async with sem:
            start = time.perf_counter()
            try:
                resp = await client.post(url, headers=headers, json=payload, timeout=120)
                resp.raise_for_status()
                resp.json()
                elapsed = (time.perf_counter() - start) * 1000
                times.append(elapsed)
            except Exception as e:
                errors += 1
                if len(err_details) < 3:
                    err_details.append(str(e)[:150])

    tasks = [asyncio.create_task(single_request()) for _ in range(total)]
    wall_start = time.perf_counter()
    await asyncio.gather(*tasks)
    wall = time.perf_counter() - wall_start

    if times:
        ms = sorted(times)
        p50 = statistics.median(ms)
        p95 = ms[int(len(ms) * 0.95)] if len(ms) >= 20 else max(ms)
        rps = len(times) / wall
        print(f"  {label:12s} (c={concurrency:3d}, n={len(times):3d}): wall={wall:6.2f}s p50={p50:8.1f}ms p95={p95:8.1f}ms max={max(ms):8.1f}ms rps={rps:5.1f} errors={errors}")
        if err_details:
            for d in err_details:
                print(f"               ERR: {d}")
        return {
            "label": label, "concurrency": concurrency, "n": len(times),
            "wall": round(wall, 2), "p50": round(p50, 1), "p95": round(p95, 1),
            "max": round(max(ms), 1), "rps": round(rps, 1), "errors": errors,
        }
    else:
        print(f"  {label:12s} (c={concurrency:3d}): ALL FAILED errors={errors} {err_details[:2]}")
        return {"label": label, "concurrency": concurrency, "n": 0, "wall": round(wall, 2), "errors": errors}


async def main():
    results = []

    small_b64 = load_image_b64(SMALL_IMG_PATH)
    large_b64 = load_image_b64(LARGE_IMG_PATH)
    print(f"Small image: {len(SMALL_IMG_PATH) and os.path.getsize(SMALL_IMG_PATH)} bytes, b64 len: {len(small_b64)}")
    print(f"Large image: {os.path.getsize(LARGE_IMG_PATH)} bytes, b64 len: {len(large_b64)}")
    print()

    small_payload = build_payload(small_b64)
    large_payload = build_payload(large_b64)

    async with httpx.AsyncClient(limits=LIMITS) as client:
        for img_label, payload in [("256x256", small_payload), ("2000x2000", large_payload)]:
            print(f"{'='*60}")
            print(f"IMAGE: {img_label}")
            print(f"{'='*60}")

            # Warmup both paths
            print("  Warmup...")
            for _ in range(2):
                try:
                    await client.post(DIRECT_URL, headers=DIRECT_HEADERS, json=payload, timeout=60)
                except:
                    pass
                try:
                    await client.post(LITELLM_URL, headers=LITELLM_HEADERS, json=payload, timeout=60)
                except:
                    pass
            print("  Warmup done")
            print()

            for c, n in [(1, 20), (50, 100), (100, 200), (200, 200)]:
                print(f"  --- c={c}, n={n} ---")
                d = await run_benchmark(DIRECT_URL, DIRECT_HEADERS, "Direct", payload, c, n, client)
                l = await run_benchmark(LITELLM_URL, LITELLM_HEADERS, "Svc(2rep)", payload, c, n, client)
                if d["n"] and l["n"]:
                    ov_wall = l["wall"] - d["wall"]
                    ov_p50 = l["p50"] - d["p50"]
                    print(f"    Overhead: wall={ov_wall:+.2f}s ({ov_wall/d['wall']*100:+.1f}%) p50={ov_p50:+.1f}ms ({ov_p50/d['p50']*100:+.1f}%)")
                    d["image"] = img_label
                    l["image"] = img_label
                    results.extend([d, l])
                print()

    print(f"{'='*60}")
    print("=== JSON RESULTS ===")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    import os
    asyncio.run(main())
