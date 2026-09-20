TAG            ?= 0.24.0
SRC_REGISTRY   := quay.io/submariner
DST_REGISTRIES := registry.adeoaiengine.ecouncil.ae harbor.ai.ecouncil.ae

IMAGES := submariner-operator \
          submariner-gateway \
          submariner-route-agent \
          submariner-globalnet \
          lighthouse-agent \
          lighthouse-coredns \
          nettest \
          subctl

.PHONY: pull login tag push all verify

all: pull login tag push

pull:
	@for i in $(IMAGES); do \
		echo "== pulling $$i:$(TAG) =="; \
		podman pull $(SRC_REGISTRY)/$$i:$(TAG); \
	done

login:
	@for r in $(DST_REGISTRIES); do \
		echo "== logging in to $$r =="; \
		podman login --tls-verify=false $$r; \
	done

tag:
	@for r in $(DST_REGISTRIES); do \
		for i in $(IMAGES); do \
			echo "== tagging $$i:$(TAG) -> $$r/submariner/$$i:$(TAG) =="; \
			podman tag $(SRC_REGISTRY)/$$i:$(TAG) $$r/submariner/$$i:$(TAG); \
		done; \
	done

push:
	@for r in $(DST_REGISTRIES); do \
		for i in $(IMAGES); do \
			echo "== pushing $$r/submariner/$$i:$(TAG) =="; \
			podman push --tls-verify=false $$r/submariner/$$i:$(TAG); \
		done; \
	done

verify:
	@echo "== local images =="; \
	podman images | grep -E "submariner|lighthouse|nettest" | grep "$(TAG)"; \
	echo ""; \
	echo "== remote check =="; \
	for r in $(DST_REGISTRIES); do \
		echo "-- $$r --"; \
		for i in $(IMAGES); do \
			echo -n "  $$i: "; \
			podman manifest inspect --tls-verify=false $$r/submariner/$$i:$(TAG) >/dev/null 2>&1 \
				&& echo "present" || echo "MISSING"; \
		done; \
	done
