# Postgres Inside the LiteLLM Image

A self-contained image that runs the LiteLLM proxy and a Postgres it owns in a single container. Intended for demos, CI, and MCP experimentation where requiring an external database defeats the purpose. Published to `zdc-ai-harbor.ecouncil.ae/adeo-gpt-agent/adeo-litellm-edge`

For anything running in the cluster, use the managed Postgres instead. See [Why not in the cluster](#why-not-in-the-cluster)

## Why Postgres and not SQLite

SQLite is not an option. Three independent guards reject it: `schema.prisma` pins `provider = "postgresql"`, `litellm/proxy/db/db_url_settings.py` restricts `SUPPORTED_DB_SCHEMES` to `postgresql` and `postgres`, and `proxy_cli.py` exits on an unsupported scheme. The schema also uses 68 `String[]` fields that Prisma cannot express on SQLite, and the query layer depends on `jsonb`, `timestamptz`, `ON CONFLICT`, and `ANY(` in 87+ raw SQL sites

## Build

Postgres is layered on top of the already-built proxy image, so that image must exist first

```
make -C .. litellm-src-build
make build
```

`Dockerfile` takes the base from `BASE_IMAGE`, which defaults to the `litellm-src` image at the current branch tag. Nothing is rebuilt from source, so the venv, Prisma engines, admin UI, and `docker/prod_entrypoint.sh` are inherited unchanged

## Run

```
make run
```

Or straight from the registry, with no flags at all. The image defaults `POSTGRES_PASSWORD`, `LITELLM_MASTER_KEY`, and the UI login to the same value, so a fresh host needs nothing but the pull:

```
docker pull zdc-ai-harbor.ecouncil.ae/adeo-gpt-agent/adeo-litellm-edge:<tag>
docker run -d --name adeo-litellm-edge -p 4000:4000 \
  -v adeo_litellm_edge_data:/var/lib/postgresql/data \
  zdc-ai-harbor.ecouncil.ae/adeo-gpt-agent/adeo-litellm-edge:<tag>
```

`docker` has no `--tls-verify` flag; that is podman's. Docker reads trusted CAs from `/etc/docker/certs.d/<registry>/ca.crt`, so a host that trusts the internal CA pulls with no flags. For the full deployment walkthrough, including the failures that are easy to hit, see [EDGE-VM-DEPLOY.md](EDGE-VM-DEPLOY.md)

The UI is at http://localhost:4000/ui/ (user `ai-admin`) and the API at http://localhost:4000/v1/models. The master key, the UI password, and the Postgres password are all `P@$$w0rd-13579` unless overridden. Data lives in the `adeo_litellm_edge_data` volume and survives restarts

First boot takes about a minute because Prisma applies the schema. Errors mentioning `relation "LiteLLM_VerificationTokenView" does not exist` and `_prisma_migrations` during that window are the proxy probing before Prisma finishes and are expected

Open a psql session against the running container with `make shell`

### The password must not be passed with -e

`P@$$w0rd-13579` contains a literal `$$` and an `@`. A shell expands `$$` to its own process id, and the entrypoint percent-encodes the password before putting it in `DATABASE_URL` because an unencoded `@` ends the URL's userinfo section and the driver then looks for a host named `$$w0rd@127.0.0.1`. Use the image default, or `--env-file`, never `-e PASSWORD='...'`

## Mirroring production's models

The image ships `configure_edge_models.sh` at `/usr/local/bin/`, so a running container can be seeded with every model production serves. Credentials are baked in, so no arguments are needed:

```
make mirror
podman exec -it adeo-litellm-edge /usr/local/bin/configure_edge_models.sh
```

Both endpoints are read because neither is complete on its own. `/api/v1/models` is the OpenRouter-compatible catalogue and the only source of `context_length` and pricing. `/model/info` is the only source of `mode`, which is load-bearing: an embedding or TTS deployment registered as `chat` will not route

Registration is idempotent on `model_name`, because `/model/new` mints a fresh row per call and a rerun would otherwise double every deployment for the router to load-balance across. Pass `DRY_RUN=1` to preview and `ONLY=substr` to copy a subset

## Publishing to Harbor

```
make release
```

That builds, tags, and pushes to `zdc-ai-harbor.ecouncil.ae/adeo-gpt-agent/adeo-litellm-edge:<branch-tag>`. Pass `TAG=1.106.1` to publish under a version of your choosing. `make push` does the tag and push without rebuilding, and `make tags`, `make inspect`, `make delete` manage what is already published

The registry is served from an internal CA (`EC-ISSUINGCA`) that is not in the system trust store on this build host, so every podman call here passes `--tls-verify=false`. To drop that flag, install the CA at `/etc/containers/certs.d/zdc-ai-harbor.ecouncil.ae/ca.crt` and set `TLS_VERIFY=true`. Pulling with docker needs no equivalent flag: docker reads `/etc/docker/certs.d/<registry>/ca.crt`, and the edge VM already trusts the CA

```
docker pull zdc-ai-harbor.ecouncil.ae/adeo-gpt-agent/adeo-litellm-edge:<tag>
```

## How it works

`scripts/pg-entrypoint.sh` replaces the base image's entrypoint and does four things in order: initialise the cluster if `PGDATA` is empty, start Postgres in the foreground, wait for `pg_isready`, create the database if missing, then exec the original `docker/prod_entrypoint.sh` with the container's arguments
The ordering is not cosmetic. `litellm/proxy/db/prisma_client.py` runs `prisma db push` or `prisma migrate deploy` during startup with only four attempts and `random.randrange(5, 15)` second sleeps between them. If Postgres is not accepting connections when the proxy starts, those retries are spent and the proxy comes up without a database

Postgres runs as an unprivileged `postgres` user via `gosu`, because both `initdb` and `postgres` refuse to run as root. The container itself stays root, matching the base image

### Why postgres runs in the foreground

The obvious implementation uses `pg_ctl start`, which detaches Postgres from the supervisor. On `docker stop` the signal then reaches only the shell, Postgres is never asked to shut down, and `postmaster.pid` survives in the volume. The next boot fails with `pg_ctl: another server might be running` and then `the database system is starting up`. Running `postgres` directly and trapping `TERM` lets a stop reach the server, so it removes the pid file and exits cleanly

### Credentials

The image defaults `POSTGRES_PASSWORD` and `LITELLM_MASTER_KEY` to `P@$$w0rd-13579`, the same value as the UI login, so a pull is runnable with no flags. That is a deliberate trade for a demo image and it means the credential is readable by anyone who can pull the image. Override both with `--env-file` for anything else. The entrypoint percent-encodes the password before building `DATABASE_URL`, and the URL is exported rather than printed, because container logs are routinely collected. Set `DATABASE_URL` yourself to point the proxy at a real database instead


Run-time values come from `.env.edge` in this directory, passed with `--env-file`. That file is gitignored and must exist on the build host, because `Dockerfile` copies it to `/etc/litellm-edge/credentials.env` for the mirror script. It is not tracked because `PROD_API_KEY` is a live production virtual key and this repository is public

Two consumers read it and neither expands it. `podman --env-file` passes values through byte for byte, and the mirror script parses `key=value` rather than sourcing the file. This matters because the UI password contains a literal `$$`: make turns it into `P@-13579` when it includes a `.env`, and a shell turns it into `P@<pid>w0rd-13579`. Both forms fail to authenticate, so the value must never pass through either

## Why not in the cluster

This image breaks the invariant that the gateway holds no state of its own. A single container with an embedded database is a reasonable fit for a self-contained test image and a poor fit for a deployment: restarts, backups, PVCs, and resource limits all become harder, and one liveness probe and one memory limit have to cover two processes with very different failure modes

For the cluster, keep the database external. The proxy accepts any Postgres-wire database, so a managed Postgres or a compatible engine such as YugabyteDB works without code changes

## Known limitation

The image inherits whatever `openssl` the base image pinned. If the Wolfi repository has since moved to a new `openssl` major version, `apk add postgresql-16` fails with `trying to overwrite etc/ssl/openssl.cnf owned by openssl-3.6.4-r4`. This only affects a stale base image; rebuilding `litellm-src` picks up a consistent set. If you hit it on an old base, `apk add --no-cache --upgrade openssl` before installing Postgres
