"""Build status sources from the declarative source config.

Kept separate from the controller so the wiring is one testable function: the
config decides which OICM instances exist, and each instance's credentials are
resolved from its own environment variables, which the Deployment wires from the
matching Secret with ``secretKeyRef``.

Credential variable names are derived from the source name
(``OICM_SOURCE_<NAME>_USERNAME`` / ``_PASSWORD``), so adding a cluster is a
ConfigMap entry plus a Secret, with no code change.
"""

from __future__ import annotations

import logging
import os
from typing import Mapping, Optional, Sequence

from .sources_config import OicmSourceConfig, load_sources
from .status import OicmStatusSource, StatusSource

logger = logging.getLogger("oicm-discovery")


def _env_prefix(config: OicmSourceConfig) -> str:
    return config.env_prefix()


def build_status_sources(
    env: Optional[Mapping[str, str]] = None,
    configs: Optional[Sequence[OicmSourceConfig]] = None,
) -> tuple[StatusSource, ...]:
    """Create one ``OicmStatusSource`` per configured instance.

    A source whose credentials are absent is skipped with a warning rather than
    created with empty credentials, because an unauthenticated source would fail
    every cycle and log noise for a config mistake that is visible immediately.
    """
    env = env if env is not None else os.environ
    configs = configs if configs is not None else load_sources(env=env)

    sources: list[StatusSource] = []
    for config in configs:
        prefix = _env_prefix(config)
        username = env.get(f"{prefix}_USERNAME", "")
        password = env.get(f"{prefix}_PASSWORD", "")
        if not username or not password:
            logger.warning(
                "OICM source %s skipped: %s_USERNAME / %s_PASSWORD are not set",
                config.name,
                prefix,
                prefix,
            )
            continue
        sources.append(OicmStatusSource(config, username=username, password=password))
    return tuple(sources)
