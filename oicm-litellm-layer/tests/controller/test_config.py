"""Tests for the controller's LITELLM_ADMIN_KEY resolution.

The key must come from LITELLM_ADMIN_KEY when set (in-cluster the Deployment
injects it via secretKeyRef), and otherwise fall back to the single source of
truth in deploy/prod/litellm-proxy.yaml so local runs match production.
"""

import importlib
import sys
from pathlib import Path

import pytest

_CONTROLLER_SRC = Path(__file__).parents[2] / "controller"
if str(_CONTROLLER_SRC.parent) not in sys.path:
    sys.path.insert(0, str(_CONTROLLER_SRC.parent))


def _reload_config_with_env(monkeypatch, env_value=None):
    if env_value is None:
        monkeypatch.delenv("LITELLM_ADMIN_KEY", raising=False)
    else:
        monkeypatch.setenv("LITELLM_ADMIN_KEY", env_value)
    # Re-import so module-level os.getenv runs under the patched env.
    module = importlib.import_module("controller.config")
    return importlib.reload(module)


def test_admin_key_defaults_to_manifest_value(monkeypatch):
    config = _reload_config_with_env(monkeypatch)
    assert config.LITELLM_ADMIN_KEY == "sk-1234"


def test_admin_key_env_overrides_manifest(monkeypatch):
    config = _reload_config_with_env(monkeypatch, env_value="sk-custom")
    assert config.LITELLM_ADMIN_KEY == "sk-custom"


def test_master_key_from_manifest_reads_prod_manifest():
    import controller.config as config

    value = config._master_key_from_manifest()
    assert value == "sk-1234"