REGISTRY     := registry.adeoaiengine.ecouncil.ae
REPO_PATH    := openinnovationai/platform/mlops/mlops-serving
HARBOR_USER  := custom-images
HARBOR_PASS  := CustomImages123

DISCOVERY_IMG := $(REGISTRY)/$(REPO_PATH)/oicm-discovery-controller
LITELLM_IMG   := docker.litellm.ai/berriai/litellm:main-stable
LITELLM_HARBOR_IMG := $(REGISTRY)/$(REPO_PATH)/litellm

# Discovery controller image tag: a semver release plus a git-SHA build stamp,
# e.g. 0.1.0-20261006-bb95744. The version is the single source of truth for
# which build is running; the SHA is the immutable part that makes two builds of
# the same version distinguishable, so an unchanged version still re-pulls.
CONTROLLER_VERSION ?= 0.1.0
GIT_SHA            := $(shell git rev-parse --short=7 HEAD)
BUILD_DATE         := $(shell date -u +%Y%m%d)
TAG                := $(CONTROLLER_VERSION)-$(BUILD_DATE)-$(GIT_SHA)

# Docker tag for the legacy vendored litellm image. The controller now uses
# version tags, but litellm still tracks the moving `main-stable` upstream.
LITELLM_LEGACY_TAG := latest

# LiteLLM source image (built from the upstream Dockerfile at the repo root).
# The tag is derived from the current git branch of the litellm repo, with
# slashes replaced by underscores so it stays a valid Docker tag. On branch
# jya0-v1.89.3 this yields: .../litellm:jya0-v1.89.3
LITELLM_SRC_DIR    := ..
LITELLM_DOCKERFILE := $(LITELLM_SRC_DIR)/Dockerfile
LITELLM_SRC_TAG    ?= $(shell git -C $(LITELLM_SRC_DIR) rev-parse --abbrev-ref HEAD | tr '/' '_')
LITELLM_SRC_HARBOR_IMG := $(REGISTRY)/$(REPO_PATH)/litellm-src

LITELLM_VENV_BIN := .venv/bin
LITELLM_LOCAL_CONFIG := config/local_dev.yaml
LITELLM_LOCAL_PORT := 4000
LITELLM_UI_DIR    := $(LITELLM_SRC_DIR)/ui/litellm-dashboard

# The kubeconfig for the Al Ain cluster, where every target here runs. One
# variable so a renamed or relocated kubeconfig is a one-line change, and so no
# target can point at a file that does not exist. An exported KUBECONFIG still
# wins at run time, which is what makes these targets overridable per shell.
ALAIN_KUBECONFIG ?= $(HOME)/.kube/alain-oicm.conf

# Master key for local runs, derived from the single source of truth: the
# `litellm-master-key` Secret in deploy/base/gateway/secrets, the base both
# environment overlays build on. A rotation in the base propagates here, and to
# both environments, automatically.
MASTER_KEY := $(shell python3 scripts/get_master_key.py 2>/dev/null)

.PHONY: all pull-discovery pull-litellm pull build push-discovery push-litellm push login deploy clean \
	controller-release controller-build-dev controller-push-dev controller-release-dev controller-deploy-dev deploy-dev \
	litellm-src-build litellm-src-push litellm-src-build-push litellm-src-release litellm-src-deploy \
	litellm-local-run litellm-local-datasource litellm-local-docker litellm-ui-dev litellm-local-stop \
	port-forward-datasources docs

all: pull build push

## Pull base/dependency images
pull: pull-discovery pull-litellm

pull-discovery:
	podman pull python:3.12-slim

pull-litellm:
	podman pull $(LITELLM_IMG)

## Build the discovery controller image
build:
	cp ../model_prices_and_context_window.json model_prices_and_context_window.json
	podman build -t $(DISCOVERY_IMG):$(TAG) -f controller/Dockerfile .
	rm -f model_prices_and_context_window.json

## Login to the internal Harbor registry
login:
	@echo "Logging in to $(REGISTRY)"
	@echo "$(HARBOR_PASS)" | podman login --tls-verify=false $(REGISTRY) -u $(HARBOR_USER) --password-stdin

## Push images to Harbor (insecure, internal registry)
push: push-discovery push-litellm

push-discovery:
	podman push --tls-verify=false $(DISCOVERY_IMG):$(TAG)

push-litellm:
	podman tag $(LITELLM_IMG) $(LITELLM_HARBOR_IMG):$(LITELLM_LEGACY_TAG)
	podman push --tls-verify=false $(LITELLM_HARBOR_IMG):$(LITELLM_LEGACY_TAG)

## Build, push, and pin the version tag in the dev and prod manifests. This only
## edits the manifest files and pushes the image; it never touches the cluster,
## so prod is not upgraded until someone deliberately runs `make deploy`.
## Use `make controller-release-dev` when only dev should move.
controller-release: build push-discovery
	@echo "Pinning $(DISCOVERY_IMG):$(TAG)"
	sed -i 's|image: $(DISCOVERY_IMG):.*|image: $(DISCOVERY_IMG):$(TAG)|' deploy/dev/discovery-controller-dev.yaml
	sed -i 's|image: $(DISCOVERY_IMG):.*|image: $(DISCOVERY_IMG):$(TAG)|' deploy/prod/discovery-controller.yaml
	@echo "Pinned dev and prod manifests. Prod was NOT applied; run 'make deploy' for that."

## Build the discovery controller image for a dev iteration. Same image as
## `build`; named separately so the dev flow reads as one thing.
controller-build-dev: build

## Push the dev build to Harbor (requires `make login` first)
controller-push-dev: push-discovery

## Build, push, and pin the tag in the DEV manifest only. Prod's manifest is
## left untouched, so a dev iteration can never move production. Follow with
## `make deploy-dev`. This is the dev counterpart of `controller-release`.
controller-release-dev: build push-discovery
	@echo "Pinning $(DISCOVERY_IMG):$(TAG) in the dev manifest only"
	sed -i 's|image: $(DISCOVERY_IMG):.*|image: $(DISCOVERY_IMG):$(TAG)|' deploy/dev/discovery-controller-dev.yaml
	@echo "Pinned deploy/dev/discovery-controller-dev.yaml. Prod manifest untouched."

## Build, push, pin the dev manifest, and roll the dev controller out. One
## command for the whole dev loop. Prod is never touched.
controller-deploy-dev: controller-release-dev deploy-dev

## Apply the dev controller manifest and the dev gateway overlay. Also applies the
## oicm-sources ConfigMap the controller mounts, so the target is self-contained
## rather than depending on a ConfigMap someone applied by hand.
deploy-dev:
	KUBECONFIG=$${KUBECONFIG:-$(ALAIN_KUBECONFIG)} kubectl apply -f deploy/oicm/sources.yaml
	KUBECONFIG=$${KUBECONFIG:-$(ALAIN_KUBECONFIG)} kubectl apply -f deploy/oicm/exclusions.yaml
	KUBECONFIG=$${KUBECONFIG:-$(ALAIN_KUBECONFIG)} kubectl apply -f deploy/dev/discovery-controller-dev.yaml
	KUBECONFIG=$${KUBECONFIG:-$(ALAIN_KUBECONFIG)} kubectl apply -k deploy/overlays/dev

## Deploy all prod manifests to the cluster. Applies the prod overlay; note this
## does NOT restart the gateway, so it keeps serving its running container.
## Use `litellm-src-deploy` when the image should move too.
deploy:
	KUBECONFIG=$${KUBECONFIG:-$(ALAIN_KUBECONFIG)} kubectl apply -f deploy/oicm/sources.yaml
	KUBECONFIG=$${KUBECONFIG:-$(ALAIN_KUBECONFIG)} kubectl apply -f deploy/oicm/exclusions.yaml
	KUBECONFIG=$${KUBECONFIG:-$(ALAIN_KUBECONFIG)} kubectl apply -f deploy/prod/discovery-controller.yaml
	KUBECONFIG=$${KUBECONFIG:-$(ALAIN_KUBECONFIG)} kubectl apply -k deploy/overlays/prod
	KUBECONFIG=$${KUBECONFIG:-$(ALAIN_KUBECONFIG)} kubectl apply -f deploy/prod/litellm-servicemonitor.yaml

## Build the ConfigMap that carries the service-account provisioner script.
## Both cluster Jobs mount it, so the script lives in exactly one place.
oicm-sa-config:
	KUBECONFIG=$${KUBECONFIG:-$(ALAIN_KUBECONFIG)} kubectl -n adeo-litellm \
		create configmap oicm-service-account-provisioner \
		--from-file=provision_keycloak_service_account.py=deploy/oicm/provision_keycloak_service_account.py \
		--dry-run=client -o yaml | \
		KUBECONFIG=$${KUBECONFIG:-$(ALAIN_KUBECONFIG)} kubectl apply -f -

## Provision the Al Ain OICM service account and verify it end to end.
## Run oicm-sa-secrets first, which creates the two Secrets it mounts.
oicm-sa-provision: oicm-sa-config
	KUBECONFIG=$${KUBECONFIG:-$(ALAIN_KUBECONFIG)} kubectl apply -f deploy/oicm/provision-alain.yaml
	KUBECONFIG=$${KUBECONFIG:-$(ALAIN_KUBECONFIG)} kubectl -n adeo-litellm delete job provision-oicm-sa-alain --ignore-not-found
	KUBECONFIG=$${KUBECONFIG:-$(ALAIN_KUBECONFIG)} kubectl apply -f deploy/oicm/provision-alain.yaml
	KUBECONFIG=$${KUBECONFIG:-$(ALAIN_KUBECONFIG)} kubectl -n adeo-litellm wait --for=condition=Complete job/provision-oicm-sa-alain --timeout=180s
	KUBECONFIG=$${KUBECONFIG:-$(ALAIN_KUBECONFIG)} kubectl -n adeo-litellm logs job/provision-oicm-sa-alain

## Provision the Abu Dhabi OICM service account and verify it end to end.
## Runs in Al Ain, because that is where the controller consuming the credential
## lives and where the clusterset names to Abu Dhabi resolve (pods on
## adeo-gpu-03). Run `make oicm-sa-secrets CLUSTER=abudhabi` first.
oicm-sa-provision-abudhabi: oicm-sa-config
	KUBECONFIG=$${KUBECONFIG:-$(ALAIN_KUBECONFIG)} kubectl -n adeo-litellm delete job provision-oicm-sa-abudhabi --ignore-not-found
	KUBECONFIG=$${KUBECONFIG:-$(ALAIN_KUBECONFIG)} kubectl apply -f deploy/oicm/provision-abudhabi.yaml
	KUBECONFIG=$${KUBECONFIG:-$(ALAIN_KUBECONFIG)} kubectl -n adeo-litellm wait --for=condition=Complete job/provision-oicm-sa-abudhabi --timeout=180s
	KUBECONFIG=$${KUBECONFIG:-$(ALAIN_KUBECONFIG)} kubectl -n adeo-litellm logs job/provision-oicm-sa-abudhabi

## Generate the two Secrets the provisioning Jobs mount. Rotates the service
## account password, so follow with the matching provisioning target.
oicm-sa-secrets:
	scripts/make_service_account_secrets.sh $${CLUSTER:-alain}

## Remove built images from local storage
clean:
	podman rmi $(DISCOVERY_IMG):$(TAG) 2>/dev/null || true
	podman rmi $(LITELLM_HARBOR_IMG):$(LITELLM_LEGACY_TAG) 2>/dev/null || true

## Build the LiteLLM source image (with the enterprise dev override) from the
## upstream Dockerfile at the repo root. Tag is the sanitized git branch name,
## e.g. jya0-v1.89.3. Override with: make litellm-src-build LITELLM_SRC_TAG=foo
litellm-src-build:
	@echo "Building $(LITELLM_SRC_HARBOR_IMG):$(LITELLM_SRC_TAG)"
	podman build \
		--tag $(LITELLM_SRC_HARBOR_IMG):$(LITELLM_SRC_TAG) \
		--file $(LITELLM_DOCKERFILE) \
		$(LITELLM_SRC_DIR)
	@echo "Built $(LITELLM_SRC_HARBOR_IMG):$(LITELLM_SRC_TAG)"

## Push the LiteLLM source image to Harbor (requires `make login` first)
litellm-src-push:
	@echo "Pushing $(LITELLM_SRC_HARBOR_IMG):$(LITELLM_SRC_TAG) to Harbor"
	podman push --tls-verify=false $(LITELLM_SRC_HARBOR_IMG):$(LITELLM_SRC_TAG)
	@echo "Pushed $(LITELLM_SRC_HARBOR_IMG):$(LITELLM_SRC_TAG)"

## Build then push the LiteLLM source image in one step
litellm-src-build-push: litellm-src-build litellm-src-push

## Build, push, update the deploy manifest image tag, and apply to the cluster.
## Uses LITELLM_SRC_TAG (defaults to current git branch name).
## e.g. make litellm-src-release  (on branch jya0-v1.95.0 -> tag jya0-v1.95.0)
litellm-src-release: litellm-src-build-push litellm-src-deploy

## Update the prod overlay's image to the current LITELLM_SRC_TAG, then apply all
## deploy manifests. The image lives in deploy/base/gateway/proxy/deployment.yaml,
## which both overlays build on, so pinning it moves prod and dev together.
litellm-src-deploy:
	@echo "Updating image tag to $(LITELLM_SRC_HARBOR_IMG):$(LITELLM_SRC_TAG) in deploy/base/gateway/proxy/deployment.yaml"
	sed -i 's|image: $(LITELLM_SRC_HARBOR_IMG):.*|image: $(LITELLM_SRC_HARBOR_IMG):$(LITELLM_SRC_TAG)|' deploy/base/gateway/proxy/deployment.yaml
	@echo "Deploying to cluster"
	KUBECONFIG=$${KUBECONFIG:-$(ALAIN_KUBECONFIG)} kubectl apply -f deploy/oicm/sources.yaml
	KUBECONFIG=$${KUBECONFIG:-$(ALAIN_KUBECONFIG)} kubectl apply -f deploy/oicm/exclusions.yaml
	KUBECONFIG=$${KUBECONFIG:-$(ALAIN_KUBECONFIG)} kubectl apply -f deploy/prod/discovery-controller.yaml
	KUBECONFIG=$${KUBECONFIG:-$(ALAIN_KUBECONFIG)} kubectl apply -k deploy/overlays/prod
	KUBECONFIG=$${KUBECONFIG:-$(ALAIN_KUBECONFIG)} kubectl apply -f deploy/prod/litellm-servicemonitor.yaml
	@echo "Restarting litellm-proxy to pull the freshly tagged image"
	KUBECONFIG=$${KUBECONFIG:-$(ALAIN_KUBECONFIG)} kubectl -n adeo-litellm rollout restart deployment/litellm-proxy
	KUBECONFIG=$${KUBECONFIG:-$(ALAIN_KUBECONFIG)} kubectl -n adeo-litellm rollout status deployment/litellm-proxy --timeout=300s
	@echo "Deployed $(LITELLM_SRC_HARBOR_IMG):$(LITELLM_SRC_TAG)"

## Build, push, then roll the DEV gateway out. The dev counterpart of
## `litellm-src-release`, and the ONLY safe way to ship the gateway to dev.
##
## CAVEAT: dev and prod reference the SAME tag (`litellm-src:$(LITELLM_SRC_TAG)`),
## so a build overwrites the bytes behind that tag for both. This target only
## restarts the dev Deployment, so prod keeps serving its already-running
## container until prod is deliberately restarted. Treat the tag as a dev
## channel until prod is moved to its own tag.
litellm-src-release-dev: litellm-src-build-push litellm-src-deploy-dev

## Pin the current LITELLM_SRC_TAG in the DEV overlay and roll it out.
## Prod's overlay and Deployment are never touched.
litellm-src-deploy-dev:
	@echo "Pinning image tag $(LITELLM_SRC_HARBOR_IMG):$(LITELLM_SRC_TAG) in deploy/base/gateway/proxy/deployment.yaml"
	sed -i 's|image: $(LITELLM_SRC_HARBOR_IMG):.*|image: $(LITELLM_SRC_HARBOR_IMG):$(LITELLM_SRC_TAG)|' deploy/base/gateway/proxy/deployment.yaml
	@echo "Deploying dev gateway"
	KUBECONFIG=$${KUBECONFIG:-$(ALAIN_KUBECONFIG)} kubectl apply -k deploy/overlays/dev
	@echo "Restarting litellm-proxy-dev to pull the freshly tagged image"
	KUBECONFIG=$${KUBECONFIG:-$(ALAIN_KUBECONFIG)} kubectl -n adeo-litellm rollout restart deployment/litellm-proxy-dev
	KUBECONFIG=$${KUBECONFIG:-$(ALAIN_KUBECONFIG)} kubectl -n adeo-litellm rollout status deployment/litellm-proxy-dev --timeout=300s
	@echo "Deployed $(LITELLM_SRC_HARBOR_IMG):$(LITELLM_SRC_TAG) to dev. Prod overlay untouched."

## Build the MkDocs documentation site. Requires the docs venv (see
## requirements-docs.txt). Used by the rotation runbook in docs/credentials.md
## to re-inject the current master key into the built docs.
docs:
	python3 -m venv .venv-docs 2>/dev/null; \
	.venv-docs/bin/pip install -q -r requirements-docs.txt; \
	.venv-docs/bin/mkdocs build

## Create the litellm-logo ConfigMap from the optimized logo and favicon files
litellm-logo:
	@echo "Creating litellm-logo ConfigMap"
	KUBECONFIG=$${KUBECONFIG:-$(ALAIN_KUBECONFIG)} \
		kubectl create configmap litellm-logo -n adeo-litellm \
		--from-file=adeo-ai-gateway.jpg=decor/adeo-ai-gateway.jpg \
		--from-file=favicon.png=decor/favicon.png \
		--dry-run=client -o yaml | \
		KUBECONFIG=$${KUBECONFIG:-$(ALAIN_KUBECONFIG)} kubectl apply -f -

## Run LiteLLM proxy from the local venv (no Docker). Uses config/local_dev.yaml
## which has no DB/Redis deps so it starts instantly. The proxy serves the
## packaged UI bundle at http://localhost:4000/ui/. Use this to test backend
## changes or the built UI. Override the config with:
##   make litellm-local-run LITELLM_LOCAL_CONFIG=../litellm/proxy/dev_config.yaml
litellm-local-run:
	@if [ ! -x "$(LITELLM_SRC_DIR)/$(LITELLM_VENV_BIN)/litellm" ]; then \
		echo "LiteLLM not found in $(LITELLM_SRC_DIR)/$(LITELLM_VENV_BIN). Run: cd $(LITELLM_SRC_DIR) && uv sync --extra proxy && uv pip install -e ."; \
		exit 1; \
	fi
	@echo "Starting LiteLLM proxy on port $(LITELLM_LOCAL_PORT) with $(LITELLM_LOCAL_CONFIG)"
	@echo "UI: http://localhost:$(LITELLM_LOCAL_PORT)/ui/"
	@echo "API: http://localhost:$(LITELLM_LOCAL_PORT)/v1/models"
	@echo "Master key: $(MASTER_KEY)"
	cd $(LITELLM_SRC_DIR) && PYTHONPATH=$(CURDIR) LITELLM_MODE=PRODUCTION LITELLM_MASTER_KEY=$(MASTER_KEY) env -u DATABASE_URL $(LITELLM_VENV_BIN)/litellm \
		--config $(CURDIR)/$(LITELLM_LOCAL_CONFIG) \
		--port $(LITELLM_LOCAL_PORT) \
		--detailed_debug

## Run LiteLLM against the port-forwarded cluster datasources (Postgres/Redis/
## Prometheus). Start the forwards first (one terminal):
##   make port-forward-datasources
## then run this (another terminal). Uses config/local_datasource.yaml + the
## env values in .env.datasource (copy from .env.datasource.example). See
## docs/runbooks/DATASOURCE-LOCAL-VALIDATION.md for the full runbook.
litellm-local-datasource:
	@if [ ! -f "$(CURDIR)/.env.datasource" ]; then \
		echo "Missing $(CURDIR)/.env.datasource. Copy from .env.datasource.example and fill in secrets."; \
		exit 1; \
	fi
	@if [ ! -x "$(LITELLM_SRC_DIR)/$(LITELLM_VENV_BIN)/litellm" ]; then \
		echo "LiteLLM not found in $(LITELLM_SRC_DIR)/$(LITELLM_VENV_BIN). Run: cd $(LITELLM_SRC_DIR) && uv sync --extra proxy && uv pip install -e ."; \
		exit 1; \
	fi
	@echo "Starting LiteLLM proxy against forwarded datasources on port $(LITELLM_LOCAL_PORT)"
	@echo "UI: http://localhost:$(LITELLM_LOCAL_PORT)/ui/"
	@echo "API: http://localhost:$(LITELLM_LOCAL_PORT)/v1/models"
	cd $(LITELLM_SRC_DIR) && export LITELLM_MASTER_KEY=$(MASTER_KEY) && set -a && . $(CURDIR)/.env.datasource && set +a && \
		PATH="$(LITELLM_SRC_DIR)/$(LITELLM_VENV_BIN):$$PATH" $(LITELLM_VENV_BIN)/litellm \
		--config $(CURDIR)/config/local_datasource.yaml \
		--port $(LITELLM_LOCAL_PORT) \
		--detailed_debug

## Port-forward the cluster datasources for local validation (Postgres/Redis/
## Prometheus). Runs in the foreground; Ctrl+C to stop. See
## docs/runbooks/DATASOURCE-LOCAL-VALIDATION.md.
port-forward-datasources:
	./scripts/port-forward-datasources.sh --validate

## Run the built Docker image locally with podman. Mounts the local config and
## hooks directory so changes are picked up without rebuilding. The UI is the
## one baked into the image (from the last `make litellm-src-build`). Use this
## to test the full image including the UI build before pushing.
## Override the tag with: make litellm-local-docker LITELLM_SRC_TAG=foo
litellm-local-docker:
	@echo "Running $(LITELLM_SRC_HARBOR_IMG):$(LITELLM_SRC_TAG) on port $(LITELLM_LOCAL_PORT)"
	@echo "UI: http://localhost:$(LITELLM_LOCAL_PORT)/ui/"
	@echo "API: http://localhost:$(LITELLM_LOCAL_PORT)/v1/models"
	@echo "Master key: $(MASTER_KEY)"
	@echo "Config: $(CURDIR)/$(LITELLM_LOCAL_CONFIG)"
	podman run --rm -it \
		--name litellm-local \
		-p $(LITELLM_LOCAL_PORT):4000 \
		-v $(CURDIR)/$(LITELLM_LOCAL_CONFIG):/app/config.yaml:Z \
		-v $(CURDIR)/hooks:/app/litellm_hooks:Z \
		-e STORE_MODEL_IN_DB=true \
		-e LITELLM_MASTER_KEY=$(MASTER_KEY) \
		$(LITELLM_SRC_HARBOR_IMG):$(LITELLM_SRC_TAG) \
		--config /app/config.yaml \
		--port 4000 \
		--detailed_debug

## Run the UI dev server with hot reload (Next.js dev mode). Points at a local
## proxy running on port 4000. Start the proxy first with `make litellm-local-run`
## or `make litellm-local-docker`, then run this in a second terminal.
## UI: http://localhost:3000/ui/
litellm-ui-dev:
	@if [ ! -d "$(LITELLM_UI_DIR)/node_modules" ]; then \
		echo "node_modules not found. Run: cd $(LITELLM_UI_DIR) && npm ci"; \
		exit 1; \
	fi
	@echo "Starting UI dev server on port 3000 (proxy at localhost:$(LITELLM_LOCAL_PORT))"
	@echo "UI: http://localhost:3000/ui/"
	cd $(LITELLM_UI_DIR) && \
		NEXT_PUBLIC_BASE_URL="http://localhost:$(LITELLM_LOCAL_PORT)" \
		npm run dev

## Stop a locally-running Docker LiteLLM container (started by litellm-local-docker)
litellm-local-stop:
	-podman rm -f litellm-local 2>/dev/null || true
