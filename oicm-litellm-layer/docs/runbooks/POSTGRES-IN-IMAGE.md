# Postgres Inside the LiteLLM Image

A self-contained image that runs the LiteLLM proxy and a Postgres it owns in a single container. Intended for demos, CI, and MCP experimentation where requiring an external database defeats the purpose

For anything running in the cluster, use the managed Postgres instead. See [Why not in the cluster](#why-not-in-the-cluster)

## Why Postgres and not SQLite

SQLite is not an option. Three independent guards reject it: `schema.prisma` pins `provider = "postgresql"`, `litellm/proxy/db/db_url_settings.py` restricts `SUPPORTED_DB_SCHEMES` to `postgresql` and `postgres`, and `proxy_cli.py` exits on an unsupported scheme. The schema also uses 68 `String[]` fields that Prisma cannot express on SQLite, and the query layer depends on `jsonb`, `timestamptz`, `ON CONFLICT`, and `ANY(` in 87+ raw SQL sites

## Build

Postgres is layered on top of the already-built proxy image, so that image must exist first

```
make litellm-src-build
make litellm-pg-build
```

`Dockerfile.postgres` takes the base from `LITELLM_PG_BASE_IMAGE`, which defaults to the `litellm-src` image at the current `LITELLM_SRC_TAG`. Nothing is rebuilt from source, so the venv, Prisma engines, admin UI, and `docker/prod_entrypoint.sh` are inherited unchanged

## Run

```
make litellm-pg-run POSTGRES_PASSWORD=devpassword
```

The UI is at http://localhost:4000/ui/ and the API at http://localhost:4000/v1/models. Data lives in the `litellm_pg_data` volume and survives restarts

Open a psql session against the running container with `make litellm-pg-shell`

## How it works

`scripts/pg-entrypoint.sh` replaces the base image's entrypoint and does four things in order: initialise the cluster if `PGDATA` is empty, start Postgres in the foreground, wait for `pg_isready`, create the database if missing, then exec the original `docker/prod_entrypoint.sh` with the container's arguments

The ordering is not cosmetic. `litellm/proxy/db/prisma_client.py` runs `prisma db push` or `prisma migrate deploy` during startup with only four attempts and `random.randrange(5, 15)` second sleeps between them. If Postgres is not accepting connections when the proxy starts, those retries are spent and the proxy comes up without a database

Postgres runs as an unprivileged `postgres` user via `gosu`, because both `initdb` and `postgres` refuse to run as root. The container itself stays root, matching the base image

### Why postgres runs in the foreground

The obvious implementation uses `pg_ctl start`, which detaches Postgres from the supervisor. On `docker stop` the signal then reaches only the shell, Postgres is never asked to shut down, and `postmaster.pid` survives in the volume. The next boot fails with `pg_ctl: another server might be running` and then `the database system is starting up`. Running `postgres` directly and trapping `TERM` lets a stop reach the server, so it removes the pid file and exits cleanly

### Credentials

`POSTGRES_PASSWORD` has no default and is not baked into any layer. A credential in an image layer is readable by anyone who can pull the image. The entrypoint refuses to start without it. The generated `DATABASE_URL` is exported rather than printed, because container logs are routinely collected. Set `DATABASE_URL` yourself to point the proxy at a real database instead

## Why not in the cluster

This image breaks the invariant that the gateway holds no state of its own. A single container with an embedded database is a reasonable fit for a self-contained test image and a poor fit for a deployment: restarts, backups, PVCs, and resource limits all become harder, and one liveness probe and one memory limit have to cover two processes with very different failure modes

For the cluster, keep the database external. The proxy accepts any Postgres-wire database, so a managed Postgres or a compatible engine such as YugabyteDB works without code changes

## Known limitation

The image inherits whatever `openssl` the base image pinned. If the Wolfi repository has since moved to a new `openssl` major version, `apk add postgresql-16` fails with `trying to overwrite etc/ssl/openssl.cnf owned by openssl-3.6.4-r4`. This only affects a stale base image; rebuilding `litellm-src` picks up a consistent set. If you hit it on an old base, `apk add --no-cache --upgrade openssl` before installing Postgres
