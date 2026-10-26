# Self-contained LiteLLM image: the proxy plus a Postgres it owns, in one
# container. Intended for demos, CI, and MCP experimentation where requiring an
# external database defeats the purpose. Not for the cluster; see
# docs/runbooks/POSTGRES-IN-IMAGE.md for why.
#
# Derives from the already-built proxy image so it inherits the venv, Prisma
# engines, admin UI, and entrypoint unchanged. The only additions are Postgres
# from the Wolfi repo the base already uses (glibc, matching the runtime) and a
# supervisor that starts Postgres before the proxy.
ARG LITELLM_PG_BASE_IMAGE

FROM ${LITELLM_PG_BASE_IMAGE}

USER root

# initdb and postgres both refuse to run as root, so the server runs as an
# unprivileged `postgres` user and gosu drops privileges for it.
RUN apk add --no-cache \
        postgresql-16 \
        postgresql-16-client \
        postgresql-16-contrib \
        gosu \
    && addgroup -S postgres \
    && adduser -S -G postgres -h /var/lib/postgresql postgres \
    && mkdir -p /var/lib/postgresql/data /var/run/postgresql \
    && chown -R postgres:postgres /var/lib/postgresql /var/run/postgresql \
    && chmod 700 /var/lib/postgresql/data

COPY scripts/pg-entrypoint.sh /usr/local/bin/pg-entrypoint.sh
RUN chmod +x /usr/local/bin/pg-entrypoint.sh

# Mirrors production's model list onto this instance. Shipped in the image so it
# can be run with `podman exec` after the container is up, with no host checkout
# and no mount.
COPY scripts/configure_edge_models.sh /usr/local/bin/configure_edge_models.sh
RUN chmod +x /usr/local/bin/configure_edge_models.sh

# Credentials the script above reads, so a plain `podman exec` needs no flags.
# .env.edge is untracked and gitignored because PROD_API_KEY is a live
# production virtual key and this repository is public, so the file must exist
# on the build host. Not read by LiteLLM itself: the proxy's own settings still
# come from environment variables at run time.
COPY .env.edge /etc/litellm-edge/credentials.env
RUN chmod 600 /etc/litellm-edge/credentials.env

# Baked in rather than mounted, so the image is self-contained. The parent image
# has no /app/assets, so these are the only files there. Regenerate with
# decor/generate-icons.py.
COPY decor/edge-logo.png /app/assets/edge-logo.png
COPY decor/edge-favicon.png /app/assets/edge-favicon.png

# Baked rather than mounted so the image is self-contained. disable_spend_logs
# lives here because it has no environment-variable equivalent.
COPY config/local_postgres.yaml /app/config.yaml

# POSTGRES_PASSWORD is deliberately not defaulted: a credential baked into a
# layer is readable by anyone who can pull the image. It is required at run time.
# The UI credentials below are the exception, because this variant is a local
# demo image and the login has to be knowable without reading the pod env.
#
# LITELLM_LOG is deliberately left unset so container logs stay at the upstream
# default. Silencing per-request rows is disable_spend_logs in
# config/local_postgres.yaml, which is a separate concern from stdout logging.
ENV PGDATA=/var/lib/postgresql/data \
    POSTGRES_USER=litellm \
    POSTGRES_DB=litellm \
    PGPORT=5432 \
    UI_LOGO_PATH=/app/assets/edge-logo.png \
    LITELLM_FAVICON_URL=/app/assets/edge-favicon.png \
    UI_USERNAME=ai-admin \
    UI_PASSWORD='P@$$w0rd-13579'

VOLUME ["/var/lib/postgresql/data"]

# Kept identical to the base image: its ENTRYPOINT is the relative path
# docker/prod_entrypoint.sh, which only resolves from /app.
WORKDIR /app

ENTRYPOINT ["/usr/local/bin/pg-entrypoint.sh"]
CMD ["--config", "/app/config.yaml", "--port", "4000"]
