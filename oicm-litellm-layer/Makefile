REGISTRY     := registry.adeoaiengine.ecouncil.ae
REPO_PATH    := openinnovationai/platform/mlops/mlops-serving
HARBOR_USER  := custom-images
HARBOR_PASS  := CustomImages123

DISCOVERY_IMG := $(REGISTRY)/$(REPO_PATH)/oicm-discovery-controller
LITELLM_IMG   := docker.litellm.ai/berriai/litellm:main-stable
LITELLM_HARBOR_IMG := $(REGISTRY)/$(REPO_PATH)/litellm
TAG           := latest

.PHONY: all pull-discovery pull-litellm pull build push-discovery push-litellm push login deploy clean

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
