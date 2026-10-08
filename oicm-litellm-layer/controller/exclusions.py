"""Excluded model ids: deployments the controller must never register.

A vertical slice, like ``sources_config``: one YAML file (applied as the
``oicm-exclusions`` ConfigMap) declares which models must not exist on the
gateway at all. Exclusion is stronger than the ``blocked`` routing flag: an
excluded deployment is never registered, and one that is already registered is
deleted, so it is absent from ``/v1/models`` and invisible to every consumer
rather than merely unroutable.

Each entry matches any identity an operator is likely to write: the served model
id, the gateway's sanitized name (slashes as ``--``), or the deployment uuid.
The uuid is what covers a Stopped deployment, whose served id is not
discoverable. Matching by uuid alone would be fragile, because a uuid changes on
every redeploy, so the model id is the durable choice wherever it is known.

Environment overrides, all optional:

- ``OICM_EXCLUSIONS_FILE``: path to the exclusions YAML (default
  ``/etc/oicm-exclusions/exclusions.yaml``).
- ``OICM_EXCLUDED_MODEL_IDS``: comma-separated ids, merged with the file.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Final, FrozenSet, Mapping, Optional

import yaml

from .models import OicmModel

logger = logging.getLogger("oicm-discovery")

DEFAULT_EXCLUSIONS_FILE: Final[str] = "/etc/oicm-exclusions/exclusions.yaml"

# Repository-relative fallback so a local run works without the ConfigMap.
_LOCAL_EXCLUSIONS_FILE: Final = (
    Path(__file__).resolve().parent.parent / "deploy" / "oicm" / "exclusions.yaml"
)


def _normalize(ids: object) -> FrozenSet[str]:
    if not isinstance(ids, list):
        return frozenset()
    return frozenset(i.strip() for i in ids if isinstance(i, str) and i.strip())


def parse_exclusions(document: object) -> FrozenSet[str]:
    """The excluded identities in a parsed exclusions document.

    Accepts either a bare list or a mapping with a ``model_ids`` key. A
    malformed document yields the empty set rather than raising: exclusion is a
    safety net, so an unreadable one must never stop the controller, and the
    worst case of missing it is a model that stays registered (recoverable)
    rather than every model being deleted.
    """
    if isinstance(document, list):
        return _normalize(document)
    if isinstance(document, dict):
        return _normalize(document.get("model_ids"))
    return frozenset()


def _read_exclusions_file(path: str) -> Optional[object]:
    """Read the exclusions document from a file.

    The same file serves both roles: applied as a ConfigMap in-cluster, where the
    document lives under ``data["exclusions.yaml"]``, and read directly by a
    local run. A ConfigMap wrapper is unwrapped so one file is the single source
    of truth rather than two copies that can drift.

    A file that cannot be read or is not valid YAML yields None, which the caller
    reads as "nothing excluded". Parsing is a safety net, so a broken file must
    degrade to fewer exclusions rather than crash the controller: the caller
    loads this in its constructor, so a raise here would stop the controller from
    starting at all.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("OICM exclusions file %s not readable: %s", path, e)
        return None
    try:
        document = yaml.safe_load(text)
        if isinstance(document, dict) and document.get("kind") == "ConfigMap":
            return yaml.safe_load((document.get("data") or {}).get("exclusions.yaml", ""))
        return document
    except yaml.YAMLError as e:
        logger.warning("OICM exclusions file %s is not valid YAML: %s", path, e)
        return None


def _env_ids(env: Mapping[str, str]) -> FrozenSet[str]:
    raw = env.get("OICM_EXCLUDED_MODEL_IDS")
    if raw is None:
        return frozenset()
    return frozenset(i.strip() for i in raw.split(",") if i.strip())


def load_exclusions(
    path: Optional[str] = None, env: Optional[Mapping[str, str]] = None
) -> FrozenSet[str]:
    """Load excluded identities from the ConfigMap file and the env override.

    Returns an empty set when no config is found, which means "nothing is
    excluded". The env var is merged rather than replacing the file, so an
    operator can add a temporary exclusion without editing the ConfigMap.

    Read once at startup rather than per sync on purpose: a file that is
    transiently unreadable would otherwise yield an empty set and re-register
    every excluded model.
    """
    env = env if env is not None else os.environ
    candidate = path or env.get("OICM_EXCLUSIONS_FILE") or DEFAULT_EXCLUSIONS_FILE
    document = _read_exclusions_file(candidate)
    if document is None and path is None and candidate != str(_LOCAL_EXCLUSIONS_FILE):
        document = _read_exclusions_file(str(_LOCAL_EXCLUSIONS_FILE))
    ids = parse_exclusions(document) | _env_ids(env)
    if ids:
        logger.info("Excluding %d model identit(ies): %s", len(ids), ", ".join(sorted(ids)))
    return ids


def excluded(model: OicmModel, ids: FrozenSet[str]) -> bool:
    """True when a discovered deployment must not be registered.

    Matches the served model id, the sanitized gateway name, the deployment uuid,
    or the stripped deployment id. The last two are the same value for a local
    deployment; they differ only for a Submariner import, whose uuid carries the
    source prefix.
    """
    if not ids:
        return False
    return bool(
        {model.model_id, model.model_name, model.uuid, model.deployment_id} & ids
    )
