# Architecture

## Host model

One public hostname, `litellm.ecouncil.ae`, one port (443). Three backends
behind the existing nginx ingress, routed by path prefix:

```
                        litellm.ecouncil.ae:443
                                 │
                          nginx ingress
                                 │
         ┌───────────────────────┼────────────────────────┐
         │                       │                        │
         ▼                       ▼                        ▼
     /install, /install.ps1  /mirror/*              /v1/*, /ui, ...
     (ConfigMap-served        (artifact pod:         (litellm-proxy
      small shell scripts)    static files)           service, unchanged)
```

This is deliberately not served from the LiteLLM FastAPI app. Binary artifacts
have nothing to do with the API server process, and keeping them off the app
means zero code changes to the proxy, no new attack surface on the request path,
and no risk to the `/v1` routes from artifact traffic spikes. The firewall team
sees one hostname and one port either way.

### Path map

| Path | Backend | Content |
|------|---------|---------|
| `/install` | ingress-nginx `configuration-snippet` -> static | POSIX bootstrap script |
| `/install.ps1` | same | Windows PowerShell bootstrap |
| `/mirror/uv/<version>/...` | artifact pod | uv release tarballs/zips + checksums |
| `/mirror/pypi/...` | artifact pod | flat wheelhouse of `litellm[cli]` + full dependency closure |
| `/mirror/python/...` | artifact pod | python-build-standalone archives `uv` would fetch |
| `/mirror/manifest.json` | artifact pod | versions + sha256 of everything current |
| `/v1/*`, `/ui/*`, `/health/*`, `/.well-known/*` | litellm-proxy svc | unchanged |

Path prefix choice: `/mirror` is ugly but collision-proof. The FastAPI route
collision check found no `/install` or `/client` routes today, but `/mirror`
cannot ever collide with a future upstream route, and it groups cleanly in
ingress rules and network policies. Revisit in [open-questions.md](open-questions.md)
if we want a nicer public name.

## The full first-run flow

```
curl -fsSL https://litellm.ecouncil.ae/install | sh
        │
        ▼
  [install script]                    (runs on the dev machine)
        │  uname -s, uname -m, libc detect
        │  GET /mirror/manifest.json
        ▼
  install/upgrade uv  ──── GET /mirror/uv/<pinned>/uv-<target>.tar.gz
        │                (curl, checksum-verified, to ~/.local/bin)
        ▼
  uv tool install 'litellm[cli]'
        │   UV_INDEX_URL            = https://litellm.ecouncil.ae/mirror/pypi
        │   --no-index --find-links ... (see installer-script.md for exact form)
        │   UV_PYTHON_INSTALL_MIRROR = https://litellm.ecouncil.ae/mirror/python
        │   (uv downloads CPython from the mirror if no system Python fits)
        ▼
  lite -> ~/.local/bin/lite
        │
        ▼
  lite config set base_url https://litellm.ecouncil.ae
        │
        ▼
  lite login (or --api-key / LITELLM_PROXY_API_KEY)
        │
        ▼
  lite configure / lite claude / lite codex / lite opencode / lite pi
        │   each fetches GET /v1/models with the user's key
        │   and writes the harness config that mirrors that key's access
        ▼
  done
```

Key property: after the initial `curl`, every request is a GET on
`litellm.ecouncil.ae`. The per-user model-rights adaptation happens in the
stock CLI (OpenCode's `OPENCODE_CONFIG_CONTENT`, Claude Code's gateway model
discovery, pi's `models.json` sync), not in the served script. We distribute
the distributor's tooling unchanged; the personalization boundary stays
client-side.

## Why this shape (decisions already settled)

- **Mirror uv as a binary, not `lite` as a binary.** `uv` is the only
  standalone-native piece of upstream's chain, and freezing `litellm[cli]`
  (PyInstaller/Nuitka, keyring backends, glibc, ARM, signing) creates a
  distribution format upstream does not test. This was weighed in the earlier
  research and settled: mirrored `uv` + wheelhouse first; frozen `lite` binary
  only as a possible later optimization if the chain proves too heavy.
- **Same-origin only.** No CDN, no GitHub fallback. The failure mode of a
  half-mirrored chain is exactly the situation we are designing away.
- **The artifact store is a separate pod, not the proxy.** Sizing, restarts,
  and network policy for static files are independent of the API server.

## What stays untouched upstream

- `litellm[cli]` package: byte-identical wheels from PyPI (mirrored).
- `uv`: byte-identical release artifacts from the pinned GitHub release
  (mirrored), installed by upstream's own installer logic.
- The proxy's config, auth, model list, routing: unchanged. The artifact pod
  does not touch Postgres/Redis.
