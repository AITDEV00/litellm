"""Dev must be the same shape as prod, differing only where dev says it does.

The dev and prod gateways are built from one base, so the only ways they can
diverge are the dev overlay's patches. This test renders both overlays and
asserts that the differences are exactly the ones the patches declare, so a
change that reaches only one environment fails here instead of in production.

It reads the rendered output of `kubectl kustomize`, which is what the Makefile
applies, so it tests the real artifact rather than the source files.

Run with the controller's test extra:

    cd oicm-litellm-layer && uv run --extra test --python 3.13 \
        python -m pytest tests/deploy/test_dev_prod_parity.py
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Final

import pytest
import yaml

_LAYER_ROOT: Final = Path(__file__).resolve().parents[2]
_OVERLAYS: Final = _LAYER_ROOT / "deploy" / "overlays"

# Names that intentionally differ between the two environments. Everything the
# overlays produce must be either an identical object, or a dev object whose name
# is the prod name plus "-dev".
_DEV_SUFFIX: Final = "-dev"

# Objects both environments share on purpose, so they must render byte-identical
# and must NOT be suffixed. A dev-only edit to one of these would change prod.
_SHARED_UNSUFFIXED: Final = ("ConfigMap/litellm-hooks", "Secret/litellm-redis-password")

# Env keys whose value is expected to differ, with the reason. Anything else that
# differs is a drift bug.
_EXPECTED_ENV_DIFFS: Final = frozenset(
    {
        "LITELLM_MASTER_KEY",  # dev has its own master key Secret
        "UI_PASSWORD",  # follows the master key
        "LITELLM_SALT_KEY",  # dev has its own salt key Secret
        "DATABASE_URL",  # dev points at the dev-only Postgres
    }
)

# Config keys under `general_settings` expected to differ, with the reason. Dev
# sets drain sizing at the gateway; prod delegates it to the janitor CronJob.
_EXPECTED_CONFIG_DIFFS: Final = frozenset(
    {
        "maximum_spend_logs_retention_interval",
        "maximum_spend_logs_cleanup_batch_size",
        "maximum_spend_logs_cleanup_max_batches",
        "maximum_spend_logs_cleanup_run_budget",
        "maximum_health_check_retention_period",
    }
)


def _render(overlay: str) -> list[dict]:
    """Render one overlay the way the Makefile applies it."""
    result = subprocess.run(
        ["kubectl", "kustomize", str(_OVERLAYS / overlay)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"kubectl kustomize {overlay} failed:\n{result.stderr}")
    return [d for d in yaml.safe_load_all(result.stdout) if isinstance(d, dict)]


def _by_key(docs: list[dict]) -> dict[str, dict]:
    return {f"{d['kind']}/{d['metadata']['name']}": d for d in docs}


@pytest.fixture(scope="module")
def prod() -> dict[str, dict]:
    return _by_key(_render("prod"))


@pytest.fixture(scope="module")
def dev() -> dict[str, dict]:
    return _by_key(_render("dev"))


def _container(doc: dict) -> dict:
    return doc["spec"]["template"]["spec"]["containers"][0]


def _env_sources(doc: dict) -> dict[str, object]:
    """Map each env var to where its value comes from, so a source swap is caught."""
    out: dict[str, object] = {}
    for entry in _container(doc).get("env", []):
        value_from = entry.get("valueFrom") or {}
        if "secretKeyRef" in value_from:
            ref = value_from["secretKeyRef"]
            out[entry["name"]] = ("secret", ref["name"], ref["key"])
        elif "configMapKeyRef" in value_from:
            ref = value_from["configMapKeyRef"]
            out[entry["name"]] = ("configmap", ref["name"], ref["key"])
        else:
            out[entry["name"]] = ("value", entry.get("value"))
    return out


def test_every_prod_object_has_a_dev_counterpart(prod: dict[str, dict], dev: dict[str, dict]):
    """Prod and dev must declare the same kinds and names, dev suffixed."""
    expected = {
        key if key in _SHARED_UNSUFFIXED else f"{key.split('/')[0]}/{key.split('/')[1]}{_DEV_SUFFIX}"
        for key in prod
    }
    assert set(dev) == expected, (
        "dev must declare one object per prod object (shared ones unsuffixed).\n"
        f"missing from dev: {sorted(expected - set(dev))}\n"
        f"unexpected in dev: {sorted(set(dev) - expected)}"
    )


def test_shared_objects_are_identical(prod: dict[str, dict], dev: dict[str, dict]):
    """The shared ConfigMap and Redis Secret must render identically.

    These are the objects dev mounts from prod on purpose. If one diverges, a
    dev-only edit has silently changed what prod serves.
    """
    for key in _SHARED_UNSUFFIXED:
        assert key in prod, f"{key} must be declared in prod"
        assert key in dev, f"{key} must be shared into dev unsuffixed"
        assert prod[key] == dev[key], f"{key} differs between prod and dev"


def test_deployment_env_keys_match(prod: dict[str, dict], dev: dict[str, dict]):
    """The two Deployments must set exactly the same env vars."""
    prod_env = set(_env_sources(prod["Deployment/litellm-proxy"]))
    dev_env = set(_env_sources(dev[f"Deployment/litellm-proxy{_DEV_SUFFIX}"]))
    assert prod_env == dev_env, (
        f"env vars only in prod: {sorted(prod_env - dev_env)}\n"
        f"env vars only in dev: {sorted(dev_env - prod_env)}"
    )


def test_deployment_env_diff_is_only_the_expected_values(prod: dict[str, dict], dev: dict[str, dict]):
    """Every env difference must be one the dev overlay declares."""
    prod_env = _env_sources(prod["Deployment/litellm-proxy"])
    dev_env = _env_sources(dev[f"Deployment/litellm-proxy{_DEV_SUFFIX}"])
    differing = {name for name in prod_env if prod_env[name] != dev_env[name]}
    unexpected = differing - _EXPECTED_ENV_DIFFS
    assert not unexpected, (
        f"env vars differ without being declared in the dev overlay: {sorted(unexpected)}"
    )
    stale = _EXPECTED_ENV_DIFFS - differing
    assert not stale, (
        f"the dev overlay declares env diffs that no longer differ: {sorted(stale)}"
    )


def test_deployment_image_and_probe_shape_match(prod: dict[str, dict], dev: dict[str, dict]):
    """Image, ports, probes, mounts, and resources must match, so dev runs prod's path.

    Command and replicas are deliberately excluded: dev runs one worker and one
    replica for debugging, which the overlay declares. The `config` volume's
    source is also expected to differ, because dev mounts its own ConfigMap.
    """
    prod_container = _container(prod["Deployment/litellm-proxy"])
    dev_container = _container(dev[f"Deployment/litellm-proxy{_DEV_SUFFIX}"])
    for field in ("image", "ports", "readinessProbe", "livenessProbe", "resources"):
        assert prod_container.get(field) == dev_container.get(field), (
            f"container field {field!r} differs between prod and dev"
        )
    assert prod_container.get("volumeMounts") == dev_container.get("volumeMounts"), (
        "the two Deployments must mount the same volumes at the same paths"
    )

    prod_volumes = {v["name"]: v for v in prod["Deployment/litellm-proxy"]["spec"]["template"]["spec"]["volumes"]}
    dev_volumes = {v["name"]: v for v in dev[f"Deployment/litellm-proxy{_DEV_SUFFIX}"]["spec"]["template"]["spec"]["volumes"]}
    assert set(prod_volumes) == set(dev_volumes), "the two Deployments must declare the same volume names"

    # The shared volumes must be byte-identical; only `config` points elsewhere.
    for name in set(prod_volumes) - {"config"}:
        assert prod_volumes[name] == dev_volumes[name], (
            f"volume {name!r} differs between prod and dev"
        )
    prod_source = prod_volumes["config"]["configMap"]["name"]
    dev_source = dev_volumes["config"]["configMap"]["name"]
    assert dev_source == f"{prod_source}{_DEV_SUFFIX}", (
        f"the config volume must point at the dev copy: expected {prod_source}{_DEV_SUFFIX}, got {dev_source}"
    )


def test_health_check_retention_is_bounded_on_dev(prod: dict[str, dict], dev: dict[str, dict]):
    """Dev sets health-check retention, prod leaves it unset (unbounded)."""
    prod_config = _parsed_config(prod["ConfigMap/litellm-config"])
    dev_config = _parsed_config(dev[f"ConfigMap/litellm-config{_DEV_SUFFIX}"])
    assert "maximum_health_check_retention_period" not in prod_config["general_settings"]
    assert dev_config["general_settings"]["maximum_health_check_retention_period"] == "30d"


def _parsed_config(configmap: dict) -> dict:
    return yaml.safe_load(configmap["data"]["config.yaml"])


def test_config_diff_is_only_the_expected_keys(prod: dict[str, dict], dev: dict[str, dict]):
    """Every config difference must be one the dev overlay declares.

    This is what catches a config value changed in one environment only, which is
    the failure mode that would make a prod deploy behave unlike the dev run that
    was tested.
    """
    prod_config = _parsed_config(prod["ConfigMap/litellm-config"])
    dev_config = _parsed_config(dev[f"ConfigMap/litellm-config{_DEV_SUFFIX}"])

    prod_flat = _flatten(prod_config)
    dev_flat = _flatten(dev_config)

    # Dev may add keys (the cleanup knobs prod delegates to its janitor) and may
    # change values, but only for the keys the overlay declares.
    declared = {f"general_settings.{name}" for name in _EXPECTED_CONFIG_DIFFS}
    differing = {k for k in prod_flat if dev_flat.get(k) != prod_flat[k]}
    differing |= {k for k in dev_flat if k not in prod_flat}
    unexpected = differing - declared
    assert not unexpected, (
        "config keys differ without being declared in the dev overlay: "
        f"{sorted(unexpected)}"
    )
    stale = declared - differing
    assert not stale, (
        f"the dev overlay declares config diffs that no longer differ: {sorted(stale)}"
    )


def _flatten(document: object, prefix: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    if isinstance(document, dict):
        for key, value in document.items():
            out.update(_flatten(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(document, list):
        out[prefix] = tuple(document)
    else:
        out[prefix] = document
    return out


# The pod label each environment's own pods carry. The base hardcodes the prod
# value, and `nameSuffix` renames objects but never rewrites a selector, so a dev
# object left at the base value selects the PROD pods.
_PROD_POD_LABEL: Final = "litellm-proxy"
_DEV_POD_LABEL: Final = "litellm-proxy-dev"


def _selector_labels(document: dict) -> tuple[str, ...]:
    """Every pod-label value this object selects on, from either selector shape."""
    spec = document.get("spec") or {}
    found: list[str] = []
    selector = spec.get("selector")
    if isinstance(selector, dict):
        if isinstance(selector.get("matchLabels"), dict):
            found.extend(str(v) for v in selector["matchLabels"].values())
        found.extend(str(v) for v in selector.values() if isinstance(v, str))
    return tuple(found)


def test_dev_selectors_never_target_prod_pods(dev: dict[str, dict]):
    """No dev object may select a prod pod label.

    This is the regression guard for the dev Service (and PDB) selecting
    `app: litellm-proxy`, the prod pod label. With that selector the dev Service's
    endpoints were the prod replicas, so the dev discovery controller read and
    wrote PRODUCTION. A selector value is a pod label, so a dev object naming the
    prod label is always a routing bug, never a coincidence.
    """
    offenders = {
        key: _selector_labels(doc)
        for key, doc in dev.items()
        if _PROD_POD_LABEL in _selector_labels(doc)
    }
    assert not offenders, (
        "dev objects select the prod pod label "
        f"{_PROD_POD_LABEL!r}, so they route to production: {offenders}"
    )


def test_dev_deployment_pods_carry_the_dev_label(dev: dict[str, dict]):
    """Dev pods must be labeled with the dev value, not the base's prod value.

    The pod label is what a Service selects on. If it stays at the base value,
    the prod Service selects the dev pod and sends production traffic to it.
    """
    labels = dev["Deployment/litellm-proxy-dev"]["spec"]["template"]["metadata"]["labels"]
    assert labels.get("app") == _DEV_POD_LABEL, (
        f"dev pods must carry app={_DEV_POD_LABEL!r}, got {labels.get('app')!r}"
    )


def test_prod_selectors_target_prod_pods(prod: dict[str, dict]):
    """Prod objects must still select the prod pod label, unchanged."""
    for key in ("Service/litellm-proxy", "PodDisruptionBudget/litellm-proxy-pdb"):
        assert _PROD_POD_LABEL in _selector_labels(prod[key]), (
            f"{key} must select app={_PROD_POD_LABEL!r}"
        )
