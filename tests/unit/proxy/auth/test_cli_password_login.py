"""Password verification for connector login must not mint a dashboard key.

The connector login is machine-facing: it verifies a username/password and then
issues exactly one restricted credential. If verification went through
``authenticate_user`` it would also mint a UI session key, which is a credential
the connector never asked for and, for an admin, one that outlives the 403 that
rejects the login. These tests pin the split: verification alone creates no key,
and only the successful ordinary-user path proceeds to issuance.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

import pytest

from litellm.proxy._types import LitellmUserRoles, ProxyException
from litellm.proxy.auth import login_utils
from litellm.proxy.auth.login_throttle import LoginAttempt


class _Row:
    def __init__(self, user_id: str, user_role: str, password: str, email: str = "user@example.com") -> None:
        self.user_id = user_id
        self.user_role = user_role
        self.password = password
        self.user_email = email
        self.password_reset_required = False
        self.last_breach_check_at = None


class _Table:
    def __init__(self, row: _Row | None) -> None:
        self._row = row

    async def find_first(self, where: object) -> _Row | None:
        return self._row


class _Repository:
    def __init__(self, row: _Row | None) -> None:
        self.table = _Table(row)


class _Prisma:
    """Only what ``verify_password_only`` reaches: user lookup by email."""


class _Throttle:
    """Records attempt/settle calls without touching the shared throttle stores."""

    def __init__(self) -> None:
        self.attempts = 0
        self.successes = 0
        self.failures = 0
        self.enabled = False

    async def attempt(self, username: str) -> LoginAttempt:
        self.attempts += 1
        outer = self

        class _Recording(LoginAttempt):  # pyright: ignore[reportUntypedBaseClass]  # test seam over a frozen dataclass
            async def succeeded(self) -> None:
                outer.successes += 1

            async def failed(self) -> None:
                outer.failures += 1

        return _Recording(throttle=self, username=username)  # pyright: ignore[reportArgumentType]  # recording seam

    async def clear_pair(self, username: str) -> None:
        return None

    async def record_failure(self, username: str) -> tuple[int, int]:
        return (0, 0)


_ORDINARY: Final = LitellmUserRoles.INTERNAL_USER.value


def _patch_repository(monkeypatch: pytest.MonkeyPatch, row: _Row | None) -> None:
    monkeypatch.setattr(login_utils, "UserRepository", lambda _prisma: _Repository(row), raising=True)


def _password(value: str) -> str:
    return login_utils.hash_password(value)


@pytest.mark.asyncio
async def test_correct_password_returns_identity_and_mints_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The successful path returns the authenticated user with no credential.

    This is the fix for double-key proliferation: verification must not create a
    dashboard key. ``VerifiedIdentity`` has no ``key`` attribute, so a caller
    cannot accidentally treat a verification result as a credential.
    """
    row: Final = _Row("user-1", _ORDINARY, _password("correct horse battery staple"))
    _patch_repository(monkeypatch, row)
    minted: Final[list[object]] = []

    async def _no_mint(*args: object, **kwargs: object) -> object:
        minted.append((args, kwargs))
        return {"token": "should-not-exist"}

    monkeypatch.setattr(login_utils, "generate_key_helper_fn", _no_mint, raising=False)
    throttle: Final = _Throttle()

    verified: Final = await login_utils.verify_password_only(
        username="user@example.com",
        password="correct horse battery staple",
        master_key="sk-master",
        prisma_client=_Prisma(),  # pyright: ignore[reportArgumentType]  # narrow fake
        throttle=throttle,  # pyright: ignore[reportArgumentType]  # recording seam
        general_settings=MappingProxyType({}),
    )

    assert verified.user_id == "user-1"
    assert verified.user_role == _ORDINARY
    assert not hasattr(verified, "key"), "verification must never carry a credential"
    assert minted == [], "verification must not mint any key"
    assert throttle.successes == 1


@pytest.mark.asyncio
async def test_wrong_password_counts_a_failed_attempt_and_mints_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad password is rate-limited through the same throttle as a UI sign-in and
    never reaches key issuance."""
    row: Final = _Row("user-1", _ORDINARY, _password("right-password"))
    _patch_repository(monkeypatch, row)
    throttle: Final = _Throttle()

    with pytest.raises(ProxyException) as raised:
        await login_utils.verify_password_only(
            username="user@example.com",
            password="wrong-password",
            master_key="sk-master",
            prisma_client=_Prisma(),  # pyright: ignore[reportArgumentType]  # narrow fake
            throttle=throttle,  # pyright: ignore[reportArgumentType]  # recording seam
            general_settings=MappingProxyType({}),
        )

    assert str(raised.value.code) == "401"
    assert throttle.failures == 1, "a wrong password must be counted as a failed sign-in"


@pytest.mark.asyncio
async def test_admin_is_rejected_by_verification_before_any_key_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An admin credential must be refused at verification, not after a UI key was
    already minted for it. This is the exact defect the old handler had."""
    monkeypatch.setattr(login_utils, "_matches_env_credentials", lambda _u, _p, _m: True, raising=True)
    throttle: Final = _Throttle()

    with pytest.raises(ProxyException) as raised:
        await login_utils.verify_password_only(
            username="admin",
            password="admin-password",
            master_key="sk-admin-password",
            prisma_client=None,
            throttle=throttle,  # pyright: ignore[reportArgumentType]  # recording seam
            general_settings=MappingProxyType({"UI_USERNAME": "admin", "UI_PASSWORD": "admin-password"}),
        )

    assert str(raised.value.code) == "403"
    assert "ordinary user account" in raised.value.message


@pytest.mark.asyncio
async def test_reset_required_is_reported_not_silently_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reset-required password verifies but is flagged, so the caller refuses it
    instead of issuing a working connector credential."""
    row: Final = _Row("user-1", _ORDINARY, _password("shared-password"))
    row.password_reset_required = True
    _patch_repository(monkeypatch, row)

    verified: Final = await login_utils.verify_password_only(
        username="user@example.com",
        password="shared-password",
        master_key="sk-master",
        prisma_client=_Prisma(),  # pyright: ignore[reportArgumentType]  # narrow fake
        throttle=_Throttle(),  # pyright: ignore[reportArgumentType]  # recording seam
        general_settings=MappingProxyType({}),
    )

    assert verified.password_reset_required is True
    assert not hasattr(verified, "key")


@pytest.mark.asyncio
async def test_disabled_password_login_is_honored(monkeypatch: pytest.MonkeyPatch) -> None:
    """When SSO fully disables password login, verification refuses before checking
    any password, so the connector cannot bypass that policy."""
    monkeypatch.setattr(login_utils, "is_sso_provider_fully_configured", lambda: True, raising=True)
    throttle: Final = _Throttle()

    with pytest.raises(ProxyException) as raised:
        await login_utils.verify_password_only(
            username="user@example.com",
            password="anything",
            master_key="sk-master",
            prisma_client=None,
            throttle=throttle,  # pyright: ignore[reportArgumentType]  # recording seam
            general_settings=MappingProxyType({"disable_password_login_when_sso_enabled": True}),
        )

    assert str(raised.value.code) == "403"
    assert throttle.attempts == 0, "a disabled path must not even consume a throttle attempt"
