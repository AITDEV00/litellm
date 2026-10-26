# Deploying the Edge Image to the Edge VM

The exact commands that worked on the edge VM, plus the things that do not behave the way the container-side docs assume. Verified on 2026-10-26 against image config `sha256:b3f9ab4d`.

## Access

SSH to the edge VM as `ai-admin`. The host address and the login are in the team password store, not here: this repository is public, and an internal host plus a working credential is a foothold. The container login is `ai-admin` with the password in `.env.edge`.

## Pull

```
sudo docker pull zdc-ai-harbor.ecouncil.ae/adeo-gpt-agent/adeo-litellm-edge:jya0-v1.106.1
```

Three things about this line, each of which cost a retry:

- **`docker` has no `--tls-verify` flag.** That flag is podman's. Passing it to docker fails with `unknown flag: --tls-verify`. Docker reads trusted CAs from `/etc/docker/certs.d/<registry>/ca.crt`, and on this VM the internal CA is already trusted, so no flag is needed at all.
- **`sudo` is required.** `ai-admin` is not in the `docker` group, so a bare `docker pull` fails with `permission denied while trying to connect to the docker API at unix:///var/run/docker.sock`. Either use `sudo` on every command or add the user to the `docker` group.
- **Do not let the command wrap across lines when pasting.** A wrapped paste turned the tag into `jya0-v1.106.1: command not found` and pulled `...:jya` instead, which fails as `artifact adeo-gpt-agent/adeo-litellm-edge:jy not found`.

## Run

```
sudo docker run -d --name adeo-litellm-edge -p 4000:4000 \
  -v adeo_litellm_edge_data:/var/lib/postgresql/data \
  zdc-ai-harbor.ecouncil.ae/adeo-gpt-agent/adeo-litellm-edge:jya0-v1.106.1
```

No environment variables are needed. The image defaults `POSTGRES_PASSWORD`, `LITELLM_MASTER_KEY`, and the UI login to the same value.

First boot takes about a minute while Prisma applies the schema. During that window the logs contain `relation "LiteLLM_VerificationTokenView" does not exist` and `_prisma_migrations does not exist`. Those are the proxy probing before Prisma finishes and are expected. Confirm readiness rather than reading the log:

```
sudo docker exec adeo-litellm-edge python3 -c "import urllib.request; print(urllib.request.urlopen('http://localhost:4000/health/readiness', timeout=10).read().decode())"
```

That prints `{"status":"healthy","db":"connected"}` when the proxy is serving. The image has no `curl` and no `wget`, only `python3` and `busybox`, so a `wget -qO-` health check fails with `wget: not found` even though the proxy is fine.

## Mirror the model catalogue

```
sudo docker exec -it adeo-litellm-edge /usr/local/bin/configure_edge_models.sh
```

Reads the credentials baked into the image, so it needs no arguments. On a fresh database it reports `created=26 updated=0 skipped=0 failed=0` and `edge now serves 26 model(s)`. On a rerun it reports `created=0 updated=26`, because registration is idempotent on `model_name`.

## Verify

```
sudo docker exec adeo-litellm-edge python3 -c "import json,urllib.request; r=urllib.request.Request('http://localhost:4000/v1/models'); r.add_header('Authorization','Bearer P@\$\$w0rd-13579'); print(len(json.load(urllib.request.urlopen(r, timeout=15)).get('data',[])), 'models')"
```

Or from any host that can reach the VM:

```
curl -s -H 'Authorization: Bearer P@$$w0rd-13579' http://<edge-vm>:4000/v1/models
```

The UI is at `http://<edge-vm>:4000/ui/`, user `ai-admin`.

## Operational notes

**The tag is mutable.** `jya0-v1.106.1` is the git branch name, so every push to that branch overwrites the same tag. Three successive pushes during development all landed on `jya0-v1.106.1`, and two of them were pulled under the same tag minutes apart, which is why two different digests appeared. The tag does not identify a build. To pin one, pull by digest:

```
sudo docker pull zdc-ai-harbor.ecouncil.ae/adeo-gpt-agent/adeo-litellm-edge@sha256:<digest>
```

Get the digest from `docker inspect --format '{{index .RepoDigests 0}}' <image>`, or from Harbor. Publish a version tag with `make release TAG=1.106.1` when a build needs to be reproducible.

**Port 4000 is published on all interfaces.** `-p 4000:4000` binds `0.0.0.0`, so the proxy is reachable from anything that can route to the VM. Bind `127.0.0.1:4000:4000` instead if it should be local only.

**The image contains the production key.** `PROD_API_KEY` is baked in at `/etc/litellm-edge/credentials.env` so the mirror script needs no arguments. Anyone who can pull the image can read it and use the production gateway. Acceptable for a private demo image, not for anything wider.

## Restart from scratch

The Postgres data lives in the named volume, so the container can be replaced without losing registered models:

```
sudo docker rm -f adeo-litellm-edge
sudo docker run -d --name adeo-litellm-edge -p 4000:4000 \
  -v adeo_litellm_edge_data:/var/lib/postgresql/data \
  zdc-ai-harbor.ecouncil.ae/adeo-gpt-agent/adeo-litellm-edge:jya0-v1.106.1
```

To discard the database and start clean, remove the volume too:

```
sudo docker rm -f adeo-litellm-edge
sudo docker volume rm adeo_litellm_edge_data
```

The volume is not deleted by `docker rm`, which is deliberate: deleting a container should not silently discard registered models and spend data.
