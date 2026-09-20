# Credentials & Secrets

> **Read this before changing any password or API key.** The most dangerous
> change in this repo is a credential rotation, because the **same secret is
> consumed by multiple deployments**. Missing one consumer breaks model
> discovery or proxy auth silently.
>
> **⚠️ BEFORE ROTATING ANYTHING, READ THE "THE SALT KEY" SECTION BELOW. THE
> `LITELLM_SALT_KEY` ENCRYPTS ALL CREDENTIALS IN THE SHARED DATABASE. CHANGING
> IT WITHOUT A RE-ENCRYPTION MIGRATION BREAKS BOTH DEV AND PROD PERMANENTLY.
> DO NOT TOUCH IT.** ⚠️

## The master key — a shared secret

The proxy's admin key (the master key) is stored in **one Kubernetes Secret**
and is read by **two different Deployments**:

| Consumer | Deployment | Env var | Source |
|----------|------------|---------|--------|
| LiteLLM Proxy | `litellm-proxy` | `LITELLM_MASTER_KEY` | `secretKeyRef` → `litellm-master-key` / key `master-key` |
| Discovery Controller | `oicm-discovery-controller` | `LITELLM_ADMIN_KEY` | `secretKeyRef` → `litellm-master-key` / key `master-key` |

Both read the **same** `litellm-master-key` secret's `master-key` field, so
rotating the secret value affects both. You must restart **both** Deployments
after a rotation, because env vars are snapshotted at pod creation.

### Where the value is defined

| Location | Purpose |
|----------|---------|
| `deploy/prod/litellm-proxy.yaml` | Inline `Secret` `litellm-master-key` with `stringData.master-key` — **the single source of truth** |
| `deploy/prod/litellm-proxy.yaml` | Proxy container `LITELLM_MASTER_KEY` env (`secretKeyRef`) |
| `deploy/prod/discovery-controller.yaml` | Controller container `LITELLM_ADMIN_KEY` env (`secretKeyRef`) |
| `controller/config.py` | Local fallback: reads the manifest when `LITELLM_ADMIN_KEY` is unset (env still wins in-cluster) |
| `controller/README.md` | Documents the env var table |
| `config/local_dev.yaml` | `master_key: os.environ/LITELLM_MASTER_KEY` — reads from env (set by `make`) |
| `config/local_test_voice.yaml` | `master_key: os.environ/LITELLM_MASTER_KEY` — reads from env (set by `make`) |

### Everything derives from the one Secret

Rotating the value in `deploy/prod/litellm-proxy.yaml` and restarting both
Deployments covers all consumers:

- **Proxy** reads `LITELLM_MASTER_KEY` via `secretKeyRef` → `litellm-master-key`.
- **Discovery controller** reads `LITELLM_ADMIN_KEY` via `secretKeyRef` → the same Secret.
- **Docs** use `{{ master_key }}`, injected at build time by the MkDocs hook
  (`scripts/mkdocs_master_key.py`) straight from the manifest — no second copy.
- **Local tooling** (`make`, `config/*.yaml`, `.env.datasource`, benchmarks)
  derive the key from the manifest via `scripts/get_master_key.py` or the
  `os.environ/` mechanism.

So you change exactly **one** file (`deploy/prod/litellm-proxy.yaml`) and everything
else follows.

## Admin UI login

The LiteLLM Admin UI login is governed by `UI_USERNAME` / `UI_PASSWORD` env vars:

| Setting | Default | Notes |
|---------|---------|-------|
| `UI_USERNAME` | `admin` | Set via env if you want a non-default username |
| `UI_PASSWORD` | unset | **Falls back to the master key** if unset |

Logic lives in `litellm/proxy/auth/login_utils.py` → `get_ui_credentials()`.

- If `UI_PASSWORD` is unset, the UI password = the master key value.
- To decouple the UI password from the API master key, set `UI_PASSWORD`
  explicitly in `deploy/prod/litellm-proxy.yaml`.

## The Admin UI password vs. the API master key

| Concern | Env var | Where |
|---------|---------|-------|
| UI login password | `UI_PASSWORD` (falls back to master key) | `deploy/prod/litellm-proxy.yaml` container env |
| API `Authorization: Bearer` key | `LITELLM_MASTER_KEY` / `LITELLM_ADMIN_KEY` | Secret `litellm-master-key` |

If you want them the same value, set `UI_PASSWORD` to the same value as the
secret. If you want them independent, set `UI_PASSWORD` to something else.

## Rotation runbook (do this, not just change one file)

1. Edit the value in **`deploy/prod/litellm-proxy.yaml`** (the inline `Secret`) —
   this is the single place to change.
2. `kubectl apply -f deploy/prod/litellm-proxy.yaml` — this also updates the Secret.
3. **Restart BOTH Deployments** so pods re-resolve the secret:
   ```bash
   kubectl -n mlops rollout restart deployment/litellm-proxy
   kubectl -n mlops rollout restart deployment/oicm-discovery-controller
   ```
4. Wait for both rollouts and verify health:
   ```bash
   kubectl -n mlops rollout status deployment/litellm-proxy
   kubectl -n mlops rollout status deployment/oicm-discovery-controller
   ```
5. Verify the controller can still talk to the proxy (check controller logs for
   successful `/model/*` calls).
6. Rebuild the docs (`make docs` / `mkdocs build`) so the injected `{{ master_key }}`
   value reflects the new secret. Local `make` targets and benchmarks pick up the
   new value automatically from `deploy/prod/litellm-proxy.yaml`.

No other file needs a manual edit: docs, local configs, the controller fallback,
and benchmarks all derive from `deploy/prod/litellm-proxy.yaml`.

## ⚠️ THE SALT KEY — DO NOT TOUCH IT (READ BEFORE ROTATING ANYTHING) ⚠️

> **⚠️⚠️⚠️ THE `LITELLM_SALT_KEY` IS THE SINGLE MOST DANGEROUS SETTING IN THIS
> DEPLOYMENT. DO NOT CHANGE IT. DO NOT ROTATE IT. DO NOT DELETE IT. DO NOT
> "FIX" IT. LEAVE IT EXACTLY AS IT IS. ⚠️⚠️⚠️**

### WHAT THE SALT KEY DOES

The salt key (`LITELLM_SALT_KEY`) is the **at-rest credential encryption key**.
It is **NOT** the same as the master key (`LITELLM_MASTER_KEY`), and they must
**NEVER** be confused:

| Key | What it does | What happens if you change it |
|-----|--------------|-------------------------------|
| `LITELLM_MASTER_KEY` | **AUTHENTICATION** — the admin/root key used to log in and authorize API calls | Login stops working (401). Safe to rotate if you also restart consumers. |
| `LITELLM_SALT_KEY` | **ENCRYPTION** — the key used to encrypt/decrypt ALL sensitive credential values (API keys, provider secrets, virtual-key secrets) **stored at rest in the SHARED PostgreSQL database** | **EVERYTHING becomes undecryptable garbage. ALL MODELS FAIL TO LOAD. /v1/models returns 0 models. Both DEV AND PROD BREAK, because they share the same DB.** |

### ⚠️ DO NOT CHANGE THE SALT KEY — EVER — UNLESS YOU RUN THE FULL RE-ENCRYPTION MIGRATION

The salt key protects **every sensitive value written to the shared DB** via
`encrypt_value_helper` / `decrypt_value_helper`
(`litellm/proxy/common_utils/encrypt_decrypt_utils.py`). These include model
`api_key`s, provider secrets, and virtual-key secrets.

- **If `LITELLM_SALT_KEY` is unset**, LiteLLM falls back to the master key
  (`_get_salt_key()`). That is why the original DB was encrypted with `sk-1234`.
- **If you change the salt key without re-encrypting**, the stored values were
  encrypted under the OLD key. The new key cannot decrypt them. Result: every
  stored credential reads back as **garbage** (the classic failure is
  `LLM Provider NOT provided ... you passed model=<base64-garbage>`) and the
  gateway silently drops ALL models.
- **Because DEV AND PROD SHARE THE SAME DATABASE**, changing the salt key breaks
  **BOTH** environments at the same time, even if you only intended to change one.

### THE ONLY LEGAL WAY TO CHANGE THE SALT KEY

Changing the salt key is only safe if you FIRST re-encrypt every stored
credential under the new key, coordinated across dev and prod **simultaneously**:

1. Run LiteLLM's credential migration to re-encrypt all rows under the NEW key
   (see `litellm/proxy/management_endpoints/credential_migration.py`).
2. Verify every model decrypts and loads under the new key.
3. THEN update `LITELLM_SALT_KEY` on all pods (dev + prod) and restart.

If you are not doing this full migration, **DO NOT TOUCH THE SALT KEY.**

### WHY BOTH PROD AND DEV PIN THE SALT KEY EXPLICITLY

When the master key was changed from `sk-1234` to `sk-05132025` (on dev first,
then rolled out to prod), the proxies would have inherited the NEW key as their
salt (because salt falls back to master key). That would have made them unable
to decrypt the shared DB. **To prevent that, BOTH deployments pin
`LITELLM_SALT_KEY` to the ORIGINAL `sk-1234`** so DB decryption keeps working:

| Deployment | `LITELLM_MASTER_KEY` | `LITELLM_SALT_KEY` | Secret |
|------------|----------------------|--------------------|--------|
| Prod `litellm-proxy` | `sk-05132025` | **`sk-1234` (pinned, MUST stay)** | `litellm-master-key` + `litellm-salt-key` |
| Dev `litellm-proxy-dev` | `sk-05132025` | **`sk-1234` (pinned, MUST stay)** | `litellm-master-key-dev` + `litellm-salt-key-dev` |

> ⚠️ **RULE:** The salt value in `litellm-salt-key` / `litellm-salt-key-dev`
> MUST always equal the original encryption key (`sk-1234`). It MUST NOT be
> changed alongside the master key. If you rotate the master key again, do NOT
> rotate the salt key with it.

### CHECKLIST BEFORE TOUCHING ANY KEY

- [ ] Am I about to change `LITELLM_SALT_KEY`? → **STOP. Do not.** Re-read this section.
- [ ] Am I changing only `LITELLM_MASTER_KEY` for auth? → OK, but keep the salt
      key pointing at the original encryption key.
- [ ] Is a credential re-encryption migration queued for BOTH dev and prod? →
      Only then is a salt change allowed.

### ROLLBACK (in case a master-key change breaks prod)

If a master-key rotation to `sk-05132025` ever breaks prod, revert with the
dedicated rollback manifests in `deploy/rollback/`. They restore the master key
to `sk-1234` and re-point the salt key so DB decryption keeps working:

```bash
KUBECONFIG=$HOME/.kube/oicm-alain.conf \
  kubectl apply -f deploy/rollback/litellm-proxy-rollback-key.yaml
KUBECONFIG=$HOME/.kube/oicm-alain.conf \
  kubectl apply -f deploy/rollback/discovery-controller-rollback-key.yaml
KUBECONFIG=$HOME/.kube/oicm-alain.conf \
  kubectl rollout restart deployment/litellm-proxy -n mlops
KUBECONFIG=$HOME/.kube/oicm-alain.conf \
  kubectl rollout restart deployment/oicm-discovery-controller -n mlops
```

The rollback is idempotent and safe to apply at any time against the forward
state.

## Other secrets

| Secret | File | Consumed by |
|--------|------|-------------|
| `litellm-db-credentials` (DATABASE_URL) | `deploy/prod/litellm-proxy.yaml` | Proxy → Postgres |
| `litellm-redis-password` | `deploy/prod/litellm-proxy.yaml` | Proxy → Redis |

## Local / non-cluster configs that also hold a master key

| File | Value | Notes |
|------|-------|-------|
| `config/local_dev.yaml` | `os.environ/LITELLM_MASTER_KEY` | reads from env; `make` sets it from the manifest |
| `config/local_test_voice.yaml` | `os.environ/LITELLM_MASTER_KEY` | reads from env; `make` sets it from the manifest |
| `config/local_datasource.yaml` | `os.environ/LITELLM_MASTER_KEY` | reads from env |

All of these derive from `deploy/prod/litellm-proxy.yaml` via `scripts/get_master_key.py`,
so rotating the cluster secret automatically updates local runs too.