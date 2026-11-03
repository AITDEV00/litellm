# Makefile Reference

Every target in `oicm-litellm-layer/Makefile`, grouped by workflow. Run all
commands from `oicm-litellm-layer/`.

## Key variables (top of Makefile)

| Variable | Value / meaning |
|----------|-----------------|
| `REGISTRY` | `registry.adeoaiengine.ecouncil.ae` (internal Harbor) |
| `REPO_PATH` | `openinnovationai/platform/mlops/mlops-serving` |
| `HARBOR_USER` / `HARBOR_PASS` | Harbor credentials for `make login` |
| `DISCOVERY_IMG` | `$(REGISTRY)/$(REPO_PATH)/oicm-discovery-controller` |
| `LITELLM_HARBOR_IMG` | `$(REGISTRY)/$(REPO_PATH)/litellm` |
| `LITELLM_SRC_HARBOR_IMG` | `$(REGISTRY)/$(REPO_PATH)/litellm-src` |
| `LITELLM_SRC_TAG` | Sanitized current git branch name (slashes → `_`), e.g. `jya0-v1.106.6`. Override: `LITELLM_SRC_TAG=foo` |
| `CONTROLLER_VERSION` | Discovery controller semver release, e.g. `0.1.0`. Override: `CONTROLLER_VERSION=0.2.0` |
| `GIT_SHA` / `BUILD_DATE` | Short git SHA and UTC build date, auto-derived |
| `TAG` | `$(CONTROLLER_VERSION)-$(BUILD_DATE)-$(GIT_SHA)`, e.g. `0.1.0-20261103-4d922d8`. Override with `TAG=<exact-tag>` |
| `BUILD` | `1` (default) builds and pushes. `BUILD=0` skips both, so a composite rolls out an already-built tag. Combine with `TAG=` |
| `NAMESPACE` | `adeo-litellm`; both controllers are Deployments in this one namespace |
| `ROLLOUT_TIMEOUT` | `300s`, the default wait for every `rollout status` |
| `KUBECTL` | `KUBECONFIG=$${KUBECONFIG:-$(ALAIN_KUBECONFIG)} kubectl`, so no recipe repeats the prefix |
| `LITELLM_LEGACY_TAG` | `latest`, used only for the vendored `litellm` image |
| `MASTER_KEY` | Derived from `deploy/base/gateway` via `scripts/get_master_key.py` (single source of truth) |

Path constants: `CONTROLLER_MANIFEST_{DEV,PROD}`, `CONTROLLER_DEPLOYMENT_{DEV,PROD}`,
`EXCLUSIONS_{DEV,PROD}`, `OVERLAY_{DEV,PROD}`, `GATEWAY_MANIFEST`,
`GATEWAY_DEPLOYMENT_{DEV,PROD}`, `SERVICEMONITOR_PROD`. Every target names these
rather than repeating a literal path, so the dev/prod split is visible at a
glance.

Run `make help` for the same list from the terminal.

## Harbor login

```bash
make login
```
Logs into `$(REGISTRY)` with `--tls-verify=false` (insecure internal registry).

## Pull base images

```bash
make pull            # podman pull python:3.12-slim + the vendored litellm image
make pull-discovery  # podman pull python:3.12-slim
make pull-litellm    # podman pull the vendored litellm image
```

## Build / push / deploy images

### Discovery controller: primitives, then compositions

The controller image is tagged `$(CONTROLLER_VERSION)-$(BUILD_DATE)-$(GIT_SHA)`,
for example `0.1.0-20261103-4d922d8`. There is no `latest` tag. The tag is
immutable, so a node reboot or a rollout can never pull a different build than
the one pinned in the manifest, and the deployed image always identifies the
exact commit it was built from.

Four primitives, one job each. The first three never touch the cluster.

```bash
make controller-build            # podman build $(DISCOVERY_IMG):$(TAG)
make controller-push             # push that tag to Harbor (needs `make login`)
make controller-pin              # point BOTH manifests at $(TAG)
make controller-pin-dev          # point only deploy/dev/discovery-controller-dev.yaml
make controller-pin-prod         # point only deploy/prod/discovery-controller.yaml
make controller-apply-dev        # apply the dev manifest as pinned, wait for rollout
make controller-apply-prod       # apply the prod manifest as pinned, wait for rollout
```

Compositions built from them:

```bash
make controller-release          # build + push + pin both manifests (no cluster)
make controller-release-dev      # build + push + pin the dev manifest only
make controller-release-prod     # build + push + pin the prod manifest only

make controller-deploy-dev       # release-dev + roll dev out (prod untouched)
make controller-deploy-prod      # release-prod + roll prod out
```

#### Shipping an already-validated tag to prod

`BUILD=0` skips both the build and the push, so the composite rolls out an
existing tag instead of rebuilding it. Pair it with `TAG=` to name the exact
build that already passed in dev:

```bash
# prod gets the same bytes dev validated, no rebuild
make controller-deploy-prod BUILD=0 TAG=0.1.0-20261103-4d922d8

# or just repoint the manifest without touching the cluster
make controller-pin-prod TAG=0.1.0-20261103-4d922d8
```

This is the recommended prod path: a tag that ran in dev is the thing worth
promoting, and rebuilding it produces different bytes with the same label shape.

The proven sequence is release to dev, watch one sync cycle, then promote:

```bash
make controller-deploy-dev                          # builds, pins, rolls dev
kubectl -n adeo-litellm logs deploy/oicm-discovery-controller-dev | tail
make controller-deploy-prod BUILD=0 TAG=<the-dev-tag>
```

Current state (2026-11-03): both controllers run
`oicm-discovery-controller:0.1.0-20261103-4d922d8`, pinned from the same build.

Legacy aliases kept so existing scripts keep working: `controller-build-dev`
(alias of `controller-build`) and `controller-push-dev` (alias of
`controller-push`).

Bump `CONTROLLER_VERSION` for a release; the date and SHA change on their own,
so two builds of the same version are still distinguishable.

`make push` and `make push-litellm` still push the vendored upstream `litellm`
image under the moving `$(LITELLM_LEGACY_TAG)` (`latest`).

### LiteLLM source image (from repo root Dockerfile)

```bash
make litellm-src-build        # build litellm-src:<branch>
make litellm-src-push         # push to Harbor (needs `make login` first)
make litellm-src-build-push   # build then push
make litellm-src-pin          # repoint deploy/base/gateway/proxy/deployment.yaml
make litellm-src-deploy-dev   # pin + apply dev overlay + restart dev gateway
make litellm-src-release-dev  # build-push + deploy-dev
make litellm-src-deploy       # pin + apply prod + restart prod gateway
make litellm-src-release      # build-push + deploy
```

CAVEAT: dev and prod reference the SAME tag, so a build overwrites the bytes
behind it for both. `litellm-src-deploy-dev` only restarts the dev Deployment,
so prod keeps serving its already-running container until someone deliberately
runs the prod target. Treat the tag as a dev channel until prod is moved to its
own tag.

Current state (2026-11-03): both gateways run
`litellm-src:jya0-v1.106.6` at digest `sha256:c12a86ab…`, which predates the
keyless-TTS fix (`87c91acdcc`). Prod masks that gap because its Deployment sets
`HOSTED_VLLM_API_KEY`; dev does not, so a keyless `audio_speech` row (drama-3)
fails its health probe there with `AuthenticationError`.

### OICM service-account provisioning

```bash
make oicm-sa-secrets            # generate the two SA Secrets (rotates the password)
                                #   CLUSTER=alain (default) or CLUSTER=abudhabi
make oicm-sa-provision          # run the Al Ain provisioning Job and verify
make oicm-sa-provision-abudhabi # run the Abu Dhabi provisioning Job and verify
make oicm-sa-config             # (re)create the oicm-service-account-provisioner ConfigMap
```

### Cluster apply

One primitive per object group and environment, so a rollout touches only what
it is meant to:

```bash
make dev-controller    # apply dev controller manifest + dev exclusions, wait
make prod-controller   # apply prod controller manifest + prod exclusions, wait
make dev-gateway       # kubectl apply -k deploy/overlays/dev
make prod-gateway      # kubectl apply -k deploy/overlays/prod + prod ServiceMonitor

make deploy-dev        # dev-config + dev-controller + dev-gateway
make deploy            # prod-config + prod-controller + prod-gateway
```

`make deploy` used to be one monolithic apply, which meant shipping a controller
change also re-applied the prod gateway overlay. It is now a composition of
`prod-controller` and `prod-gateway`. Neither restarts a gateway whose image has
not changed; use the `litellm-src-*` targets when the image should move.

```bash
make clean        # podman rmi local image
```

## Local development

```bash
make litellm-local-run         # proxy from local venv on :4000 (no DB/Redis)
make litellm-local-datasource  # proxy against port-forwarded cluster datasources
make port-forward-datasources  # port-forward Postgres/Redis/Prometheus (foreground)
make litellm-local-docker      # run built image locally via podman
make litellm-ui-dev            # Next.js UI dev server on :3000
make litellm-local-stop        # stop the local docker container
```

## Live smoke tests (post-merge, against a deployed gateway)

Behavior checks that run against a **deployed** gateway, not the source tree.
Mandatory after every upstream merge (Step 7 of the
[merge technique](techniques/upstream_merge_technique.md)). See
`tests/smoke/AGENTS.md` for the suite conventions.

```bash
make smoke-fast          # Tier A: contract only, no inference, ~1-2 min
make smoke-models        # Tier A + B: adds real model calls (costs tokens)
make smoke-leaks         # Tier C read-only: task census + cache boundedness
make smoke-leaks-mutate  # Tier C mutating: DEV ONLY, writes routing_strategy_args
make smoke               # every read-only tier (excludes mutating tests)
```

| Variable | Default | Meaning |
|----------|---------|---------|
| `BASE` | `http://localhost:14001` | Target gateway. Use `https://litellm.ecouncil.ae` for prod. |
| `KEY` | unset | A scoped `key_type=llm_api` key. Required for Tier B and the scoped-auth tests; the master key bypasses `allowed_routes` and cannot prove a customer key works. |
| `MASTER_KEY` | manifest value | Auto-derived from `deploy/base/gateway/secrets/litellm-master-key.yaml`. |

Typical post-merge run:

```bash
KUBECONFIG=~/.kube/alain-oicm.conf \
  kubectl -n adeo-litellm port-forward deploy/litellm-proxy-dev 14001:4000 &
cd oicm-litellm-layer
export KEY=sk-...
make smoke
```

> `smoke-leaks-mutate` is excluded from every other target because it writes a
> live router setting through `POST /config/update`, which persists to the DB.
> It restores what it changes, but never point it at prod.

## Docs
```bash
make docs          # build mkdocs site to ./site (gitignored)
```

## Other
```bash
make litellm-logo  # create litellm-logo ConfigMap from decor/ images
```

## Notes
- The kubeconfig default is `~/.kube/alain-oicm.conf` via the `ALAIN_KUBECONFIG`
  variable (an exported `KUBECONFIG` still wins at run time).
- `litellm-local-*` requires the LiteLLM venv at `$(LITELLM_SRC_DIR)/.venv`
  (run `uv sync --extra proxy && uv pip install -e .` in the repo root if missing).
- The `docs` target only **builds** the site locally; there is no push-to-Harbor
  or cluster deploy for the docs site yet (see the Build & Deploy page).