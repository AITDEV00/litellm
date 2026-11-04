# syntax=docker/dockerfile:1.7

# Base image for building
ARG LITELLM_BUILD_IMAGE=cgr.dev/chainguard/wolfi-base@sha256:360896efb0753a5701c48f860e17130c205eae5a264797aa3e7ea126ee2923a7

# Runtime image
ARG LITELLM_RUNTIME_IMAGE=cgr.dev/chainguard/wolfi-base@sha256:360896efb0753a5701c48f860e17130c205eae5a264797aa3e7ea126ee2923a7
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.7@sha256:240fb85ab0f263ef12f492d8476aa3a2e4e1e333f7d67fbdd923d00a506a516a
# Pinned by digest like the other base images; bump explicitly on Node upgrades.
ARG UI_BUILD_IMAGE=node:24.19-alpine3.24@sha256:d32cdf619f63fe0471182d08996dd516c6275bb5fd31ae06e55a570bd9e1ad43
# Checksum from https://www.pgbouncer.org/downloads/ (the Wolfi repo only carries 1.24.x)
ARG PGBOUNCER_VERSION=1.26.0
ARG PGBOUNCER_SHA256=afd25dd61ee6775d37b40629b87ce08736b3e6955f3057bb212e410fbf21c71d

FROM $UV_IMAGE AS uvbin

# Admin UI builder. Pinned to the build platform so the architecture-independent
# Next.js static export compiles once natively even in a multi-arch build,
# instead of once per target arch under QEMU.
FROM --platform=$BUILDPLATFORM $UI_BUILD_IMAGE AS ui-builder

ENV NEXT_TELEMETRY_DISABLED=1 \
    npm_config_fund=false \
    npm_config_audit=false

WORKDIR /ui

COPY ui/litellm-dashboard/package.json ui/litellm-dashboard/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --prefer-offline

COPY ui/litellm-dashboard/ ./
RUN npm run build

# Builder stage
FROM $LITELLM_BUILD_IMAGE AS builder

WORKDIR /app
USER root

COPY --from=uvbin /uv /usr/local/bin/uv
COPY --from=uvbin /uvx /usr/local/bin/uvx

RUN apk add --no-cache \
    bash \
    gcc \
    python-3.13 \
    python-3.13-dev \
    rust \
    openssl \
    libssl3 \
    openssl-dev \
    nodejs \
    npm \
    libsndfile

ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    PATH="/app/.venv/bin:${PATH}"

# Copy dependency metadata first for layer caching
COPY pyproject.toml uv.lock ./
COPY enterprise/pyproject.toml enterprise/
COPY litellm-proxy-extras/pyproject.toml litellm-proxy-extras/

# Build the Rust extension before any Python source lands in the context
# layer: it depends only on litellm-rust/ + the toolchain files, so a Python
# code change never invalidates it. Cargo builds the same cdylib maturin
# would (same manifest, same pyo3 extension-module feature); the artifact
# is transplanted into the venv after the project sync below.
COPY rust-toolchain.toml .cargo/ ./.cargo/
COPY litellm-rust/ ./litellm-rust/
RUN --mount=type=cache,target=/root/.cargo \
    cargo build --release --manifest-path litellm-rust/crates/python-bridge/Cargo.toml \
        --features extension-module && \
    cp litellm-rust/target/release/lib_native.so /opt/_native.abi3.so

# Install third-party dependencies (cached unless pyproject.toml/uv.lock change).
# The uv cache is a cache mount so repeated builds don't re-download wheels when
# the lockfile changes; it persists across builds on the same host.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-install-workspace --no-default-groups --no-editable \
    --extra proxy \
    --extra proxy-runtime \
    --group admin-mcp \
    --extra extra_proxy \
    --extra semantic-router \
    --extra saml \
    --python python3.13

# Prisma client generation needs only schema.prisma, so it runs before the
# source COPY and stays cached across code changes. The prisma CLI + engines
# are baked under /opt/prisma, a fixed path every runtime uid can read and that
# no cache volume mount shadows; the paths are pinned via
# PRISMA_BINARY_CACHE_DIR / PRISMA_CLI_PATH and recorded into the generated
# client at build time, so `prisma migrate deploy` on a fresh database needs
# no npm and no network access (#33650, #24554). chmod happens here, in the
# builder, so the runtime stage stays copy-only: a metadata-only RUN layer
# re-ships its full byte span on every build even when nothing changed.
COPY schema.prisma ./schema.prisma
RUN HOME=/opt/prisma XDG_CACHE_HOME=/opt/prisma/.cache PRISMA_BINARY_CACHE_DIR=/opt/prisma/binaries \
    npm_config_cache=/root/.npm \
    prisma generate --schema=./schema.prisma && chmod -R a+rX /opt/prisma

# Copy full source tree
COPY . .

# Replace the committed UI bundle with the one built from this exact source.
# Clearing first drops the committed bundle's content-hashed chunks that COPY
# would otherwise leave behind alongside the fresh ones.
RUN rm -rf litellm/proxy/_experimental/out
COPY --from=ui-builder /ui/out/. litellm/proxy/_experimental/out/

# Build Admin UI before final sync (applies the enterprise color override when present)
RUN sed -i 's/\r$//' docker/build_admin_ui.sh && chmod +x docker/build_admin_ui.sh && ./docker/build_admin_ui.sh

# Install project and workspace packages (fast - deps already cached), then
# strip the parts of the workspace installs whose bytes change per build so
# the 700MB venv layer re-digests only when a dependency actually changes:
# - the workspace dist-infos: their uv_cache.json embeds a wall-clock build
#   timestamp and their RECORD hashes it, so they differ on every rebuild.
#   The module dirs themselves stay: the proxy resolves
#   `import litellm_enterprise` / `import litellm_proxy_extras` from
#   site-packages at runtime (proxy_server.py, prisma_migration.py).
# - litellm's own wheel copy: replaced by a .pth pointing at /app, where the
#   runtime stage ships the source tree as its own layer (the only layer a
#   code change re-uploads). A minimal dist-info survives so
#   `importlib.metadata.version("litellm")` (proxy_cli version echo,
#   litellm._version) still resolves; METADATA is content-stable because it
#   only changes when pyproject.toml changes, which re-builds this layer
#   anyway.
# The prebuilt release .so goes back in its module home; litellm then
# imports entirely from /app.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-default-groups --no-editable \
    --extra proxy \
    --extra proxy-runtime \
    --group admin-mcp \
    --extra extra_proxy \
    --extra semantic-router \
    --extra saml \
    --python python3.13 && \
    SP=/app/.venv/lib/python3.13/site-packages && \
    LDI=$(echo "$SP"/litellm-[0-9]*.dist-info) && \
    rm -rf "$SP/litellm" "$LDI"/RECORD "$LDI"/uv_build.json "$LDI"/uv_cache.json "$LDI"/sboms \
           "$SP/litellm_enterprise-"*.dist-info \
           "$SP/litellm_proxy_extras-"*.dist-info \
           "$SP"/litellm_enterprise*.pth "$SP"/litellm_proxy_extras*.pth && \
    mkdir -p "$SP/litellm/rust_bridge" && \
    cp /opt/_native.abi3.so "$SP/litellm/rust_bridge/_native.abi3.so" && \
    printf '/app\n' > "$SP/litellm.pth"

# Tornado's test suite rides inside its wheel. Deleting it here, in the
# builder, keeps the runtime stage copy-only.
RUN find /app/.venv -type f -path "*/tornado/test/*" -delete && \
    find /app/.venv -type d -path "*/tornado/test" -delete

RUN sed -i 's/\r$//' docker/entrypoint.sh && chmod +x docker/entrypoint.sh && \
    sed -i 's/\r$//' docker/prod_entrypoint.sh && chmod +x docker/prod_entrypoint.sh

FROM $LITELLM_BUILD_IMAGE AS liteadmin-builder
COPY --from=uvbin /uv /usr/local/bin/uv
RUN apk add --no-cache python-3.13 curl
# Download then verify rather than `ADD --checksum`, which buildah and podman
# reject ("ADD only supports the --chmod and --chown flag").
RUN curl -fsSL -o /tmp/liteadmin.tar.gz \
        https://github.com/BerriAI/litellm-admin-agent/archive/5661c805ce479a0cf11b842ea133cf8d22cd7e34.tar.gz && \
    echo "4fb1847e28e2be30ba76dac94f02c4903291326516a0f4ba772f6ffb8b25d05c  /tmp/liteadmin.tar.gz" | sha256sum -c -
RUN mkdir /tmp/liteadmin && tar xzf /tmp/liteadmin.tar.gz --strip-components=1 -C /tmp/liteadmin && \
    uv venv /opt/liteadmin --python python3.13 && \
    uv pip install --python /opt/liteadmin/bin/python --require-hashes -r /tmp/liteadmin/requirements.txt && \
    uv pip install --python /opt/liteadmin/bin/python --no-deps /tmp/liteadmin

# Runtime stage
FROM $LITELLM_RUNTIME_IMAGE AS runtime
ARG LITELLM_RELEASE_TAG=""
ENV LITELLM_RELEASE_TAG=${LITELLM_RELEASE_TAG}

USER root

# node (without npm) is required by the prisma CLI at runtime
RUN apk add --no-cache bash openssl libssl3 tzdata nodejs python-3.13 libsndfile

WORKDIR /app
ENV PATH="/app/.venv/bin:${PATH}" \
    PRISMA_BINARY_CACHE_DIR=/opt/prisma/binaries \
    PRISMA_CLI_PATH=/opt/prisma/binaries/node_modules/.bin/prisma \
    PRISMA_CLI_QUERY_ENGINE_TYPE=binary \
    PRISMA_OFFLINE_MODE=true

# Copy only what runtime needs. The venv holds dependencies plus an editable
# .pth pointer to /app; the application runs from this source tree, which is
# the ONLY layer a code change re-uploads. The rest of the builder's /app is
# build metadata that must not ship (manifest-scanning tools attribute
# everything in it to this image).
# entrypoint.sh invokes litellm/proxy/prisma_migration.py by source path.
COPY --from=builder /app/.venv /app/.venv
COPY --from=liteadmin-builder /opt/liteadmin /opt/liteadmin
COPY --from=builder /app/docker /app/docker
COPY --from=builder /app/schema.prisma /app/schema.prisma
COPY --from=builder /app/litellm /app/litellm
# .dockerignore excludes host-built .so files from the source COPY above (a
# build host's 890MB dev-profile artifact must never ride the context), so the
# prebuilt release .so from the builder lands next to the loader, its natural
# module home for `from litellm.rust_bridge import _native`.
COPY --from=builder /opt/_native.abi3.so /app/litellm/rust_bridge/_native.abi3.so
COPY --from=builder /app/litellm/proxy/prisma_migration.py /app/litellm/proxy/prisma_migration.py
# enterprise/ is imported by source path at runtime (proxy_cli puts the
# working directory on sys.path; litellm/proxy/hooks resolves
# enterprise.enterprise_hooks from it)
COPY --from=builder /app/enterprise /app/enterprise
COPY --from=builder /app/litellm-proxy-extras /app/litellm-proxy-extras

# Profiling tools for the memory-leak investigation (dev/profiler image only).
# memray = allocation-level profiler with native stacks; py-spy = sampling
# profiler attachable to a running process. Both are inert unless invoked.
# Placed AFTER the venv COPY so /app/.venv exists.
ARG INSTALL_PROFILERS=false
RUN if [ "$INSTALL_PROFILERS" = "true" ]; then \
        /app/.venv/bin/python -m ensurepip --upgrade 2>/dev/null || true; \
        /app/.venv/bin/python -m pip install --no-cache-dir memray py-spy; \
    fi
# Prisma CLI + engines are baked under /opt/prisma, a fixed path every
# runtime uid can read and that no cache volume mount shadows. The paths are
# pinned via PRISMA_BINARY_CACHE_DIR / PRISMA_CLI_PATH and recorded into the
# generated client at build time, so `prisma migrate deploy` on a fresh
# database needs no npm and no network access (#33650, #24554).
COPY --from=builder /opt/prisma /opt/prisma

# Smoke-verify the prisma pin (assertions only; cleanup and chmod already
# happened in the builder, so this RUN layer stays small and copy-stable).
RUN test -x /opt/prisma/binaries/node_modules/.bin/prisma && \
    test -f /opt/prisma/binaries/node_modules/prisma/build/index.js && \
    python -c "from prisma.client import BINARY_PATHS; paths = list(BINARY_PATHS.query_engine.values()); assert paths and all(p.startswith('/opt/prisma/') for p in paths), paths"

EXPOSE 4000/tcp

ENTRYPOINT ["docker/prod_entrypoint.sh"]
CMD ["--port", "4000"]
