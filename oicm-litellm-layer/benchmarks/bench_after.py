import asyncio
import httpx
import time
import statistics
import json
import os
import re
from pathlib import Path


def master_key() -> str:
    env = os.getenv("LITELLM_MASTER_KEY")
    if env:
        return env
    manifest = Path(__file__).resolve().parent.parent / "deploy" / "litellm-proxy.yaml"
    block = next(b for b in re.split(r"^---\s*$", manifest.read_text(), flags=re.MULTILINE) if "name: litellm-master-key" in b)
    scope = block[block.rfind("stringData:"):]
    return re.search(r"^\s*master-key:\s*(\S.*?)\s*$", scope, re.MULTILINE).group(1)


MODEL = "Qwen/Qwen3-Next-80B-A3B-Instruct"
DIRECT_URL = "http://s-a500a62d-ddda-45cc-87d9-f0b53e5d62af.adeo.svc.cluster.local:8080/v1/chat/completions"
LITELLM_URL = "http://localhost:4000/v1/chat/completions"
LITELLM_HEADERS = {"Content-Type": "application/json", "Authorization": f"Bearer {master_key()}"}
DIRECT_HEADERS = {"Content-Type": "application/json"}

PAYLOAD = {
    "model": MODEL,
    "messages": [{"role": "user", "content": "Say hello in one word."}],
    "max_tokens": 5,
    "temperature": 0.1,
    "stream": False,
}


async def run_benchmark(url, headers, label, concurrency, total, client):
    sem = asyncio.Semaphore(concurrency)
    times = []
    errors = 0

    async def single_request():
        async with sem:
            start = time.perf_counter()
            try:
                resp = await client.post(url, headers=headers, json=PAYLOAD, timeout=120)
                resp.raise_for_status()
                resp.json()
                elapsed = (time.perf_counter() - start) * 1000
                times.append(elapsed)
            except Exception as e:
                nonlocal_count[0] += 1
                if len(nonlocal_errs) < 3:
                    nonlocal_errs.append(str(e)[:100])

    nonlocal_count = [0]
    nonlocal_errs = []

    tasks = [asyncio.create_task(single_request()) for _ in range(total)]
    wall_start = time.perf_counter()
    await asyncio.gather(*tasks)
    wall = time.perf_counter() - wall_start

    if times:
        ms = sorted(times)
        p50 = statistics.median(ms)
        p95 = ms[int(len(ms) * 0.95)] if len(ms) >= 20 else max(ms)
        rps = len(times) / wall
        print(f"{label} (c={concurrency}, n={len(times)}): wall={wall:.2f}s p50={p50:.1f}ms p95={p95:.1f}ms max={max(ms):.1f}ms rps={rps:.1f} errors={nonlocal_count[0]}")
        return {
            "label": label, "concurrency": concurrency, "n": len(times),
            "wall": wall, "p50": p50, "p95": p95, "max": max(ms),
            "rps": rps, "errors": nonlocal_count[0],
        }
    else:
        print(f"{label} (c={concurrency}): ALL FAILED errors={nonlocal_count[0]} {nonlocal_errs[:2]}")
        return {"label": label, "concurrency": concurrency, "n": 0, "wall": wall, "errors": nonlocal_count[0]}


async def main():
    results = []

    async with httpx.AsyncClient() as client:
        # Warmup both paths
        print("=== Warmup ===")
        for _ in range(3):
            try:
                await client.post(DIRECT_URL, headers=DIRECT_HEADERS, json=PAYLOAD, timeout=60)
            except: pass
            try:
                await client.post(LITELLM_URL, headers=LITELLM_HEADERS, json=PAYLOAD, timeout=60)
            except: pass
        print("Warmup done\n")

        # c=1, n=20 (sequential latency)
        print("=== c=1, n=20 ===")
        d = await run_benchmark(DIRECT_URL, DIRECT_HEADERS, "Direct  ", 1, 20, client)
        l = await run_benchmark(LITELLM_URL, LITELLM_HEADERS, "LiteLLM ", 1, 20, client)
        if d["n"] and l["n"]:
            print(f"  Overhead: {l['p50']-d['p50']:.1f}ms ({(l['p50']-d['p50'])/d['p50']*100:.1f}%)")
        results.extend([d, l])
        print()

        # c=50, n=200
        print("=== c=50, n=200 ===")
        d = await run_benchmark(DIRECT_URL, DIRECT_HEADERS, "Direct  ", 50, 200, client)
        l = await run_benchmark(LITELLM_URL, LITELLM_HEADERS, "LiteLLM ", 50, 200, client)
        if d["n"] and l["n"]:
            ov = l["wall"] - d["wall"]
            print(f"  Wall overhead: {ov:.2f}s ({ov/d['wall']*100:.1f}%)")
        results.extend([d, l])
        print()

        # c=100, n=300
        print("=== c=100, n=300 ===")
        d = await run_benchmark(DIRECT_URL, DIRECT_HEADERS, "Direct  ", 100, 300, client)
        l = await run_benchmark(LITELLM_URL, LITELLM_HEADERS, "LiteLLM ", 100, 300, client)
        if d["n"] and l["n"]:
            ov = l["wall"] - d["wall"]
            print(f"  Wall overhead: {ov:.2f}s ({ov/d['wall']*100:.1f}%)")
        results.extend([d, l])
        print()

        # c=200, n=400
        print("=== c=200, n=400 ===")
        d = await run_benchmark(DIRECT_URL, DIRECT_HEADERS, "Direct  ", 200, 400, client)
        l = await run_benchmark(LITELLM_URL, LITELLM_HEADERS, "LiteLLM ", 200, 400, client)
        if d["n"] and l["n"]:
            ov = l["wall"] - d["wall"]
            print(f"  Wall overhead: {ov:.2f}s ({ov/d['wall']*100:.1f}%)")
        results.extend([d, l])
        print()

        # c=500, n=500
        print("=== c=500, n=500 ===")
        d = await run_benchmark(DIRECT_URL, DIRECT_HEADERS, "Direct  ", 500, 500, client)
        l = await run_benchmark(LITELLM_URL, LITELLM_HEADERS, "LiteLLM ", 500, 500, client)
        if d["n"] and l["n"]:
            ov = l["wall"] - d["wall"]
            print(f"  Wall overhead: {ov:.2f}s ({ov/d['wall']*100:.1f}%)")
        results.extend([d, l])
        print()

        # c=1000, n=500
        print("=== c=1000, n=500 ===")
        d = await run_benchmark(DIRECT_URL, DIRECT_HEADERS, "Direct  ", 1000, 500, client)
        l = await run_benchmark(LITELLM_URL, LITELLM_HEADERS, "LiteLLM ", 1000, 500, client)
        if d["n"] and l["n"]:
            ov = l["wall"] - d["wall"]
            print(f"  Wall overhead: {ov:.2f}s ({ov/d['wall']*100:.1f}%)")
        results.extend([d, l])

    print("\n=== JSON RESULTS ===")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
