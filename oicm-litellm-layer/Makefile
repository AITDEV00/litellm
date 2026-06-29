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

.PHONY: all pull-discovery pull-litellm pull build push-discovery push-litellm push login deploy clean \
	litellm-src-build litellm-src-push litellm-src-build-push

all: pull build push

## Pull base/dependency images
pull: pull-discovery pull-litellm

pull-discovery:
	podman pull python:3.12-slim

pull-litellm:
	podman pull $(LITELLM_IMG)

## Build the discovery controller image
build:
	podman build -t $(DISCOVERY_IMG):$(TAG) -f controller/Dockerfile .

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

## Deploy both manifests to the cluster
deploy:
	KUBECONFIG=$${KUBECONFIG:-$$HOME/.kube/oicm-local.conf} kubectl apply -f deploy/discovery-controller.yaml
	KUBECONFIG=$${KUBECONFIG:-$$HOME/.kube/oicm-local.conf} kubectl apply -f deploy/litellm-proxy.yaml

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

## Create the litellm-logo ConfigMap from the optimized logo and favicon files
litellm-logo:
	@echo "Creating litellm-logo ConfigMap"
	KUBECONFIG=$${KUBECONFIG:-$$HOME/.kube/oicm-alain.conf} \
		kubectl create configmap litellm-logo -n mlops \
		--from-file=adeo-ai-gateway.jpg=decor/adeo-ai-gateway.jpg \
		--from-file=favicon.png=decor/favicon.png \
		--dry-run=client -o yaml | \
		KUBECONFIG=$${KUBECONFIG:-$$HOME/.kube/oicm-alain.conf} kubectl apply -f -
