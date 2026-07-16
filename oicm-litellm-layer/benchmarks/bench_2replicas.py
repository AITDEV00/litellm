"""
Benchmark for 2-replica litellm proxy.
Tests through the service IP so traffic load-balances across both pods.
Also tests each pod individually for comparison.
"""
import asyncio
import httpx
import time
import statistics
import json

MODEL = "Qwen/Qwen3-Next-80B-A3B-Instruct"
DIRECT_URL = "http://s-a500a62d-ddda-45cc-87d9-f0b53e5d62af.adeo.svc.cluster.local:8080/v1/chat/completions"
# Service IP - load balances across both replicas
LITELLM_SVC_URL = "http://litellm-proxy.mlops.svc.cluster.local:4000/v1/chat/completions"
# Single pod URLs (for comparison)
LITELLM_POD1_URL = "http://10.42.1.161:4000/v1/chat/completions"
LITELLM_POD2_URL = "http://10.42.2.94:4000/v1/chat/completions"
# Localhost (single pod, no service overhead)
LITELLM_LOCAL_URL = "http://localhost:4000/v1/chat/completions"

LITELLM_HEADERS = {"Content-Type": "application/json", "Authorization": "Bearer sk-1234"}
DIRECT_HEADERS = {"Content-Type": "application/json"}

PAYLOAD = {
    "model": MODEL,
    "messages": [{"role": "user", "content": "Say hello in one word."}],
    "max_tokens": 5,
    "temperature": 0.1,
    "stream": False,
}

LIMITS = httpx.Limits(max_connections=1000, max_keepalive_connections=200, keepalive_expiry=120)


async def run_benchmark(url, headers, label, concurrency, total, client):
    sem = asyncio.Semaphore(concurrency)
    times = []
    errors = 0
    err_details = []

    async def single_request():
        nonlocal errors
        async with sem:
            start = time.perf_counter()
            try:
                resp = await client.post(url, headers=headers, json=PAYLOAD, timeout=120)
                resp.raise_for_status()
                resp.json()
                elapsed = (time.perf_counter() - start) * 1000
                times.append(elapsed)
            except Exception as e:
                errors += 1
                if len(err_details) < 3:
                    err_details.append(str(e)[:100])

    tasks = [asyncio.create_task(single_request()) for _ in range(total)]
    wall_start = time.perf_counter()
    await asyncio.gather(*tasks)
    wall = time.perf_counter() - wall_start

    if times:
        ms = sorted(times)
        p50 = statistics.median(ms)
        p95 = ms[int(len(ms) * 0.95)] if len(ms) >= 20 else max(ms)
        rps = len(times) / wall
        print(f"{label} (c={concurrency}, n={len(times)}): wall={wall:.2f}s p50={p50:.1f}ms p95={p95:.1f}ms max={max(ms):.1f}ms rps={rps:.1f} errors={errors}")
        return {
            "label": label, "concurrency": concurrency, "n": len(times),
            "wall": round(wall, 2), "p50": round(p50, 1), "p95": round(p95, 1),
            "max": round(max(ms), 1), "rps": round(rps, 1), "errors": errors,
        }
    else:
        print(f"{label} (c={concurrency}): ALL FAILED errors={errors} {err_details[:2]}")
        return {"label": label, "concurrency": concurrency, "n": 0, "wall": round(wall, 2), "errors": errors}


async def main():
    results = []

    async with httpx.AsyncClient(limits=LIMITS) as client:
        print("=== Warmup ===")
        for _ in range(3):
            try:
                await client.post(DIRECT_URL, headers=DIRECT_HEADERS, json=PAYLOAD, timeout=60)
            except:
                pass
            try:
                await client.post(LITELLM_SVC_URL, headers=LITELLM_HEADERS, json=PAYLOAD, timeout=60)
            except:
                pass
        print("Warmup done\n")

        for c, n in [(1, 20), (50, 200), (100, 300), (200, 400), (500, 500), (1000, 500)]:
            print(f"=== c={c}, n={n} ===")
            d = await run_benchmark(DIRECT_URL, DIRECT_HEADERS, "Direct   ", c, n, client)
            # Test through service (load-balanced across 2 replicas)
            s = await run_benchmark(LITELLM_SVC_URL, LITELLM_HEADERS, "Svc(2rep)", c, n, client)
            if d["n"] and s["n"]:
                ov_wall = s["wall"] - d["wall"]
                ov_p50 = s["p50"] - d["p50"]
                print(f"  Overhead: wall={ov_wall:+.2f}s ({ov_wall/d['wall']*100:+.1f}%) p50={ov_p50:+.1f}ms ({ov_p50/d['p50']*100:+.1f}%)")
            results.extend([d, s])
            print()

    print("=== JSON RESULTS ===")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
