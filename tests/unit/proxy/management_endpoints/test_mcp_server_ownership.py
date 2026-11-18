"""Ownership rules for editing and deleting an MCP server.

A user may correct their own submission while it is pending review or has been
rejected. Once a row is active it serves traffic, so changing it is an admin
decision. These pin both halves and the admin escape hatch.
"""

from __future__ import annotations

import pytest

from litellm.proxy._types import (
    LiteLLM_MCPServerTable,
    LitellmUserRoles,
    MCPApprovalStatus,
    MCPTransport,
)
from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth
from litellm.proxy.management_endpoints.mcp_management_endpoints import owner_may_mutate


def _server(submitted_by: str | None, approval_status: str | None) -> LiteLLM_MCPServerTable:
    return LiteLLM_MCPServerTable(
        server_id="srv-1",
        server_name="reports",
        alias="reports",
        transport=MCPTransport.http,
        submitted_by=submitted_by,
        approval_status=approval_status,
    )


def _user(user_id: str | None, role: LitellmUserRoles = LitellmUserRoles.INTERNAL_USER) -> UserAPIKeyAuth:
    return UserAPIKeyAuth(user_id=user_id, user_role=role)


@pytest.mark.parametrize("status", [MCPApprovalStatus.pending_review, MCPApprovalStatus.rejected])
def test_owner_may_mutate_own_unapproved_row(status: str) -> None:
    assert owner_may_mutate(_server("alice", status), _user("alice")) is True


def test_owner_may_mutate_denies_active_row_even_for_the_submitter() -> None:
    """Approved means serving, so the user who submitted it loses the edit right."""
    assert owner_may_mutate(_server("alice", MCPApprovalStatus.active), _user("alice")) is False


def test_owner_may_mutate_denies_another_users_pending_row() -> None:
    assert owner_may_mutate(_server("alice", MCPApprovalStatus.pending_review), _user("bob")) is False


def test_owner_may_mutate_denies_rows_with_no_submitter() -> None:
    """Config-declared servers have no submitted_by; only an admin may touch them."""
    assert owner_may_mutate(_server(None, MCPApprovalStatus.pending_review), _user("alice")) is False


def test_owner_may_mutate_allows_admin_on_an_active_row() -> None:
    admin = _user("admin", LitellmUserRoles.PROXY_ADMIN)
    assert owner_may_mutate(_server("alice", MCPApprovalStatus.active), admin) is True


def test_owner_may_mutate_denies_unauthenticated_caller() -> None:
    assert owner_may_mutate(_server(None, MCPApprovalStatus.pending_review), _user(None)) is False
