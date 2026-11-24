"""The prod config patch must stay in sync with the base config.

`deploy/base/gateway/config/litellm-config.yaml` stores the whole proxy config as
one YAML block scalar (`config.yaml: |`), which kustomize treats as an opaque
string. A patch therefore REPLACES the string rather than merging into it, so
`deploy/overlays/prod/gateway/config.yaml` has to reproduce the base body in full.

That is a real hazard: if someone edits the base and does not regenerate the prod
patch, prod silently loses the change. These tests fail when that happens, so the
drift is caught here instead of in production.

Production is the base plus exactly two keys, `general_settings.enable_jwt_auth`
and `general_settings.litellm_jwtauth`. Anything else differing is either a
forgotten regeneration or an unintended prod override.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest
import yaml

_LAYER_ROOT: Final = Path(__file__).resolve().parents[2]
_BASE_CONFIG: Final = _LAYER_ROOT / "deploy" / "base" / "gateway" / "config" / "litellm-config.yaml"
_PROD_CONFIG: Final = _LAYER_ROOT / "deploy" / "overlays" / "prod" / "gateway" / "config.yaml"

# Keys prod is allowed to add on top of the base. `_flatten` recurses into
# dicts, so the scalar children of `litellm_jwtauth` are what appear as keys.
_PROD_ONLY_CONFIG_KEYS: Final = frozenset(
    {
        "general_settings.enable_jwt_auth",
        "general_settings.litellm_jwtauth.user_id_jwt_field",
        "general_settings.litellm_jwtauth.user_email_jwt_field",
        "general_settings.litellm_jwtauth.team_id_jwt_field",
        "general_settings.litellm_jwtauth.user_id_upsert",
    }
)


def _config_yaml(path: Path) -> str:
    """Pull the `config.yaml` block scalar out of a ConfigMap manifest."""
    document = yaml.safe_load(path.read_text())
    return document["data"]["config.yaml"]


def _flatten(node: object, prefix: str = "") -> dict[str, object]:
    """Flatten nested dicts to dotted keys. Only dicts recurse; lists compare whole."""
    if not isinstance(node, dict):
        return {prefix: node}
    flat: dict[str, object] = {}
    for key, value in node.items():
        child = f"{prefix}.{key}" if prefix else str(key)
        flat.update(_flatten(value, child))
    return flat


@pytest.fixture(scope="module")
def base_flat() -> dict[str, object]:
    return _flatten(yaml.safe_load(_config_yaml(_BASE_CONFIG)))


@pytest.fixture(scope="module")
def prod_flat() -> dict[str, object]:
    return _flatten(yaml.safe_load(_config_yaml(_PROD_CONFIG)))


def test_prod_patch_reproduces_every_base_key(base_flat: dict[str, object], prod_flat: dict[str, object]) -> None:
    """Every base key is present in prod with the same value.

    A missing or changed key means the base moved and the prod patch was not
    regenerated, which would silently drop the base change from prod.
    """
    missing = sorted(key for key in base_flat if key not in prod_flat)
    assert not missing, (
        f"prod config patch is missing base keys: {missing}. "
        f"Regenerate {_PROD_CONFIG.relative_to(_LAYER_ROOT)} from the base."
    )

    changed = sorted(key for key in base_flat if key in prod_flat and base_flat[key] != prod_flat[key])
    assert not changed, (
        f"prod config patch changes base keys: {changed}. "
        "Prod must match the base except for the declared JWT additions."
    )


def test_prod_adds_only_the_jwt_keys(base_flat: dict[str, object], prod_flat: dict[str, object]) -> None:
    """Prod invents no keys beyond the declared ones."""
    added = {key for key in prod_flat if key not in base_flat and key not in _PROD_ONLY_CONFIG_KEYS}
    assert not added, (
        f"prod config patch adds undeclared keys: {sorted(added)}. "
        "Declare them in _PROD_ONLY_CONFIG_KEYS with a reason, or drop them."
    )


def test_prod_enables_jwt_auth(prod_flat: dict[str, object]) -> None:
    """The two keys prod is allowed to add are actually present and enabled."""
    assert prod_flat.get("general_settings.enable_jwt_auth") is True
    assert prod_flat.get("general_settings.litellm_jwtauth.user_id_jwt_field") == "oid"
