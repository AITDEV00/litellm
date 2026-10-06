"""Declarative OICM source definitions.

One source per OICM instance the controller reads status from. Every value that
identifies an instance (endpoints, realm, workspace) lives in the ConfigMap, so
adding a cluster is a data change, not a code change.

Credentials are deliberately NOT here. A ConfigMap cannot hold or interpolate a
Secret value, so each source names the credential keys instead and the Deployment
wires them from the Secret with ``secretKeyRef``. That keeps the ConfigMap
non-sensitive and committable while still making the *source definition* (which
secret, which keys) declarative and visible in one place.

Environment overrides, all optional, so a deployment can be pointed at a
different instance without editing the ConfigMap:

- ``OICM_SOURCES_FILE``: path to the sources YAML (default
  ``/etc/oicm/sources.yaml``).
- ``OICM_SOURCE_<NAME>_BASE_URL``, ``OICM_SOURCE_<NAME>_AUTH_URL``,
  ``OICM_SOURCE_<NAME>_WORKSPACE_ID``, ``OICM_SOURCE_<NAME>_REALM``,
  ``OICM_SOURCE_<NAME>_CLIENT_ID``: override one field on one source, where
  ``<NAME>`` is the source name upper-cased with non-alphanumerics as ``_``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Tuple

import yaml

logger = logging.getLogger("oicm-discovery")

DEFAULT_SOURCES_FILE = "/etc/oicm/sources.yaml"
DEFAULT_TIMEOUT = 20.0
DEFAULT_CONCURRENCY = 20
DEFAULT_GRANT_TYPE = "password"
DEFAULT_REALM = "adeo"
DEFAULT_CLIENT_ID = "adeo"

# Repository-relative fallback so a local run works without the ConfigMap.
_LOCAL_SOURCES_FILE = (
    Path(__file__).resolve().parent.parent / "deploy" / "oicm" / "sources.yaml"
)


@dataclass(frozen=True, slots=True)
class OicmSourceConfig:
    """One OICM instance to read status from.

    ``workspace_id`` is per source rather than global, because the two clusters
    have different workspaces and each source queries its own.
    """

    name: str
    base_url: str
    auth_url: str
    workspace_id: str
    realm: str = DEFAULT_REALM
    client_id: str = DEFAULT_CLIENT_ID
    grant_type: str = DEFAULT_GRANT_TYPE
    verify_tls: bool = True
    timeout: float = DEFAULT_TIMEOUT
    concurrency: int = DEFAULT_CONCURRENCY

    def env_prefix(self) -> str:
        return "OICM_SOURCE_" + "".join(
            c.upper() if c.isalnum() else "_" for c in self.name
        )

    def with_env_overrides(self, env: Mapping[str, str]) -> "OicmSourceConfig":
        """Apply ``OICM_SOURCE_<NAME>_*`` overrides from ``env``."""
        prefix = self.env_prefix()

        def override(field: str, current: str) -> str:
            return env.get(f"{prefix}_{field}", current) or current

        verify_raw = env.get(f"{prefix}_VERIFY_TLS")
        verify = (
            self.verify_tls
            if verify_raw is None
            else verify_raw.strip().lower() in ("true", "1", "yes")
        )
        return OicmSourceConfig(
            name=self.name,
            base_url=override("BASE_URL", self.base_url),
            auth_url=override("AUTH_URL", self.auth_url),
            workspace_id=override("WORKSPACE_ID", self.workspace_id),
            realm=override("REALM", self.realm),
            client_id=override("CLIENT_ID", self.client_id),
            grant_type=override("GRANT_TYPE", self.grant_type),
            verify_tls=verify,
            timeout=self.timeout,
            concurrency=self.concurrency,
        )


def _bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return default


def _required(name: str, field: str, value: object) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError(f"OICM source {name!r} is missing required field {field!r}")


def parse_sources(document: object) -> Tuple[OicmSourceConfig, ...]:
    """Build source configs from a parsed ``sources.yaml`` document.

    Fails loudly on a malformed document rather than starting with a silently
    empty source list, because an empty list would make the controller treat
    every deployment as deleted.
    """
    if not isinstance(document, dict):
        raise ValueError("sources config must be a mapping with a 'sources' key")
    raw_sources = document.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ValueError("sources config must list at least one source")

    seen: set[str] = set()
    sources: list[OicmSourceConfig] = []
    for raw in raw_sources:
        if not isinstance(raw, dict):
            raise ValueError(f"each source must be a mapping, got {type(raw).__name__}")
        name = _required(str(raw.get("name", "")), "name", raw.get("name"))
        if name in seen:
            raise ValueError(f"duplicate OICM source name {name!r}")
        seen.add(name)
        sources.append(
            OicmSourceConfig(
                name=name,
                base_url=_required(name, "base_url", raw.get("base_url")),
                auth_url=_required(name, "auth_url", raw.get("auth_url")),
                workspace_id=_required(name, "workspace_id", raw.get("workspace_id")),
                realm=str(raw.get("realm") or DEFAULT_REALM),
                client_id=str(raw.get("client_id") or DEFAULT_CLIENT_ID),
                grant_type=str(raw.get("grant_type") or DEFAULT_GRANT_TYPE),
                verify_tls=_bool(raw.get("verify_tls"), True),
                timeout=float(raw.get("timeout") or DEFAULT_TIMEOUT),
                concurrency=int(raw.get("concurrency") or DEFAULT_CONCURRENCY),
            )
        )
    return tuple(sources)


def _read_sources_file(path: str) -> Optional[object]:
    """Read the sources document from a file.

    The same file serves both roles: applied as a ConfigMap in-cluster, where
    the document lives under ``data["sources.yaml"]``, and read directly by a
    local run. A ConfigMap wrapper is unwrapped so one file is the single source
    of truth rather than two copies that can drift.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("OICM sources file %s not readable: %s", path, e)
        return None
    document = yaml.safe_load(text)
    if isinstance(document, dict) and document.get("kind") == "ConfigMap":
        return yaml.safe_load((document.get("data") or {}).get("sources.yaml", ""))
    return document


def load_sources(
    path: Optional[str] = None, env: Optional[Mapping[str, str]] = None
) -> Tuple[OicmSourceConfig, ...]:
    """Load source configs from the ConfigMap file, then apply env overrides.

    Returns an empty tuple when no config is found, which the caller reads as
    "status polling disabled" rather than an error, so the controller still
    starts and reconciles discovery.
    """
    env = env if env is not None else os.environ
    candidate = path or env.get("OICM_SOURCES_FILE") or DEFAULT_SOURCES_FILE
    document = _read_sources_file(candidate)
    if document is None and path is None and candidate != str(_LOCAL_SOURCES_FILE):
        document = _read_sources_file(str(_LOCAL_SOURCES_FILE))
    if document is None:
        return ()

    sources = tuple(s.with_env_overrides(env) for s in parse_sources(document))
    logger.info(
        "OICM sources: %s",
        ", ".join(f"{s.name}({s.base_url})" for s in sources),
    )
    return sources
