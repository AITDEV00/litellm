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
| `TAG` | `latest` |
| `MASTER_KEY` | Derived from `deploy/prod/litellm-proxy.yaml` via `scripts/get_master_key.py` (single source of truth) |

## Harbor login

```bash
make login
```
Logs into `$(REGISTRY)` with `--tls-verify=false` (insecure internal registry).

## Build / push / deploy images

### Discovery controller (legacy flow)
```bash
make build          # build DISCOVERY_IMG:latest
make push           # push discovery + litellm images
make push-discovery
make push-litellm
```

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