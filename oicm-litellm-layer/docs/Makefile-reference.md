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
| `MASTER_KEY` | Derived from `deploy/base/gateway` via `scripts/get_master_key.py` (single source of truth) |

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

Dev-only iteration flow (prod is never touched):

```bash
make controller-build-dev    # alias of `make build`
make controller-push-dev     # alias of `make push-discovery`
make controller-release-dev  # build + push + pin the DEV manifest only
make controller-deploy-dev   # release-dev + deploy-dev (whole dev loop)
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
make litellm-src-deploy       # sed image tag in deploy/base/gateway/proxy/deployment.yaml, then kubectl apply + restart prod
make litellm-src-release      # build-push + deploy, one shot
make litellm-src-deploy-dev   # pin the tag in the dev overlay and roll dev only
make litellm-src-release-dev  # build-push + deploy-dev
```

### OICM service-account provisioning

```bash
make oicm-sa-secrets            # generate the two SA Secrets (rotates the password)
                                #   CLUSTER=alain (default) or CLUSTER=abudhabi
make oicm-sa-provision          # run the Al Ain provisioning Job and verify
make oicm-sa-provision-abudhabi # run the Abu Dhabi provisioning Job and verify
make oicm-sa-config             # (re)create the oicm-service-account-provisioner ConfigMap
```

### Cluster apply
```bash
make deploy       # kubectl apply deploy/oicm/sources.yaml + exclusions.yaml + deploy/prod/discovery-controller.yaml + deploy/overlays/prod + deploy/prod/litellm-servicemonitor.yaml
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
- The kubeconfig default is `~/.kube/alain-oicm.conf` via the `ALAIN_KUBECONFIG`
  variable (an exported `KUBECONFIG` still wins at run time).
- `litellm-local-*` requires the LiteLLM venv at `$(LITELLM_SRC_DIR)/.venv`
  (run `uv sync --extra proxy && uv pip install -e .` in the repo root if missing).
- The `docs` target only **builds** the site locally; there is no push-to-Harbor
  or cluster deploy for the docs site yet (see the Build & Deploy page).