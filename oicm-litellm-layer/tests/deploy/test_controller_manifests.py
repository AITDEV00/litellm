"""The discovery controllers must not be able to reach each other's environment.

The dev controller and the prod controller are the same program pointed at
different gateways, so every difference between them is a safety property:
`LITELLM_ADMIN_URL` is the only thing that stops the dev controller writing to
the prod gateway, and `CONTROLLER_READ_ONLY` is the only thing that stops it
writing at all. Both live in the manifest as bare env values, so nothing but a
test catches a copy-paste that points dev at prod.

This file also guards the two objects the controllers share on purpose (the
`oicm-sources` ConfigMap and the ServiceAccount/RBAC), because a silent change to
either reaches production.

Run with the controller's test extra:

    cd oicm-litellm-layer && uv run --extra test --python 3.13 \
        python -m pytest tests/deploy/test_controller_manifests.py
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest
import yaml

_LAYER_ROOT: Final = Path(__file__).resolve().parents[2]
_MANIFESTS: Final = {
    "dev": _LAYER_ROOT / "deploy" / "dev" / "discovery-controller-dev.yaml",
    "prod": _LAYER_ROOT / "deploy" / "prod" / "discovery-controller.yaml",
}

# The gateway each controller must write to. Dev reaching the prod Service is the
# failure this file exists to prevent.
_EXPECTED_ADMIN_URL: Final = {
    "dev": "http://litellm-proxy-dev.adeo-litellm.svc.cluster.local:4000",
    "prod": "http://litellm-proxy.adeo-litellm.svc.cluster.local:4000",
}

# The Secret each controller must authenticate with. Dev holding the prod master
# key would let it write to prod even with the URL pointed correctly.
_EXPECTED_KEY_SECRET: Final = {"dev": "litellm-master-key-dev", "prod": "litellm-master-key"}


def _deployment(env: str) -> dict:
    docs = [d for d in yaml.safe_load_all(_MANIFESTS[env].read_text()) if isinstance(d, dict)]
    return next(d for d in docs if d["kind"] == "Deployment")


def _container(env: str) -> dict:
    return _deployment(env)["spec"]["template"]["spec"]["containers"][0]


def _env(env: str) -> dict[str, dict]:
    return {entry["name"]: entry for entry in _container(env).get("env", [])}


def _volumes(env: str) -> dict[str, dict]:
    return {v["name"]: v for v in _deployment(env)["spec"]["template"]["spec"]["volumes"]}


@pytest.mark.parametrize("env", sorted(_MANIFESTS))
def test_controller_writes_only_to_its_own_gateway(env: str):
    """The admin URL is the only thing keeping a controller out of the other env.

    It is a plain env value with no runtime check behind it, so a dev controller
    pointed at the prod Service would write prod's model table.
    """
    entry = _env(env).get("LITELLM_ADMIN_URL")
    assert entry is not None, f"{env}: LITELLM_ADMIN_URL must be set explicitly"
    assert entry.get("value") == _EXPECTED_ADMIN_URL[env], (
        f"{env}: LITELLM_ADMIN_URL must be {_EXPECTED_ADMIN_URL[env]!r}, got {entry.get('value')!r}"
    )


@pytest.mark.parametrize("env", sorted(_MANIFESTS))
def test_controller_uses_its_own_master_key(env: str):
    """Dev must not hold prod's master key, or the URL is the only barrier left."""
    ref = _env(env)["LITELLM_ADMIN_KEY"]["valueFrom"]["secretKeyRef"]
    assert ref["name"] == _EXPECTED_KEY_SECRET[env], (
        f"{env}: LITELLM_ADMIN_KEY must come from {_EXPECTED_KEY_SECRET[env]!r}, got {ref['name']!r}"
    )
    assert ref["key"] == "master-key", f"{env}: unexpected master-key Secret key {ref['key']!r}"


def test_dev_controller_never_references_the_prod_gateway():
    """No dev env value may name the prod Service.

    `litellm-proxy.` is a prefix of `litellm-proxy-dev.`, so this matches the prod
    hostname without matching the dev one.
    """
    prod_host = "litellm-proxy.adeo-litellm"
    offenders = {
        name: entry
        for name, entry in _env("dev").items()
        if prod_host in str(entry)
    }
    assert not offenders, f"dev controller env names the prod gateway: {offenders}"


def test_dev_controller_header_matches_its_read_write_setting():
    """The header must not claim a guarantee the manifest does not make.

    It used to say the controller was READ-ONLY and could never mutate a gateway,
    while `CONTROLLER_READ_ONLY` was `false`. A stale safety claim is worse than
    none, because it is what a reviewer trusts instead of reading the env.
    """
    text = _MANIFESTS["dev"].read_text()
    header = text.split("apiVersion:", 1)[0]
    read_only = _env("dev")["CONTROLLER_READ_ONLY"]["value"]

    assert read_only == "false", "this test pins the read-write case; update it if that changes"
    for phrase in ("is READ-ONLY", "NEVER mutates", "can never change the gateway"):
        assert phrase not in header, (
            f"header still claims {phrase!r} while CONTROLLER_READ_ONLY is {read_only!r}"
        )
    assert "READ-WRITE" in header, "header must state that the controller writes"


def test_dev_controller_reuses_the_prod_service_account():
    """Dev declares no ServiceAccount or RBAC, and uses prod's.

    The controller only reads the k8s API (deployments, services, configmaps,
    endpointslices), so prod's single read-only Role covers both instances. Pinned
    because a dev manifest that declared its own SA would need matching RBAC, and a
    gap there fails silently as a 403 on one source rather than at apply time.
    """
    dev_docs = [d for d in yaml.safe_load_all(_MANIFESTS["dev"].read_text()) if isinstance(d, dict)]
    dev_kinds = {d["kind"] for d in dev_docs}
    for kind in ("ServiceAccount", "Role", "RoleBinding"):
        assert kind not in dev_kinds, f"dev must not declare a {kind}; it uses prod's"

    prod_docs = [d for d in yaml.safe_load_all(_MANIFESTS["prod"].read_text()) if isinstance(d, dict)]
    prod_kinds = {d["kind"] for d in prod_docs}
    for kind in ("ServiceAccount", "Role", "RoleBinding"):
        assert kind in prod_kinds, f"prod must declare the {kind} both controllers use"

    prod_binding = next(d for d in prod_docs if d["kind"] == "RoleBinding")
    assert prod_binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "oicm-discovery-controller", "namespace": "adeo-litellm"}
    ], "prod's RoleBinding must grant the ServiceAccount dev also runs as"

    for env in _MANIFESTS:
        assert (
            _deployment(env)["spec"]["template"]["spec"]["serviceAccountName"]
            == "oicm-discovery-controller"
        ), f"{env}: must run as the shared ServiceAccount"


def test_sources_configmap_is_shared_and_documented_as_such():
    """Both controllers mount the same sources ConfigMap, so an edit reaches prod.

    Exclusions are split per environment; sources are not, because both
    controllers read the same OICM instances. The ConfigMap must say so, since
    it is the file someone would edit for a dev-only source experiment.
    """
    for env in _MANIFESTS:
        source = _volumes(env)["oicm-sources"]["configMap"]["name"]
        assert source == "oicm-sources", f"{env}: must mount the shared oicm-sources ConfigMap"

    text = (_LAYER_ROOT / "deploy" / "oicm" / "sources.yaml").read_text()
    assert "SHARED BY BOTH CONTROLLERS" in text, (
        "deploy/oicm/sources.yaml must state that both controllers mount it"
    )


def test_exclusions_are_split_per_environment():
    """Exclusions are the one OICM input that IS per-environment."""
    assert _volumes("prod")["oicm-exclusions"]["configMap"]["name"] == "oicm-exclusions"
    assert _volumes("dev")["oicm-exclusions"]["configMap"]["name"] == "oicm-exclusions-dev"


@pytest.mark.parametrize("env", sorted(_MANIFESTS))
def test_cluster_name_names_the_physical_cluster(env: str):
    """`CLUSTER_NAME` is the local-deployment default cluster, not the environment.

    `controller/config.py` requires it (a guessed value would be a wrong answer
    that looks authoritative), and `controller/models.py` uses it as the default
    `cluster` for a local deployment, persisted as `oicm_cluster`. A Submariner
    import takes the source's own `cluster` instead, so this value must name the
    cluster the pods run in, which is Al Ain for both environments.
    """
    assert _env(env)["CLUSTER_NAME"]["value"] == "alain", (
        f"{env}: CLUSTER_NAME must name the physical cluster, not the environment"
    )
    code = (_LAYER_ROOT / "controller" / "config.py").read_text()
    assert "CLUSTER_NAME is not set" in code, (
        "controller/config.py must keep requiring CLUSTER_NAME rather than defaulting it"
    )


def test_recovery_manifests_are_not_deployable():
    """The two disaster-recovery records must stay out of every deploy path.

    `restore-prod-postgres-from-snapshots.yaml` declares the live prod cluster's
    name, so applying it rebuilds prod from snapshots. It only fails today because
    its storage request is smaller than the live one; anything that tolerates a
    shrink would destroy prod data. Both files must stay under deploy/recovery and
    be named by no Makefile target.
    """
    prod_dir = _LAYER_ROOT / "deploy" / "prod"
    stray = sorted(p.name for p in prod_dir.glob("*recovery*")) + sorted(
        p.name for p in prod_dir.glob("*old-postgres*")
    )
    assert not stray, f"recovery records must not sit in deploy/prod: {stray}"

    recovery = _LAYER_ROOT / "deploy" / "recovery"
    records = sorted(p.name for p in recovery.glob("*.yaml"))
    assert records == [
        "bind-old-postgres-pvs.yaml",
        "restore-prod-postgres-from-snapshots.yaml",
    ], f"unexpected contents in deploy/recovery: {records}"

    makefile = (_LAYER_ROOT / "Makefile").read_text()
    for name in records:
        assert name not in makefile, f"Makefile must not apply deploy/recovery/{name}"
    assert "deploy/recovery" not in makefile, "the Makefile must never apply deploy/recovery"

    for name in records:
        assert "DO NOT APPLY" in (recovery / name).read_text(), (
            f"deploy/recovery/{name} must say it is not applied"
        )


def test_recovery_cluster_record_collides_with_the_live_cluster():
    """Pin the collision, so the record cannot be mistaken for a safe manifest.

    If the name ever stops colliding the record is harmless, and this test should
    be deleted rather than relaxed; until then the name is why it must not ship.
    """
    record = _LAYER_ROOT / "deploy" / "recovery" / "restore-prod-postgres-from-snapshots.yaml"
    docs = [d for d in yaml.safe_load_all(record.read_text()) if isinstance(d, dict)]
    cluster = next(d for d in docs if d["kind"] == "Cluster")
    assert cluster["metadata"]["name"] == "adeo-litellm-postgres"

    live = _LAYER_ROOT / "deploy" / "prod" / "litellm-postgres-cluster.yaml"
    live_docs = [d for d in yaml.safe_load_all(live.read_text()) if isinstance(d, dict)]
    live_cluster = next(d for d in live_docs if d["kind"] == "Cluster")
    assert cluster["metadata"]["name"] == live_cluster["metadata"]["name"], (
        "the record no longer collides with the live cluster; this guard can go"
    )


def test_secret_template_is_never_applied():
    """`service-account-secret.yaml` holds `REPLACE_ME` and must stay a template.

    It is the only description of the four Secrets both controllers read, so it
    must stay in the repo. Applying it would overwrite live credentials with a
    placeholder and break OICM authentication for both environments.
    """
    template = _LAYER_ROOT / "deploy" / "oicm" / "service-account-secret.yaml"
    docs = [d for d in yaml.safe_load_all(template.read_text()) if isinstance(d, dict)]
    secrets = [d for d in docs if d["kind"] == "Secret"]
    assert len(secrets) == 4, f"expected the four OICM Secrets, got {len(secrets)}"

    for secret in secrets:
        for key, value in (secret.get("stringData") or {}).items():
            if "password" in key:
                assert value == "REPLACE_ME", (
                    f"{secret['metadata']['name']}/{key} is not a placeholder: {value!r}"
                )

    text = template.read_text()
    assert "TEMPLATE, NEVER APPLIED" in text, "the file must say it is not applied"
    assert "REPLACE_ME" in text, "the file must show the placeholder shape"
    assert "make_service_account_secrets.sh" in text, (
        "the file must name the script that is the real writer"
    )

    makefile = (_LAYER_ROOT / "Makefile").read_text()
    assert "service-account-secret.yaml" not in makefile, (
        "the Makefile must never apply the secret template"
    )
    assert not re.search(r"apply -f deploy/oicm/\s*$", makefile, re.MULTILINE), (
        "no Makefile target may apply the whole deploy/oicm directory"
    )
