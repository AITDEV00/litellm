# Measurements

## Scope

Empirical results from running the system under load. Every claim here is backed by a run with a date, a command, and an observed outcome.

## Contents

| File | Records |
|---|---|
| [`go-driver-concurrency-2026-11-18.md`](go-driver-concurrency-2026-11-18.md) | Tunnel concurrency batches, the 65536 clean run, the inconclusive 131072 offer, and the h2c/pooling correction |

## Rules for adding to this slice

A measurement file is only useful if a later reader can tell what was proven, what failed, and what remained unmeasured. Each file states its scope, the exact command or harness, and separates proven results from inconclusive ones. Numbers without a run do not belong here.

Name files `<subject>-<date>.md` so the slice sorts by recency and an agent can find the latest result for a subject by name.
