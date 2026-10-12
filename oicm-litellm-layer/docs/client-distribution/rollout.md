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

## Phase 1 — pilot (1 week)

- Deploy per [deployment.md](deployment.md).
- 2-3 pilot users on Linux x86-64; each does the full journey:
  `/install` -> `lite login` -> `lite configure claude` (or `lite opencode`)
  -> a day of real coding-agent use.
- Watch: install success rate, keyring behavior on their distros, PATH
  refresh quirks (new shells vs current), and whether `/v1/models`-driven
  model lists look right in each harness's picker.
- Exit criteria: all pilots install without manual intervention and complete
  a real task through their agent.

## Phase 2 — general availability

- Announce with the two-command story (`curl ... /install | sh`, then
  `lite login`), plus the two-step cautious form from
  [security.md](security.md).
- Add a docs page under usage-guides once the flow is stable (deferred until
  then; this folder is the plan of record).

## Metrics

Where they come from: no new instrumentation needed —

- Install/update health: artifact-pod access logs (per-file GET counts,
  404s, incomplete downloads via log analysis).
- Usage: existing proxy telemetry (requests per virtual key, model
  distribution) already shows which keys are being used by CLI-driven agents.
- CLI-side: `lite` itself reports its version in `--version`; a future
  `lite doctor`-style health check is an upstream feature, not ours.

## Rollback

- Ingress paths removed -> `/install` 404s; nothing else affected (the proxy
  never depends on the artifact pod).
- Installed clients keep working: `lite` talks only to `/v1`; a client whose
  mirror disappears is fully functional, it just cannot update.
- Store rollback: previous manifest retained on the PVC; swap back and the
  next `/install` run pins users to the older version.
