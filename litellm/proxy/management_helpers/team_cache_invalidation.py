"""Team key cache invalidation (OICM-custom).

Co-located slice for invalidating cached virtual-key objects for a team when
its team-level config (e.g. ``team_models``) changes. Without this, keys
served through the user-api-key cache can keep serving stale team limits until
their TTL expires.

This logic is OICM-custom and does not exist upstream. Keeping it in its own
module means upstream's ``team_endpoints.py`` and
``model_management_endpoints.py`` stay conflict-free on merges and only wire
the single helper call-site.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from litellm._logging import verbose_proxy_logger
from litellm.repositories.verification_token_repository import (
    VerificationTokenRepository,
)

if TYPE_CHECKING:
    from litellm.proxy.common_utils.user_api_key_cache import UserApiKeyCache
    from litellm.proxy.utils import ProxyLogging


def _sanitize_for_log(value: object) -> str:
    """Strip CR/LF from user-controlled values to prevent log injection."""
    try:
        text = str(value)
    except Exception:
        text = repr(value)
    return text.replace("\r", "").replace("\n", "")


async def _invalidate_team_key_caches(
    team_id: str,
    user_api_key_cache: UserApiKeyCache,
    proxy_logging_obj: Optional[ProxyLogging],
) -> None:
    """Drop the cached virtual-key objects for every key belonging to *team_id*.

    Enumerates the team's verification tokens and evicts each hashed token from
    the user-API-key cache so subsequent requests re-read the team's current
    model access from the DB. Failures are non-fatal: a stale cached key object
    serves until its TTL expires.
    """
    from litellm.proxy.auth.auth_checks import _delete_cache_key_object
    from litellm.proxy.proxy_server import prisma_client as _prisma_client
    from litellm.proxy.utils import _hash_token_if_needed

    if _prisma_client is None:
        return

    try:
        team_keys = await VerificationTokenRepository(_prisma_client).find_by_team_id(team_id=team_id)
    except Exception as e:
        verbose_proxy_logger.warning(
            "_invalidate_team_key_caches: failed to enumerate keys for team_id=%s: %s. "
            "Cached key objects may serve stale team_models until their TTL expires.",
            _sanitize_for_log(team_id),
            e,
        )
        return

    for key_row in team_keys:
        if key_row.token is None:
            continue
        hashed_token = _hash_token_if_needed(key_row.token)
        await _delete_cache_key_object(
            hashed_token=hashed_token,
            user_api_key_cache=user_api_key_cache,
            proxy_logging_obj=proxy_logging_obj,
        )