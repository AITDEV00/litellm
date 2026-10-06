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
| `LITELLM_SRC_TAG` | Sanitized current git branch name (slashes → `_`), e.g. `jya0-v1.96.2`. Override: `LITELLM_SRC_TAG=foo` |
| `CONTROLLER_VERSION` | Discovery controller semver release, e.g. `0.1.0`. Override: `CONTROLLER_VERSION=0.2.0` |
| `GIT_SHA` / `BUILD_DATE` | Short git SHA and UTC build date, auto-derived |
| `TAG` | `$(CONTROLLER_VERSION)-$(BUILD_DATE)-$(GIT_SHA)`, e.g. `0.1.0-20261006-bb95744` |
| `LITELLM_LEGACY_TAG` | `latest`, used only for the vendored `litellm` image |
| `MASTER_KEY` | Derived from `deploy/prod/litellm-proxy.yaml` via `scripts/get_master_key.py` (single source of truth) |

## Harbor login

```bash
make login
```
Logs into `$(REGISTRY)` with `--tls-verify=false` (insecure internal registry).

## Build / push / deploy images

### Discovery controller (versioned flow)

The controller image is tagged `$(CONTROLLER_VERSION)-$(BUILD_DATE)-$(GIT_SHA)`,
for example `0.1.0-20261006-bb95744`. There is no `latest` tag. The tag is
immutable, so a node reboot or a rollout can never pull a different build than
the one pinned in the manifest, and the deployed image always identifies the
exact commit it was built from.

```bash
make build             # build DISCOVERY_IMG:0.1.0-20261006-bb95744
make push-discovery    # push it to Harbor (needs `make login` first)
make controller-release  # build + push + rewrite the tag in the dev and prod
                         # manifests (does NOT apply anything to the cluster)
make deploy-dev          # apply the dev controller manifest only
```

Bump `CONTROLLER_VERSION` for a release; the date and SHA change on their own,
so two builds of the same version are still distinguishable. Build and push
both target the same tag, so a redeploy always picks up the newly built image.

`make push` and `make push-litellm` still push the vendored upstream `litellm`
image under the moving `$(LITELLM_LEGACY_TAG)` (`latest`).

### LiteLLM source image (from repo root Dockerfile)
```bash
make litellm-src-build        # build litellm-src:<branch>
make litellm-src-push         # push to Harbor (needs `make login` first)
make litellm-src-build-push   # build then push
make litellm-src-deploy       # sed image tag in deploy/prod/litellm-proxy.yaml, then kubectl apply
make litellm-src-release      # build-push + deploy, one shot
```

### Cluster apply
```bash
make deploy       # kubectl apply deploy/prod/discovery-controller.yaml + deploy/prod/litellm-proxy.yaml + deploy/prod/litellm-servicemonitor.yaml
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

## Docs
```bash
make docs          # build mkdocs site to ./site (gitignored)
```

## Other
```bash
make litellm-logo  # create litellm-logo ConfigMap from decor/ images
```

## Notes
- The kubeconfig default is not uniform: most targets use `~/.kube/oicm-alain.conf`,
  while `litellm-src-deploy` / `deploy` fall back to `~/.kube/alain-oicm.conf`.
  Override with `KUBECONFIG=...`.
- `litellm-local-*` requires the LiteLLM venv at `$(LITELLM_SRC_DIR)/.venv`
  (run `uv sync --extra proxy && uv pip install -e .` in the repo root if missing).
- The `docs` target only **builds** the site locally; there is no push-to-Harbor
  or cluster deploy for the docs site yet (see the Build & Deploy page).