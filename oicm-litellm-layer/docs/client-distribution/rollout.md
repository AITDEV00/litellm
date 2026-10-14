# Rollout Plan

## Phase 0 — proof (half a day)

Goal: prove the mirror chain works on a clean network-isolated environment
before any cluster changes.

- Run the sync job on the workstation; produce the store locally (cc-switch
  archives + checksums + manifest + provider preset).
- Serve it on `localhost:8000` (python http.server or nginx).
- Run the modified `/install` with the gateway URL pointed at localhost,
  inside a fresh container (`docker run -it --network none` after seeding
  the store, or a network namespace limited to localhost) — the point is that
  with GitHub/PyPI unroutable, the install still completes.
- Success: `cc-switch --version` prints, the provider registers, `cc-switch
  use <id> --app open-code` writes `~/.config/opencode/opencode.json`, and a
  proxy that fails closed (`HTTPS_PROXY=http://127.0.0.1:1`) shows zero
  external requests.

Phase 0 tests the script template + mirror content. In v4 that means the
generator (unit tested: render with a known manifest, assert the origin
overrides) and the bucket tree served locally. The two only meet in Phase 1.

## Phase 1 — pilot (1 week)

- Deploy RustFS + buckets, add the gateway env vars, and roll out the image
  that includes the slice wiring (per [deployment.md](deployment.md)); the
  admin then syncs artifacts via the API.
- 2-3 pilot users on Linux x86-64; each does the full journey:
  `/install` -> paste gateway key -> pick harness(es) -> a day of real
  coding-agent use through their configured harness.
- Watch: install success rate, the `/v1/models` key diagnostic (clear error
  on a wrong key), the harness multi-select UX, and whether the written
  harness config files (opencode.json, settings.json, config.toml) look right
  and are `0600`. Also watch gateway metrics during a few parallel installs
  for any artifact-traffic impact on `/v1` latency (expected none; the proxy
  streams from RustFS).
- **Resync check**: add/remove a model on the gateway, re-run `/install` on a
  pilot machine, confirm the provider's model list updates and the run is
  otherwise a no-op.
- Exit criteria: all pilots install without manual intervention and complete
  a real task through their agent; drop-detection tests green in CI.

## Phase 2 — general availability

- Announce with the single-command story (`curl ... /install | sh`, then
  paste your gateway key and pick your harnesses), plus the two-step cautious
  form from [security.md](security.md).
- State the update policy plainly: **`/install` is the authoritative way to
  update cc-switch and re-sync models; do not run `cc-switch update`** (it
  points at public GitHub, which the firewall blocks anyway).
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
