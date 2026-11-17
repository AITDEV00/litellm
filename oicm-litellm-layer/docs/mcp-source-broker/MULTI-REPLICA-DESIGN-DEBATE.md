# Multi-replica broker scaling — debate record (UNFINALIZED)

Status: OPEN debate, deliberately not finalized. Recorded 2026-11-17 against branch
`jya0-v1.108.5`. One-pod `local` mode is accepted as good enough for current dev testing.
Revisit this document when scaling the broker beyond one replica becomes a real requirement.

Trigger: the director challenged the session-affinity design with a NAT/shared-IP objection and
proposed a Redis-backed dispatch path. The debate that followed is recorded here so the decision
can be resumed with full context, not restarted from scratch.

## The correction that killed pure affinity

The original design leaned on connection/ClientIP affinity. Both load-bearing claims were wrong
and are recorded so they are not re-proposed:

- `sessionAffinity: ClientIP` does not apply: ingress-nginx does not route through the kube
  Service VIP. It reads Endpoints and picks a backend per request via its Lua balancer
  (`balancer_ewma` in the live config), so affinity is per-request, not per-client
- "One HTTP/2 connection lands on one pod" is false through nginx: nginx terminates TLS and the
  balancer can place each of a connector's four route calls (register/hold/dispatch/respond) on
  different pods the moment replicas > 1. Dev never saw this because it runs one replica
- Shared office NAT makes any IP-keyed scheme wrong anyway: many connectors, one source IP

## Debate positions

### Position A: Redis (director's proposal) / Dragonfly (variant)

A shared dispatch queue plus correlation map so any pod can serve any connector call.

What it buys:
- Any pod serves any request; no placement math; no session loss on pod death
- Seamless scale events and rolling updates without connector blips

What it costs:
- The dispatch envelope is up to ~1 MiB (760 KiB payload, base64-inflated). Payloads through
  Redis put other people's request data in a cache tier; 10k concurrent x 64 KiB average is
  ~640 MB, and a burst of large bodies can OOM it. Today bodies live in process memory for
  microseconds
- Every request becomes 2-3 hops and the reply needs its own routing (pub/sub or second queue)
  because the pod holding the waiting caller is not the pod that consumed the response
- The cache becomes a data-plane dependency: tool calls now fail when it fails, unlike today
  where Redis failure only degrades caches and rate limits
- at-least-once delivery needs dedupe and ordering semantics

Middle variant identified: Redis/Dragonfly for SIGNALS ONLY (ownership: worker -> pod, ~100
bytes per session), payloads stay in process memory. This is the favored large-fleet shape if
Position B's costs bite first.

### Position B: no shared store (pinned placement + stateless fan-out)

- Connector traffic: `nginx.ingress.kubernetes.io/upstream-hash-by: "$http_x_source_member"`
  -> nginx consistent-hash (ketama) on the authenticated worker ID header. All four calls of a
  worker land on one pod. NAT is irrelevant (hash is on the worker ID, not the IP)
- Gateway traffic: stateless fan-out to all peer pods (addresses from a headless Service DNS)
  with an in-memory LRU hint cache of member -> owner learned from fan-out results. Stale
  hints self-correct by falling back to fan-out on 503
- Revoke: fanned to all peers by the python control route, with the 2s DB reconcile sweep as
  the universal backstop
- Pod death: sessions die with the pod; connectors reconnect and re-register within their
  backoff window (observed 300ms-2s)

Known limitations and mitigations (accepted as the cost of no shared state):
1. Ingress reloads rehash the ring: moved sessions self-heal via 401 -> reconnect, but config
   churn becomes session blips
2. Fan-out costs ~N-1 wasted 503s per miss; fine at N<=3, ugly at N>10
3. Ketama imbalance at small worker counts; harmless because load follows sessions
4. No session migration: rolling updates bounce sessions (blips, not outages)
5. Hint cache can be stale: fallback fan-out is always correct, so a stale hint costs one miss
6. Revoke latency = fan-out + reconcile (seconds), not a central authority

Postgres-backed placement was rejected by both positions: placement is ephemeral state, and
writing it into the enrollment database couples pod lifecycles to the store the broker polls.

## The extension-seam analysis (what makes either future cheap)

The expensive decisions are already right. Credential validation, generation fencing, catalog
admission, health, drain, and revoke semantics are all DB-backed and pod-independent today; a
second pod already agrees on all of it. The ONLY pod-local state is one pair inside each
session: the `pending` dispatch channel and the `inflight` correlation map
(`oicm-litellm-layer/mcp-remotedialer/reverse.go`). Multi-replica is therefore "move or route
around one channel and one map", not a rewrite.

Three stackable modes, selected by configuration, connector protocol unchanged in all:

| Mode | When | What changes | Stays local |
|------|------|--------------|-------------|
| `local` (today) | One broker pod | Nothing | Everything |
| `pinned` | Few pods, rare scaling | One ingress annotation + fan-out + fanned revoke | Queue, correlation, lifecycle |
| `shared` | Large fleets, any-pod routing | Queue + correlation behind interfaces, onto Redis/Dragonfly streams; register writes a placement record; dispatch IDs must become globally unique (today `UnixNano + per-pod counter`, unique per process only) | Fencing, probe, admission, credentials (all DB-backed) |

The seam: a queue pair `enqueue(item)` / `take(ctx)` and a correlator pair
`await(ctx, id)` / `complete(id, resp)`. Local implementations are the existing channel and
map; shared implementations are a stream per worker and per-request response channels.
Handlers and pool keep their signatures.

Dragonfly compatibility constraints, recorded so the swap stays drop-in:
- Redis-protocol compatible (RESP2/RESP3); standard clients point at it unchanged
- Fully compatible for what we need: streams with consumer groups, pub/sub, hashes, TLS, auth
- Wins: multithreaded (Redis is single-core for writes), flatter tail latency under bursty
  large-envelope traffic
- Rules: only Redis-compatible primitives (no RedisJSON/RediSearch/exotic Lua), and the client
  library is the only thing that knows which engine is deployed
- A shared-mode dispatch store should be its OWN instance, never the shared LiteLLM cache Redis

## What NOT to do before scaling is actually needed (agreed)

Build none of it now. Speculative abstraction is debt; one pod is sufficient for current
testing. The five decisions that keep the future cheap cost nothing today:

1. Never move the session state machine out of the pod: fencing and admission stay in Go
   reading Postgres. Payload routing may move; security judgment does not
2. Keep the dispatch envelope JSON canonical (it already is)
3. Keep the four routes plain HTTP, free of nginx-specific coupling
4. Keep selection on config knobs (the existing `MCP_REMOTEDIALER_TRANSPORT` pattern; a mode
   knob can ride the same pattern)
5. This document: the contract, the sharp edges, the global-ID fix, and the placement record
   are all recorded so the future change is a checklist

## Resume conditions

Reopen this debate when any of:
- Measured per-pod ceiling (load-profile step) is below projected connector count
- Broker HA is demanded (pod maintenance must not blip sessions)
- A customer environment requires >1 broker replica

Sequencing stays: load profile and multi-host failover measurements first; their numbers decide
whether anything beyond `local` is ever needed.
