"""Tests for the OICM team key-cache invalidation.

The ``_invalidate_team_key_caches`` helper drops the cached virtual-key objects
for every key belonging to a team so requests re-read the team's models from
the DB after a team config change.

The helper lazy-imports its collaborators inside the function body, so the
patches target the source modules those imports resolve to.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

DELETION_PATCH = "litellm.proxy.auth.auth_checks._delete_cache_key_object"
HASH_PATCH = "litellm.proxy.utils._hash_token_if_needed"
PRISMA_PATCH = "litellm.proxy.proxy_server.prisma_client"
REPO_PATCH = (
    "litellm.proxy.management_helpers.team_cache_invalidation.VerificationTokenRepository"
)


@pytest.mark.asyncio
async def test_invalidate_team_key_caches_deletes_each_key():
    """Every non-null key for the team is dropped from the cache."""
    from litellm.proxy.management_helpers.team_cache_invalidation import (
        _invalidate_team_key_caches,
    )

    key_row = MagicMock()
    key_row.token = "sk-abc"
    key_row_skipped = MagicMock()
    key_row_skipped.token = None

    with (
        patch(REPO_PATCH) as mock_repo_cls,
        patch(HASH_PATCH, return_value="hashed-token"),
        patch(DELETION_PATCH, new_callable=AsyncMock) as mock_delete,
        patch(PRISMA_PATCH, new_callable=lambda: MagicMock()),
    ):
        mock_repo_cls.return_value.find_by_team_id = AsyncMock(
            return_value=[key_row, key_row_skipped]
        )

        await _invalidate_team_key_caches(
            team_id="team-123",
            user_api_key_cache=MagicMock(),
            proxy_logging_obj=MagicMock(),
        )

        # The None-token row is skipped; only the actual key is deleted.
        assert mock_delete.await_count == 1
        assert mock_delete.await_args.kwargs["hashed_token"] == "hashed-token"


@pytest.mark.asyncio
async def test_invalidate_team_key_caches_noop_without_prisma():
    """When prisma_client is None, no keys are queried and nothing is deleted."""
    from litellm.proxy.management_helpers.team_cache_invalidation import (
        _invalidate_team_key_caches,
    )

    with (
        patch(PRISMA_PATCH, None),
        patch(REPO_PATCH) as mock_repo_cls,
        patch(DELETION_PATCH, new_callable=AsyncMock) as mock_delete,
    ):
        await _invalidate_team_key_caches(
            team_id="team-123",
            user_api_key_cache=MagicMock(),
            proxy_logging_obj=MagicMock(),
        )

        mock_repo_cls.assert_not_called()
        mock_delete.assert_not_called()


@pytest.mark.asyncio
async def test_invalidate_team_key_caches_enumeration_failure_is_nonfatal():
    """A failure to enumerate keys logs a warning and does not raise."""
    from litellm.proxy.management_helpers.team_cache_invalidation import (
        _invalidate_team_key_caches,
    )

    with (
        patch(REPO_PATCH) as mock_repo_cls,
        patch(DELETION_PATCH, new_callable=AsyncMock) as mock_delete,
        patch(PRISMA_PATCH, new_callable=lambda: MagicMock()),
        patch(
            "litellm.proxy.management_helpers.team_cache_invalidation.verbose_proxy_logger.warning"
        ) as mock_warn,
    ):
        mock_repo_cls.return_value.find_by_team_id = AsyncMock(side_effect=RuntimeError("db down"))

        await _invalidate_team_key_caches(
            team_id="team-123",
            user_api_key_cache=MagicMock(),
            proxy_logging_obj=MagicMock(),
        )

        mock_warn.assert_called_once()
        mock_delete.assert_not_called()