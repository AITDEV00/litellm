# Smell audit: mcp-source-link (2026-11-18)

Baseline: `go vet` clean, `gofmt` clean, `go test ./...` green (36 tests).
staticcheck installed at /tmp/rd-gopath/bin/staticcheck.

## L1 — automated
| Finding | Location | Verdict |
|---|---|---|
| SA1019 grpc.DialContext deprecated | connector/tunnel.go:187 | FIX |
| SA1019 grpc.WithBlock deprecated | connector/tunnel.go:189 | FIX |
| U1000 unused field `lastRoute` | pool/pool_test.go:23 | FIX (dead test field) |
| ST1000 missing package comment | dispatchpb/*.pb.go | IGNORE (generated) |

## L3 — cross-reference (the real bugs)
1. **Catalog size cap defined twice with different expressions.**
   `tunnel.MaxCatalog = 600000` and `catalog.go:83` uses a bare literal `600000`.
   Two constants for one concept; raising one silently diverges the probe read
   limit from the verification cap. FIX: one `wire.MaxCatalogBody`.

2. **ResponseDeadline expressed two ways.** `wire.ResponseDeadlineSeconds = 90`
   (int, used by reverseh2) and `tunnel.ResponseDeadline = 90*time.Second`
   (duplicate literal, used by tunnel). Comment on the tunnel copy says
   "identical to reverse-h2", i.e. the duplication is known and unenforced.
   FIX: tunnel derives from wire like reverseh2 does.

3. **Probe poll interval duplicated as a raw literal.** tunnel.go:323 and
   reverseh2/handlers.go:193 both `time.After(2*time.Second)`; reverseh2 has
   `ProbeInterval = 15s` nearby that the literal does not use, and the 2s is
   really the poll cadence, separate from the 15s readiness window. FIX: name
   the poll cadence once.

4. **Stale comment.** tunnel.go:43 comment claims the value is "identical to
   reverse-h2", which is a claim no test enforces. After fix 2 it becomes true
   structurally rather than by assertion.

## L2 — semantic
5. **`wire` claims to own limits but only owns some.** Its package doc says it
   owns "the protocol and size limits both transports share", yet the catalog
   cap and response deadline live outside it. FIX: move MaxCatalog in.

6. **`dispatchInFlightCap` doc contradicts behavior.** The comment says it
   bounds in-flight requests; it is 0, i.e. disabled, and the least-loaded
   tie-break still counts inFlight. The comment explains the measurement but
   not that the bound is off. Clarify.

## Not smells (checked, deliberately left)
- `time.After(2*time.Second)` in control.go:60 is the boot retry cadence, a
  different concept from the probe poll. Distinct purpose, stays literal.
- main.go IdleConnTimeout 90s is an HTTP server setting unrelated to the
  dispatch deadline.
- 36 tests pass; no test asserts structure over function.
