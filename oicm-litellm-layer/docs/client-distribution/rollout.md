# Rollout Plan

## Phase 0 — proof (half a day)

Goal: prove the mirror chain works on a clean network-isolated environment
before any cluster changes.

- Run the sync job on the workstation; produce the store locally.
- Serve it on `localhost:8000` (python http.server or nginx).
- Run the modified `/install` with the gateway URL pointed at localhost,
  inside a fresh container (`docker run -it --network none` after seeding
  the store, or a network namespace limited to localhost) — the point is that
  with PyPI/GitHub/astral.sh unroutable, the install still completes.
- Success: `lite --version` prints, `lite config set base_url` persists, and
  a `--no-index` resolution log shows zero external requests (verify with a
  proxy that fails closed, e.g. `HTTPS_PROXY=http://127.0.0.1:1`).

Phase 0 tests the script template + mirror content. In v4 that means the
generator (unit tested: render with a known manifest, assert the origin
overrides) and the bucket tree served locally. The two only meet in Phase 1.

## Phase 1 — pilot (1 week)

- Deploy RustFS + buckets, add the gateway env vars, and roll out the image
  that includes the slice wiring (per [deployment.md](deployment.md)); the
  admin then syncs artifacts via the API.
- 2-3 pilot users on Linux x86-64; each does the full journey:
  `/install` -> `lite login` -> `lite configure claude` (or `lite opencode`)
  -> a day of real coding-agent use.
- Watch: install success rate, keyring behavior on their distros, PATH
  refresh quirks (new shells vs current), and whether `/v1/models`-driven
  model lists look right in each harness's picker. Also watch gateway
  metrics during a few parallel installs for any artifact-traffic impact on
  `/v1` latency (expected none; the proxy streams from RustFS).
- Exit criteria: all pilots install without manual intervention and complete
  a real task through their agent; drop-detection tests green in CI.

## Phase 2 — general availability

- Announce with the two-command story (`curl ... /install | sh`, then
  `lite login`), plus the two-step cautious form from
  [security.md](security.md).
- Add a docs page under usage-guides once the flow is stable (deferred until
  then; this folder is the plan of record).

## Metrics

Where they come from: no new instrumentation needed —

- Install/update health: the slice can log each `/install/*` GET (standard
  access logging); artifact download counts come from the same logs. Being
  in-app also means future metrics can join artifact downloads to real proxy
  telemetry if we ever care.
- Usage: existing proxy telemetry (requests per virtual key, model
  distribution) already shows which keys are being used by CLI-driven agents.
- CLI-side: `lite` itself reports its version in `--version`; a future
  `lite doctor`-style health check is an upstream feature, not ours.

## Rollback

- Comment the wiring line (or drop the slice from the image) -> `/install/*`
  404s; nothing else in the gateway is affected. The drop-detection tests
  intentionally fail in this state, which is the designed tripwire.
- Installed clients keep working: `lite` talks only to `/v1`; a client whose
  mirror disappears is fully functional, it just cannot update.
- Store rollback: republish the previous manifest (the race-guard retention
  keeps it briefly); the next `/install` run pins users to the older version.
