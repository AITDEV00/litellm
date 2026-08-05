REGISTRY     := registry.adeoaiengine.ecouncil.ae
REPO_PATH    := openinnovationai/platform/mlops/mlops-serving
HARBOR_USER  := custom-images
HARBOR_PASS  := CustomImages123

DISCOVERY_IMG := $(REGISTRY)/$(REPO_PATH)/oicm-discovery-controller
LITELLM_IMG   := docker.litellm.ai/berriai/litellm:main-stable
LITELLM_HARBOR_IMG := $(REGISTRY)/$(REPO_PATH)/litellm
TAG           := latest

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

.PHONY: all pull-discovery pull-litellm pull build push-discovery push-litellm push login deploy clean \
	litellm-src-build litellm-src-push litellm-src-build-push litellm-src-release litellm-src-deploy \
	litellm-local-run litellm-local-datasource litellm-local-docker litellm-ui-dev litellm-local-stop \
	port-forward-datasources

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
	podman tag $(LITELLM_IMG) $(LITELLM_HARBOR_IMG):$(TAG)
	podman push --tls-verify=false $(LITELLM_HARBOR_IMG):$(TAG)

## Deploy all manifests to the cluster
deploy:
	KUBECONFIG=$${KUBECONFIG:-$$HOME/.kube/alain-oicm.conf} kubectl apply -f deploy/discovery-controller.yaml
	KUBECONFIG=$${KUBECONFIG:-$$HOME/.kube/alain-oicm.conf} kubectl apply -f deploy/litellm-proxy.yaml
	KUBECONFIG=$${KUBECONFIG:-$$HOME/.kube/alain-oicm.conf} kubectl apply -f deploy/litellm-servicemonitor.yaml

## Remove built images from local storage
clean:
	podman rmi $(DISCOVERY_IMG):$(TAG) 2>/dev/null || true

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

## Update deploy/litellm-proxy.yaml to use the current LITELLM_SRC_TAG image,
## then kubectl apply all deploy manifests.
litellm-src-deploy:
	@echo "Updating image tag to $(LITELLM_SRC_HARBOR_IMG):$(LITELLM_SRC_TAG) in deploy/litellm-proxy.yaml"
	sed -i 's|image: $(LITELLM_SRC_HARBOR_IMG):.*|image: $(LITELLM_SRC_HARBOR_IMG):$(LITELLM_SRC_TAG)|' deploy/litellm-proxy.yaml
	@echo "Deploying to cluster"
	KUBECONFIG=$${KUBECONFIG:-$$HOME/.kube/alain-oicm.conf} kubectl apply -f deploy/discovery-controller.yaml
	KUBECONFIG=$${KUBECONFIG:-$$HOME/.kube/alain-oicm.conf} kubectl apply -f deploy/litellm-proxy.yaml
	KUBECONFIG=$${KUBECONFIG:-$$HOME/.kube/alain-oicm.conf} kubectl apply -f deploy/litellm-servicemonitor.yaml
	@echo "Deployed $(LITELLM_SRC_HARBOR_IMG):$(LITELLM_SRC_TAG)"

## Create the litellm-logo ConfigMap from the optimized logo and favicon files
litellm-logo:
	@echo "Creating litellm-logo ConfigMap"
	KUBECONFIG=$${KUBECONFIG:-$$HOME/.kube/oicm-alain.conf} \
		kubectl create configmap litellm-logo -n mlops \
		--from-file=adeo-ai-gateway.jpg=decor/adeo-ai-gateway.jpg \
		--from-file=favicon.png=decor/favicon.png \
		--dry-run=client -o yaml | \
		KUBECONFIG=$${KUBECONFIG:-$$HOME/.kube/oicm-alain.conf} kubectl apply -f -

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
	@echo "Master key: sk-1234"
	cd $(LITELLM_SRC_DIR) && PYTHONPATH=$(CURDIR) LITELLM_MODE=PRODUCTION env -u DATABASE_URL $(LITELLM_VENV_BIN)/litellm \
		--config $(CURDIR)/$(LITELLM_LOCAL_CONFIG) \
		--port $(LITELLM_LOCAL_PORT) \
		--detailed_debug

## Run LiteLLM against the port-forwarded cluster datasources (Postgres/Redis/
## Prometheus). Start the forwards first (one terminal):
##   make port-forward-datasources
## then run this (another terminal). Uses config/local_datasource.yaml + the
## env values in .env.datasource (copy from .env.datasource.example). See
## docs/DATASOURCE-LOCAL-VALIDATION.md for the full runbook.
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
	cd $(LITELLM_SRC_DIR) && set -a && . $(CURDIR)/.env.datasource && set +a && \
		PATH="$(LITELLM_SRC_DIR)/$(LITELLM_VENV_BIN):$$PATH" $(LITELLM_VENV_BIN)/litellm \
		--config $(CURDIR)/config/local_datasource.yaml \
		--port $(LITELLM_LOCAL_PORT) \
		--detailed_debug

## Port-forward the cluster datasources for local validation (Postgres/Redis/
## Prometheus). Runs in the foreground; Ctrl+C to stop. See
## docs/DATASOURCE-LOCAL-VALIDATION.md.
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
	@echo "Master key: sk-1234"
	@echo "Config: $(CURDIR)/$(LITELLM_LOCAL_CONFIG)"
	podman run --rm -it \
		--name litellm-local \
		-p $(LITELLM_LOCAL_PORT):4000 \
		-v $(CURDIR)/$(LITELLM_LOCAL_CONFIG):/app/config.yaml:Z \
		-v $(CURDIR)/hooks:/app/litellm_hooks:Z \
		-e STORE_MODEL_IN_DB=true \
		-e LITELLM_MASTER_KEY=sk-1234 \
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
