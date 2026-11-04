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

# Extra `podman build` flags, e.g. to route around a DNS sinkhole on this box.
# The admin-mcp dependency group pulls a tarball from codeload.github.com, which
# resolves to 6.6.6.6 here. Pass the real address with:
#   make litellm-src-build LITELLM_SRC_BUILD_ARGS=--add-host=codeload.github.com:20.233.83.150
LITELLM_SRC_BUILD_ARGS ?=

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

# Base URL the smoke suite targets. Defaults to the dev gateway port-forward;
# pass BASE=https://litellm.ecouncil.ae for prod. KEY (a scoped virtual key) is
# only needed for the Tier-B model-contract and scoped-auth tests.
BASE ?= http://localhost:14001

# Namespace every workload here lives in. Dev and prod controllers are two
# Deployments in ONE namespace, so the manifest path is the only thing that
# separates them; nothing else needs to differ between environments.
NAMESPACE ?= adeo-litellm

# One kubeconfig prefix for every cluster call, so a target cannot forget it and
# a relocated kubeconfig is a one-line change.
KUBECTL := KUBECONFIG=$${KUBECONFIG:-$(ALAIN_KUBECONFIG)} kubectl

ROLLOUT_TIMEOUT ?= 300s

# Paths, one variable per environment. Every target below names these instead of
# repeating a literal path, so moving a manifest is a one-line change and the
# dev/prod split is visible at a glance rather than buried in each recipe.
CONTROLLER_MANIFEST_DEV  := deploy/dev/discovery-controller-dev.yaml
CONTROLLER_MANIFEST_PROD := deploy/prod/discovery-controller.yaml
CONTROLLER_DEPLOYMENT_DEV  := oicm-discovery-controller-dev
CONTROLLER_DEPLOYMENT_PROD := oicm-discovery-controller

EXCLUSIONS_DEV  := deploy/dev/oicm-exclusions-dev.yaml
EXCLUSIONS_PROD := deploy/oicm/exclusions.yaml

OVERLAY_DEV  := deploy/overlays/dev
OVERLAY_PROD := deploy/overlays/prod

GATEWAY_MANIFEST := deploy/base/gateway/proxy/deployment.yaml
GATEWAY_DEPLOYMENT_DEV  := litellm-proxy-dev
GATEWAY_DEPLOYMENT_PROD := litellm-proxy
SERVICEMONITOR_PROD := deploy/prod/litellm-servicemonitor.yaml

# Build gate for the deploy composites. `make controller-deploy-prod BUILD=0`
# rolls out whatever $(TAG) names without rebuilding it, which is how a tag
# already validated in dev reaches prod with the exact bytes that were tested.
BUILD ?= 1

# Pin an image tag into one or more manifests. $(1) image repo, $(2) tag,
# $(3) manifest paths. One definition so the sed pattern and the echo cannot
# drift between environments, which is how the dev and prod pins stayed equal.
define pin_image
for f in $(3); do sed -i 's|image: $(1):.*|image: $(1):$(2)|' $$f; echo "  $$f -> $(1):$(2)"; done
endef

# Roll one Deployment out and wait for it. $(1) deployment, $(2) timeout.
define rollout
$(KUBECTL) -n $(NAMESPACE) rollout restart deployment/$(1) && \
$(KUBECTL) -n $(NAMESPACE) rollout status deployment/$(1) --timeout=$(if $(2),$(2),$(ROLLOUT_TIMEOUT))
endef

# Apply one controller manifest and wait for its rollout.
# $(1) manifest path, $(2) deployment name.
define apply_controller
$(KUBECTL) apply -f $(1) && \
$(KUBECTL) -n $(NAMESPACE) rollout status deployment/$(2) --timeout=$(ROLLOUT_TIMEOUT)
endef

.PHONY: all pull-discovery pull-litellm pull build push-discovery push-litellm push login \
	controller-build controller-push controller-build-push \
	controller-pin controller-pin-dev controller-pin-prod \
	controller-apply controller-apply-dev controller-apply-prod \
	controller-release controller-release-dev controller-release-prod \
	controller-deploy-dev controller-deploy-prod controller-build-dev controller-push-dev \
	prod-config prod-controller prod-gateway prod-extras deploy \
	dev-config dev-controller dev-gateway deploy-dev \
	litellm-src-build litellm-src-push litellm-src-build-push litellm-src-pin \
	litellm-src-deploy litellm-src-release litellm-src-deploy-dev litellm-src-release-dev \
	litellm-local-run litellm-local-datasource litellm-local-docker litellm-ui-dev litellm-local-stop \
	smoke smoke-fast smoke-models smoke-leaks smoke-leaks-mutate \
	help clean clean-artifacts \
	oicm-sa-config oicm-sa-provision oicm-sa-provision-abudhabi oicm-sa-secrets \
	docs litellm-logo port-forward-datasources

# These composites build a controller (or gateway) image and then apply it, so a
# failed build must stop the rollout and the pin must land before the apply. A
# parallel `make -j` would otherwise interleave them.
.NOTPARALLEL: controller-deploy-dev controller-deploy-prod \
	litellm-src-deploy litellm-src-deploy-dev

all: pull build push

## List the targets this Makefile offers, with their one-line descriptions.
help:
	@echo "Discovery controller + gateway + local dev, for the ADEO LiteLLM layer."
	@echo ""
	@echo "Controller, fine-grained. Build/push/pin never touch the cluster."
	@echo "  controller-build            build $(DISCOVERY_IMG):\$$(TAG)"
	@echo "  controller-push             push that tag to Harbor"
	@echo "  controller-pin              point BOTH manifests at that tag"
	@echo "  controller-pin-dev          point only the dev manifest"
	@echo "  controller-pin-prod         point only the prod manifest"
	@echo "  controller-apply-dev        apply the dev manifest as pinned + wait"
	@echo "  controller-apply-prod       apply the prod manifest as pinned + wait"
	@echo ""
	@echo "Controller, composed."
	@echo "  controller-release[-dev|-prod]   build + push + pin (no cluster)"
	@echo "  controller-deploy-dev            ... + roll out dev"
	@echo "  controller-deploy-prod           ... + roll out prod"
	@echo "  controller-deploy-prod BUILD=0 TAG=<validated-tag>"
	@echo "                                   roll out existing bytes, no rebuild"
	@echo "  controller-apply-prod            just apply the pinned manifest"
	@echo ""
	@echo "Cluster state, per object group."
	@echo "  dev-controller / prod-controller  controller manifest + exclusions"
	@echo "  dev-gateway / prod-gateway         gateway overlay (+ ServiceMonitor)"
	@echo "  deploy-dev / deploy               both groups, one environment"
	@echo ""
	@echo "Gateway image."
	@echo "  litellm-src-build-push        build + push litellm-src:\$$(LITELLM_SRC_TAG)"
	@echo "  litellm-src-release-dev       build, push, pin, roll out DEV only"
	@echo "  litellm-src-release           same for prod"
	@echo ""
	@echo "Local + smoke.  make litellm-local-run | litellm-ui-dev | smoke-fast"
	@echo "Overridable: BASE KEY TAG LITELLM_SRC_TAG BUILD NAMESPACE ROLLOUT_TIMEOUT"

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
# ---------------------------------------------------------------------------
# Discovery controller: four primitives, composed per environment.
#
# Each primitive does exactly one thing, so a rollout can use the pieces it
# actually needs. That matters most for prod: rolling out a tag already
# validated in dev is `make controller-deploy-prod BUILD=0`, which reuses the
# tested image bytes instead of rebuilding them.
#
#   build    podman build at $(TAG)      (BUILD=0 skips it in the composites)
#   push     push $(TAG) to Harbor
#   pin      rewrite one environment's manifest to $(TAG)
#   apply    kubectl apply that manifest + wait for the rollout
#
#   controller-release[-dev|-prod]  = build + push + pin   (no cluster access)
#   controller-deploy-{dev,prod}    = release + apply
# ---------------------------------------------------------------------------

## Build the discovery controller image at $(TAG) (version-date-gitsha).
## Inside the composites this honours BUILD=0, which reuses an already-built and
## already-pushed tag (the path a dev-validated build takes to prod).
controller-build:
ifeq ($(BUILD),1)
	$(MAKE) build
else
	@echo "BUILD=0: reusing existing $(DISCOVERY_IMG):$(TAG) without rebuilding"
endif

## Push the discovery controller image at $(TAG) to Harbor (run `make login`).
## Honours BUILD=0 the same way: a reused tag is already in the registry.
controller-push:
ifeq ($(BUILD),1)
	$(MAKE) push-discovery
else
	@echo "BUILD=0: $(DISCOVERY_IMG):$(TAG) is already pushed; skipping push"
endif

controller-build-push: controller-build controller-push

## Point one environment's manifest at $(TAG) without touching the cluster.
## Targets below take ENV=dev or ENV=prod.
controller-pin-dev:
	@echo "Pinning $(DISCOVERY_IMG):$(TAG) in $(CONTROLLER_MANIFEST_DEV)"
	$(call pin_image,$(DISCOVERY_IMG),$(TAG),$(CONTROLLER_MANIFEST_DEV))
	@echo "Prod manifest untouched."

controller-pin-prod:
	@echo "Pinning $(DISCOVERY_IMG):$(TAG) in $(CONTROLLER_MANIFEST_PROD)"
	$(call pin_image,$(DISCOVERY_IMG),$(TAG),$(CONTROLLER_MANIFEST_PROD))
	@echo "Dev manifest untouched."

## Point both manifests at $(TAG). Useful when both environments run one build.
controller-pin:
	@echo "Pinning $(DISCOVERY_IMG):$(TAG) in both manifests"
	$(call pin_image,$(DISCOVERY_IMG),$(TAG),$(CONTROLLER_MANIFEST_DEV) $(CONTROLLER_MANIFEST_PROD))
	@echo "Pinned dev and prod manifests. Nothing was applied."

## Apply the dev controller manifest at whatever tag it currently pins.
controller-apply-dev:
	$(call apply_controller,$(CONTROLLER_MANIFEST_DEV),$(CONTROLLER_DEPLOYMENT_DEV))

## Apply the prod controller manifest at whatever tag it currently pins.
controller-apply-prod:
	$(call apply_controller,$(CONTROLLER_MANIFEST_PROD),$(CONTROLLER_DEPLOYMENT_PROD))

## Build, push, and pin BOTH manifests; no cluster access.
controller-release: controller-build controller-push controller-pin

## Build, push, and pin the DEV manifest only.
controller-release-dev: controller-build controller-push controller-pin-dev

## Build, push, and pin the PROD manifest only.
controller-release-prod: controller-build controller-push controller-pin-prod

## Build, push, pin, and roll the dev controller out. Prod is never touched.
controller-deploy-dev: controller-release-dev dev-controller

## Build, push, pin, and roll the prod controller out.
## Reuse an already-validated tag instead of rebuilding: BUILD=0
controller-deploy-prod: controller-release-prod prod-controller

## Dev counterparts kept under their original names, so an existing muscle
## memory or script keeps working; they are aliases, not separate code paths.
controller-build-dev: controller-build
controller-push-dev: controller-push

# ---------------------------------------------------------------------------
# Cluster state, one primitive per object group and environment.
#
# `deploy` used to be one monolithic apply, so shipping a controller change also
# re-applied the gateway overlay. These split that apart: prod-controller touches
# only the controller, prod-gateway only the gateway.
# ---------------------------------------------------------------------------

## ConfigMaps both environments mount (the OICM sources list). Idempotent.
prod-config dev-config:
	$(KUBECTL) apply -f deploy/oicm/sources.yaml

## Roll the dev controller (manifest as pinned). Then apply the dev exclusion list.
dev-controller: dev-config
	$(call apply_controller,$(CONTROLLER_MANIFEST_DEV),$(CONTROLLER_DEPLOYMENT_DEV))
	$(KUBECTL) apply -f $(EXCLUSIONS_DEV)

## Roll the prod controller (manifest as pinned), then the prod exclusion list.
prod-controller: prod-config
	$(call apply_controller,$(CONTROLLER_MANIFEST_PROD),$(CONTROLLER_DEPLOYMENT_PROD))
	$(KUBECTL) apply -f $(EXCLUSIONS_PROD)

## Apply the dev gateway overlay. Does NOT restart the gateway, so an unchanged
## image keeps its running container.
dev-gateway:
	$(KUBECTL) apply -k $(OVERLAY_DEV)

## Apply the prod gateway overlay plus its ServiceMonitor. Does NOT restart the
## gateway; use litellm-src-deploy-prod when the image should move too.
prod-gateway:
	$(KUBECTL) apply -k $(OVERLAY_PROD)
	$(KUBECTL) apply -f $(SERVICEMONITOR_PROD)

## Everything prod: config, controller, gateway overlay, ServiceMonitor.
deploy: prod-controller prod-gateway

## Everything dev: config, controller, gateway overlay, exclusion list.
deploy-dev: dev-controller dev-gateway

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

## Delete regenerable build artifacts. These are all git-ignored and total
## several hundred MB, mostly the client-distribution mirror staging. Nothing
## here is needed to build, deploy, or run; each is recreated on demand by the
## command named beside it. Leaves the venvs alone, since litellm-local-run
## uses .venv.
clean-artifacts:
	@echo "Removing regenerable build artifacts"
	rm -rf examples/client-distribution/.staging   # examples/client-distribution/sync-mirror.sh
	rm -rf site .venv-docs                          # make docs
	rm -rf .mypy_cache                              # type checker
	@echo "Remaining on disk:"; du -sh . 2>/dev/null

## Build the LiteLLM source image (with the enterprise dev override) from the
## upstream Dockerfile at the repo root. Tag is the sanitized git branch name,
## e.g. jya0-v1.89.3. Override with: make litellm-src-build LITELLM_SRC_TAG=foo
## SOURCE_DATE_EPOCH pins every layer tar's file mtimes to one value, so a
## rebuild after a one-line change re-digests ONLY the layers whose content
## actually changed. Without it, uv's reinstall + COPY parent dirs stamp fresh
## mtimes into unchanged layers and the registry push re-uploads ~700MB.
## Any fixed value works; the pin is what matters, not the date.
SOURCE_DATE_EPOCH ?= 1700000000

litellm-src-build:
	@echo "Building $(LITELLM_SRC_HARBOR_IMG):$(LITELLM_SRC_TAG)"
	podman build \
		$(LITELLM_SRC_BUILD_ARGS) \
		--timestamp $(SOURCE_DATE_EPOCH) \
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

## Point the gateway deployment at $(LITELLM_SRC_TAG), no cluster access.
## The image lives in deploy/base/gateway/proxy/deployment.yaml, which BOTH
## overlays build on, so this repins dev and prod together.
litellm-src-pin:
	@echo "Pinning $(LITELLM_SRC_HARBOR_IMG):$(LITELLM_SRC_TAG) in $(GATEWAY_MANIFEST)"
	$(call pin_image,$(LITELLM_SRC_HARBOR_IMG),$(LITELLM_SRC_TAG),$(GATEWAY_MANIFEST))

## Apply all prod manifests, then move the prod gateway to the pinned image.
## Build first with litellm-src-build-push (or use litellm-src-release).
litellm-src-deploy: litellm-src-pin prod-gateway
	@echo "Restarting $(GATEWAY_DEPLOYMENT_PROD) to pull the freshly tagged image"
	$(call rollout,$(GATEWAY_DEPLOYMENT_PROD))
	@echo "Deployed $(LITELLM_SRC_HARBOR_IMG):$(LITELLM_SRC_TAG)"

## Build, pin, and roll the prod gateway out.
## e.g. make litellm-src-release  (on branch jya0-v1.95.0 -> tag jya0-v1.95.0)
litellm-src-release: litellm-src-build-push litellm-src-deploy

## Apply the dev gateway overlay, then move it to the pinned image.
litellm-src-deploy-dev: litellm-src-pin dev-gateway
	@echo "Restarting $(GATEWAY_DEPLOYMENT_DEV) to pull the freshly tagged image"
	$(call rollout,$(GATEWAY_DEPLOYMENT_DEV))
	@echo "Deployed $(LITELLM_SRC_HARBOR_IMG):$(LITELLM_SRC_TAG) to dev. Prod overlay untouched."

## Build, push, then roll the DEV gateway out. The dev counterpart of
## `litellm-src-release`, and the ONLY safe way to ship the gateway to dev.
##
## CAVEAT: dev and prod reference the SAME tag, so a build overwrites the bytes
## behind it for both. This target only restarts the dev Deployment, so prod
## keeps serving its already-running container until prod is deliberately
## restarted. Treat the tag as a dev channel until prod is moved to its own tag.
litellm-src-release-dev: litellm-src-build-push litellm-src-deploy-dev

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

## The self-contained edge image (proxy plus bundled Postgres) lives in
## edge-litellm/, which owns its own Dockerfile, build, run, and Harbor publish.
##   make -C edge-litellm help

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

# ---------------------------------------------------------------------------
# Smoke tests: live post-merge behavior checks against a DEPLOYED gateway.
#
# Run after a merge to confirm documented behaviors survived. Point at a target
# with BASE/KEY. Defaults to the dev gateway port-forward:
#   kubectl -n adeo-litellm port-forward deploy/litellm-proxy-dev 14001:4000 &
#   make smoke-fast
# Prod:  make smoke-fast BASE=https://litellm.ecouncil.ae
#
# Tier A (contract) needs only the master key (auto-derived from the manifest).
# Tier B (model_contract) and the scoped-key auth tests need a scoped key:
#   export KEY=sk-...
#
# `smoke-leaks-mutate` is the only target that writes live router state. It
# restores what it changes, but it is excluded from `smoke` and `smoke-leaks`
# on purpose: never point it at prod without reading the test first.
# ---------------------------------------------------------------------------
SMOKE_PYTEST := $(CURDIR)/.venv/bin/python -m pytest tests/smoke -p no:cacheprovider -q

smoke-fast:
	BASE=$(BASE) MASTER_KEY=$(MASTER_KEY) $(SMOKE_PYTEST) -m "contract and not mutating"

smoke-models:
	BASE=$(BASE) MASTER_KEY=$(MASTER_KEY) $(SMOKE_PYTEST) -m "(contract or model_contract) and not mutating"

smoke-leaks:
	BASE=$(BASE) MASTER_KEY=$(MASTER_KEY) $(SMOKE_PYTEST) -m "soak and not mutating"

smoke-leaks-mutate:
	BASE=$(BASE) MASTER_KEY=$(MASTER_KEY) $(SMOKE_PYTEST) -m "soak and mutating"

smoke:
	BASE=$(BASE) MASTER_KEY=$(MASTER_KEY) $(SMOKE_PYTEST) -m "not mutating"
