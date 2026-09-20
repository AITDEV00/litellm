# ADEOGPT Model Performance

> Live 7-day metrics for the 5 ADEOGPT chat models on the LiteLLM gateway (qwen 3.6, qwen 122b, glm 5.3, glm 5.2, deepseek v4). Generated 2026-09-10. `Qwen/Qwen3-Next-80B-A3B-Instruct` excluded.

## 1. Model performance

### 1.1 Throughput & latency (7d)

| Model | Requests | Avg RPM | Comp tokens | Peak conc |
|---|---:|---:|---:|---:|
| qwen 3.6 (`Qwen3.6-35B-A3B-FP8`) | 819,876 | 81.3 | 219.5M | 134 |
| qwen 122b (`Qwen3.5-122B-A10B-GPTQ-Int4`) | 297,223 | 29.5 | 223.5M | 49 |
| glm 5.3 (`zai-org/GLM-5.3-Flash`) | 173,482 | 17.2 | 166.2M | 74 |
| deepseek v4 (`DeepSeek-V4-Flash-0731`) | 93,525 | 9.3 | 72.6M | 79 |
| glm 5.2 (`zai-org/GLM-5.2-FP8`) | 81,044 | 8.0 | 59.1M | 25 |

### 1.2 Last 24 hours

| Model | Requests | Avg RPM | Peak conc |
|---|---:|---:|---:|
| qwen 3.6 | 93,351 | 64.8 | 27 |
| qwen 122b | 30,566 | 21.2 | 25 |
| deepseek v4 | 25,792 | 17.9 | 27 |
| glm 5.3 | 23,732 | 16.5 | 42 |
| glm 5.2 | 9,605 | 6.7 | 20 |

### 1.3 Sequence-length percentiles (7d)

| Model | in p50 | in p90 | in p99 | out p50 | out p90 | out p99 |
|---|---:|---:|---:|---:|---:|---:|
| qwen 3.6 | 1,379 | 4,039 | 16,023 | **2** | 690 | 2,048 |
| qwen 122b | 2,507 | 4,001 | 9,830 | 506 | 1,546 | 4,548 |
| deepseek v4 | 6,681 | 136,547 | 218,443 | 149 | 2,238 | 6,808 |
| glm 5.2 | 57,090 | 144,538 | 184,736 | 262 | 2,120 | 5,784 |
| glm 5.3 | 36,693 | 195,623 | 476,902 | 200 | 1,841 | 14,600 |

### 1.4 Daily peak concurrency

| Model | 09-03 | 09-04 | 09-05 | 09-06 | 09-07 | 09-08 | 09-09 | 09-10 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| qwen 3.6 | 41 | 35 | 53 | 48 | 43 | **147** | 17 | 10 |
| qwen 122b | 28 | 41 | 30 | 38 | 37 | 39 | 14 | 13 |
| glm 5.3 | 12 | 53 | 43 | 28 | 35 | 46 | **70** | 45 |
| deepseek v4 | 17 | **79** | 9 | 20 | 14 | 10 | 27 | 13 |
| glm 5.2 | 10 | 26 | 12 | 19 | 18 | 20 | 18 | 22 |

## 2. Spending (7d, by ADEOGPT team keys)

Only keys belonging to the ADEOGPT teams (ADEOGPT-DEV, ADEOGPT-PRD, ADEOGPT-STG) are shown. Rows: `(requests / spend)`.

| Model | Total req | Total spend | Top ADEOGPT keys |
|---|---|---:|---|
| glm 5.3 | 119,383 | **$1,210.63** | ADEOGPT-STG ($930), ADEOGPT-59 ($105), ADEOGPT-DEV ($90) |
| glm 5.2 | 8,446 | **$724.66** | ADEOGPT-58 ($360), ADEOGPT-STG ($308), ADEOGPT-59 ($52) |
| qwen 3.6 | 11,332 | $26.44 | ADEOGPT-DEV ($14), ADEOGPT-STG ($10) |
| deepseek v4 | 22,489 | $18.97 | ADEOGPT-STG ($2), ADEOGPT-DEV ($2), ADEOGPT-59 ($15) |
| qwen 122b | 131 | $2.59 | ADEOGPT-58 ($2) |

**Total ADEOGPT-team 7d spend:** $1,984.23, concentrated in the GLM models (glm 5.3 + glm 5.2 = ~98%) via the ADEOGPT-STG, ADEOGPT-58, and ADEOGPT-59 keys.

## 3. How model access is controlled

Each ADEOGPT model is only reachable through keys that are allowed to call it. Access and limits are set on three levels, in order:

1. **Who can call it (model scoping).** A key can use a model if the model's name appears in the key's own allowed list, its **team's** allowed list, or an **access group** it belongs to. If none of the three lists include it, the key gets a 403. So to grant someone a model, add it to the key, its team, or an access group.

2. **How much they can send (rate limits).** Each key and team has its own requests-per-minute (RPM) and tokens-per-minute (TPM) caps. Each model deployment also has its own RPM cap. A request is blocked if it would exceed any of these.

3. **How it's prioritized and budgeted.** Keys/teams carry a priority (prior1/2/3) that reserves a share of each model's capacity under load, and a spend budget that blocks requests once exceeded. These are set on the key or team and enforced on every request.

This is configured and managed through LiteLLM's admin endpoints (keys, teams, organizations, access groups) and stored in the LiteLLM database. The result is that the five ADEOGPT models are served only to the teams/keys that have been explicitly granted them, within their agreed limits.

## 4. Current state

- **All 5 models live and serving**; gateway idle at snapshot (instant concurrency 0).
- **Nothing nears its RPM cap** — traffic is bursty; caps and priority limiter not stressed.
- **Most efficient:** deepseek v4 (highest throughput, lowest cost).
- **Most at risk:** glm 5.2 (poor latency + highest cost) and 13–15% error rates on qwen 3.6 / deepseek v4.
- **Cost concentration:** ADEOGPT-team spend (~$1,983/7d) is ~98% in the GLM models from ~60K-token prompts; glm 5.2 cache-hit only 5.4% (top caching opportunity).
