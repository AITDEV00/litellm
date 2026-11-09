# OICM model-server registration and access-list maintenance

Verified against Al Ain OICM on 2026-11-09. This runbook covers OICM **model
server** registrations (runtime image templates), not model deployments or
LiteLLM's logical models.

The current SGLang registrations are:

- `0.5.22-cu130-amd64`
- `dev-dsv41-cu130-amd64`
- `glm-5.3-flash-cu129-amd64`
- `kimi-k31-pro-cu130-amd64`
- `qwen38flashnext-cu129-amd64`

Treat this as a dated snapshot. Re-list registrations before making changes
rather than assuming the list is current.

## Separate operations

| Operation | What changes | Tool |
|-----------|--------------|------|
| Register a runtime in OICM | `/api/v1/model_servers` | vendor `onboard.sh` |
| Remove a runtime registration | OICM model-server record | vendor `offboard.sh` |
| Change LiteLLM model permissions | org, team, and key `models` lists | `scripts/models/*.sh` |
| Remove an unavailable logical model | LiteLLM model registration | discovery controller's reconciliation |
| Modify a running model deployment | Kubernetes Deployment image/args/replicas | `devops-custom-models/deploy/` |

OICM model-server registration does not automatically create a model
deployment, and offboarding a model server does not delete an existing
deployment. Check for active consumers before offboarding.

## Vendor onboarding bundle

The surviving bundle is:

`/mnt/c/Users/jyao/Downloads/vllm-0.20.0/`

It contains `onboard.sh`, `offboard.sh`, `config.json`, `example.env`, and
`README.md`. The expected repo copy is `scripts/onboarding/vllm-0.20.0/`, which
is intentionally git-ignored because it contains credentials. It was absent
from this checkout. The Windows Downloads file timestamps do not prove bundle
contents are current. Inspect both before use.

- `onboard.sh --dry-run`: prints the payload without sending it
- `onboard.sh`: prompts `y/n`, then `POST /api/v1/model_servers`
- `offboard.sh --list`: lists model servers without deleting
- `offboard.sh --id <uuid>`: prompts before deleting that registration

`--dry-run` applies to onboarding only. `--id` selects an offboarding target;
it does not make deletion non-interactive. Use separate temporary `.env` files
or a lock to avoid concurrent runs sharing one bundle.

## Credential handling

Never put credentials into scripts, documentation, shell history, committed
`.env` files, terminal output, or PRs. Read them into variables inside a
process or create a temporary mode-`0600` `.env` and delete it in a `finally`
cleanup.

Restore the Al Ain tunnel using [Datasource local validation](DATASOURCE-LOCAL-VALIDATION.md#step-1-open-the-ssh-tunnel).
Run against `~/.kube/alain-oicm.conf`. The application-admin credentials are
in the `oicm-keycloak-initializer-config-secret` Secret, namespace
`oicm-keycloak`, key `config.yaml`. Select the `applications[]` item with
`admin_role_name: application_admin`. Do not use the `super_admin` entry.

Required shell values:

```dotenv
OICM_HOST=https://oicm.adeoaiengine.ecouncil.ae
AUTH_BASE_URL=https://auth-oicm.adeoaiengine.ecouncil.ae
APPLICATION_ADMIN_USERNAME=<from Secret>
APPLICATION_ADMIN_PASSWORD=<from Secret>
```

Every variable containing spaces or punctuation must be shell-quoted when
written into `.env`. For example, task types must be one single-quoted value.
Generate fields with Python `shlex.quote()` instead of hand-writing values.
The first test in the session wrote an unquoted task list. `source .env`
interpreted `Generation,Image-Text-to-Text,...` as a command. No model was
created by that failed run.

Use the `auth-oicm.` issuer, not `auth.adeoaiengine...`. The latter fails the
OICM userinfo round trip. See [write-access findings](../oicm-write-access-blocked.md).

Decode the Secret only inside a controlled process. The following command
prints secret material, so never run it in a terminal whose output is logged.
Supply the equivalent bytes directly to a credential-consumer process:

```bash
kubectl --kubeconfig="$HOME/.kube/alain-oicm.conf" \
  -n oicm-keycloak get secret oicm-keycloak-initializer-config-secret \
  -o jsonpath='{.data.config\.yaml}' | base64 -d
```

## Extract a canonical provider contract

The `config.json` shipped with a vLLM bundle contains vLLM arguments. Do not
reuse it for SGLang. Fetch the full record of the nearest already-registered
SGLang version, then reuse the metadata for another image tag:

- `task_types`, `supported_tasks_with_lora_adapters`
- `server_arguments`, `inference_arguments`, `tasks_base_url`
- `supported_auto_scaling_metrics`, `supported_models_architectures`
- `lora_config`, `lora_modules_separator`
- `health_check_endpoint`, `enable_metrics`,
  `multinode_inference_support`

Set `MODEL_SERVER_ARGS_JSON` to the new config filename. On 2026-11-09 this
was `sglang-config.json`, derived from the then-registered
`0.5.19-cu129-amd64`. It contained 69 server arguments, three task types,
and `/health` as health check. Since these predate the successful SGLang
batch, review current platform support rather than blindly cloning them.
Older runtime arguments are not automatically valid for a new server binary.
Where possible, compare with the target image's startup contract in
`oicm-custom-model-server-guide/examples/`.

The OICM detail response may be a direct object or wrapped in `data`. A client
should handle both. The model-servers summary is currently wrapped as
`{"data": [...]}`. Always handle pagination and errors explicitly; do not
accept script exit code alone as success. Both scripts print success or error
bodies, so inspect for the expected confirmation.

### Safe write procedure

1. **Read registrations**: `GET /api/v1/model_servers/summary`.
2. **Read exact image bindings**: for each relevant registration,
   `GET /api/v1/model_servers/<uuid>`. Inspect
   `devices.nvidia`; do not infer it from the display tag.
3. **Resolve Harbor tags**: authenticated Docker Registry request to
   `GET /v2/openinnovationai/platform/mlops/mlops-serving/<repository>/tags/list`.
   Store Harbor credentials in a process, not in command-line arguments or
   logs. The local Makefile contains repository credentials; that is an
   existing implementation detail, not a template for new docs or scripts.
4. **Defrost every ambiguous classification** before writing. Exact matching
   is the default. Do not treat different versions as aliases without image
   digest evidence. Do not assume a numeric string comparison is semantic
   version ordering.
5. **Build the payload** from a canonical provider contract. Change only
   `tag`, `devices`, and a freshly generated UUID for a related image. Retain
   independent task and multinode properties.
6. **Dry-run and inspect** the JSON, then run `onboard.sh` for exactly one
   intended new target. Verify the created row's full image reference before
   scaling up.
7. **For deletion**, first list consumers and resolve IDs again. Prefer a
   reviewable prerequisite report. `offboard.sh --id <uuid>` is the actual
   deletion path; the name is printed as `SGLang` for all versions, so ID and
   displayed version are the useful identifiers.

### 2026-11-09 mistake to avoid

The first bulk-onboarding pass incorrectly compared `0.5.9-nvidia` and
`0.5.11-nvidia` to `0.5.8` and `0.5.10.post1` as aliases with no digest
proof. Those two tags were left unregistered. Also, hand-transcribing UUIDs
caused failed deletions. Automate target discovery from one API response,
then consume that same snapshot after review. Never guess a UUID fragment or
use a placeholder for an administrative operation.

## LiteLLM model access-list maintenance

These scripts manage model permissions on the gateway. They do not register
OICM model servers. Set the key without printing it:

```bash
cd /path/to/oicm-litellm-layer
export PROXY_BASE_URL="https://litellm.adeoaiengine.ecouncil.ae"
export LITELLM_API_KEY="$(python3 scripts/get_master_key.py)"
```

Add a model wherever a model already granted exists:

```bash
scripts/models/add_model_if_has_refs.sh \
  "moonshotai/Kimi-K3" "moonshotai/Kimi-K3.1-Pro"
# Review, then:
scripts/models/add_model_if_has_refs.sh \
  "moonshotai/Kimi-K3" "moonshotai/Kimi-K3.1-Pro" --apply
```

The target must already be registered on the proxy. `TRIGGERS` and target
match exactly; it does not infer relationships between model families. Do not
assume the model name `Kimi-K3.1` or `Kimi-K3.1-Pro` exists without reading
`/model/info` first.

Dry-run dangling access-list cleanup:

```bash
scripts/models/remove_dangling_model_refs.sh
```

Apply only after reviewing every entry in the report, and restrict later edits
to one reference, not the full dangling-scan result:

```bash
scripts/models/remove_dangling_model_refs.sh --only keys
```

Both scripts currently over-fetch every key with `return_full_object=true`
and concatenate responses through command-line arguments. This exceeds the
argument-list limit on a production response: `/usr/bin/jq: Argument list too
long`. The `--only keys` form did not isolate that bug. A zero-result report
is therefore not proof of no qualifying keys; teams and orgs may have been
updated while keys were not. Use the [Linux `execve` argument limit](https://www.kernel.org/doc/html/latest/userspace-api/limits.html)
as background. Do not bypass this by widening interactive terminal input or
passing API-key collections on command lines.

For a manually verified fall-back, use secure temporary files or stdin
pipelines, not a giant argv. Add only the one exact model string to any list
that already contains the exact trigger string and lacks the target. Patch:

- `PATCH /organization/update`: `{organization_id, models}`
- `POST /team/update`: `{team_id, models}`
- `POST /key/update`: `{key, models}`

Preserve every other list entry. Re-read all pages after writing and check
that every trigger-holding list now contains the target. For targeted removal,
replace membership of the requested model only; other dangling IDs in the same
scan require their own review. Remove all temporary key-response files
afterwards.

## Verification checklist

- Re-list summary and exact details from OICM after every batch.
- Assert the complete expected set of remaining tags, not just successful writes.
- Check the image repository, tag, health check, task types, and enabled state.
- Confirm a new model can be deployed or called through OICM before deleting
  the previous generic fallback. Registration provisioning was tested here,
  not backwards-compatibility of every running deployment.
- Preserve unique version-specific serving patches by inspecting actual image
  bindings, not only the display version.
- Verify that the temporary `.env` and API-key files are gone.
- Do not commit an operational credential or a newly downloaded bundle.
