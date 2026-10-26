# The self-contained LiteLLM edge image: the proxy plus a Postgres it owns, in
# one container, plus the tooling to publish it.
#
#   make build            build the image
#   make release          build, tag and push to Harbor
#   make help             everything else
#
# Published as:
#   zdc-ai-harbor.ecouncil.ae/adeo-gpt-agent/adeo-litellm-edge:<tag>
#
# The registry is served from an internal CA (EC-ISSUINGCA) that is not in the
# system trust store, so podman calls pass --tls-verify=false by default. Set
# TLS_VERIFY=true once the CA is installed under /etc/containers/certs.d/.
#
# Credentials live in .env.edge, which is untracked and gitignored. Values are
# read with grep rather than `include`, because make collapses a literal $$ in
# an included file and one of the values in there contains one.

REGISTRY ?= zdc-ai-harbor.ecouncil.ae
PROJECT  ?= adeo-gpt-agent
REPO     ?= adeo-litellm-edge

IMAGE ?= adeo-litellm-edge

# The base carries the venv, Prisma engines, admin UI, and entrypoint. Built by
# oicm-litellm-layer/Makefile, target litellm-src-build.
BASE_REGISTRY ?= registry.adeoaiengine.ecouncil.ae
BASE_PATH     ?= openinnovationai/platform/mlops/mlops-serving/litellm-src

# Defaults to the branch name of the litellm checkout, slashes replaced so it
# stays a valid tag. This directory is nested two levels below the repo root.
# Override for a release: make release TAG=1.106.1
TAG ?= $(shell git -C ../.. rev-parse --abbrev-ref HEAD 2>/dev/null | tr '/' '_')

BASE_IMAGE ?= $(BASE_REGISTRY)/$(BASE_PATH):$(TAG)
REMOTE_IMG ?= $(REGISTRY)/$(PROJECT)/$(REPO)
LOCAL_IMG  ?= $(IMAGE):$(TAG)

TLS_VERIFY ?= false
TLS_FLAG   := --tls-verify=$(TLS_VERIFY)

# The registry serves HTTPS only, so curl needs the scheme spelled out. Without
# it curl assumes http and gets no connection at all, not a redirect.
REGISTRY_URL := https://$(REGISTRY)

ENV_FILE ?= .env.edge
PORT     ?= 4000
VOLUME   ?= adeo_litellm_edge_data

HARBOR_USER     := $(shell grep -m1 '^HARBOR_USER=' $(ENV_FILE) 2>/dev/null | cut -d= -f2-)
HARBOR_PASSWORD := $(shell grep -m1 '^HARBOR_PASSWORD=' $(ENV_FILE) 2>/dev/null | cut -d= -f2-)
MASTER_KEY      := $(shell python3 ../scripts/get_master_key.py 2>/dev/null)

.DEFAULT_GOAL := help

.PHONY: help check-env check-base build run shell mirror stop clean login tag push pull release tags inspect delete

help:
	@echo "Edge LiteLLM image"
	@echo
	@echo "  Local"
	@echo "    make build      build $(LOCAL_IMG)"
	@echo "    make run        run it on port $(PORT), data in volume $(VOLUME)"
	@echo "    make shell      open a shell in the running container"
	@echo "    make mirror     copy production's model catalogue onto the running proxy"
	@echo "    make stop       stop the running container"
	@echo "    make clean      remove the local image"
	@echo
	@echo "  Harbor"
	@echo "    make release    build, tag and push to $(REMOTE_IMG):$(TAG)"
	@echo "    make push       tag and push an existing image, no rebuild"
	@echo "    make login      authenticate to $(REGISTRY)"
	@echo "    make pull       pull $(REMOTE_IMG):$(TAG) back"
	@echo "    make tags       list published tags"
	@echo "    make inspect    show a published tag's manifest"
	@echo "    make delete     remove TAG from the registry"
	@echo
	@echo "  Overrides: TAG=1.106.1  TLS_VERIFY=true  BASE_IMAGE=<ref>  ENV_FILE=<path>"
	@echo
	@echo "  Pull on the edge VM (docker needs no TLS flag; the CA is trusted there):"
	@echo "    sudo docker pull $(REMOTE_IMG):$(TAG)"

## Fail early with a clear message rather than a confusing error later.
check-env:
	@[ -f $(ENV_FILE) ] || { \
		echo "$(ENV_FILE) is missing. It holds the edge credentials the image bakes in,"; \
		echo "including PROD_API_KEY, plus HARBOR_USER and HARBOR_PASSWORD."; \
		exit 1; \
	}
	@[ -n "$(TAG)" ] || { echo "TAG resolved to empty; pass TAG=<version>"; exit 1; }

check-base:
	@podman image exists $(BASE_IMAGE) || { \
		echo "Base image $(BASE_IMAGE) not found."; \
		echo "Build it first: make -C .. litellm-src-build"; \
		echo "Or point at another: make build BASE_IMAGE=<ref>"; \
		exit 1; \
	}

build: check-env check-base
	@echo "Building $(LOCAL_IMG) from $(BASE_IMAGE)"
	podman build \
		--tag $(LOCAL_IMG) \
		--build-arg LITELLM_PG_BASE_IMAGE=$(BASE_IMAGE) \
		--file Dockerfile \
		.
	@echo "Built $(LOCAL_IMG)"

## Credentials come from .env.edge via --env-file, which passes values through
## byte for byte. Passing them with -e would let the shell expand the literal $$
## in the password, which produces a password that does not authenticate.
run: check-env
	@echo "Running $(LOCAL_IMG) on port $(PORT)"
	@echo "UI:  http://localhost:$(PORT)/ui/"
	@echo "API: http://localhost:$(PORT)/v1/models"
	podman run --rm -it \
		--name $(IMAGE) \
		-p $(PORT):4000 \
		-v $(VOLUME):/var/lib/postgresql/data \
		--env-file $(ENV_FILE) \
		$(LOCAL_IMG) \
		--config /app/config.yaml \
		--port 4000

shell:
	podman exec -it $(IMAGE) /bin/sh

## The script and its credentials ship in the image, so this needs no arguments.
mirror:
	podman exec -it $(IMAGE) /usr/local/bin/configure_edge_models.sh

stop:
	-podman stop $(IMAGE)

clean:
	-podman rmi $(LOCAL_IMG)

login: check-env
	@[ -n "$(HARBOR_USER)" ] || { echo "HARBOR_USER is empty in $(ENV_FILE)"; exit 1; }
	@[ -n "$(HARBOR_PASSWORD)" ] || { echo "HARBOR_PASSWORD is empty in $(ENV_FILE)"; exit 1; }
	@printf '%s' '$(HARBOR_PASSWORD)' | \
		podman login $(TLS_FLAG) $(REGISTRY) -u '$(HARBOR_USER)' --password-stdin

## Separate from push so a failed push can be retried without rebuilding.
tag:
	@podman image exists $(LOCAL_IMG) || { echo "$(LOCAL_IMG) not found. Run: make build"; exit 1; }
	podman tag $(LOCAL_IMG) $(REMOTE_IMG):$(TAG)

push: tag login
	podman push $(TLS_FLAG) $(REMOTE_IMG):$(TAG)

pull:
	podman pull $(TLS_FLAG) $(REMOTE_IMG):$(TAG)

release: build push
	@echo
	@echo "Published $(REMOTE_IMG):$(TAG)"
	@echo "Pull on the edge VM with: sudo docker pull $(REMOTE_IMG):$(TAG)"
	@echo "See docs/EDGE-VM-DEPLOY.md for the full walkthrough."

tags: check-env
	@curl -sk $(REGISTRY_URL)/v2/$(PROJECT)/$(REPO)/tags/list \
		-u '$(HARBOR_USER):$(HARBOR_PASSWORD)' | python3 -m json.tool

inspect: check-env
	@curl -sk $(REGISTRY_URL)/v2/$(PROJECT)/$(REPO)/manifests/$(TAG) \
		-u '$(HARBOR_USER):$(HARBOR_PASSWORD)' \
		-H 'Accept: application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json' \
		| python3 -m json.tool

## Removes TAG from the registry, leaving the repository in place.
delete: check-env
	@curl -sk -o /dev/null -w 'deleted $(TAG): HTTP %{http_code}\n' \
		-X DELETE $(REGISTRY_URL)/v2/$(PROJECT)/$(REPO)/manifests/$(TAG) \
		-u '$(HARBOR_USER):$(HARBOR_PASSWORD)' \
		-H 'Accept: application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json'
