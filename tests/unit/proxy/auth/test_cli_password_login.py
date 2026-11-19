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
_EMPTY_SETTINGS: Final = MappingProxyType[str, object]({})


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
async def test_database_admin_is_rejected_before_issuance(monkeypatch: pytest.MonkeyPatch) -> None:
    """A password-valid database PROXY_ADMIN must be refused at verification.

    The env UI-admin is already rejected; this covers the database-backed admin,
    which previously verified successfully and only endpoint scoping limited it.
    No key may be minted for it. Endpoint scope is not the boundary."""
    row: Final = _Row("admin-1", LitellmUserRoles.PROXY_ADMIN.value, _password("admin-secret"))
    _patch_repository(monkeypatch, row)
    minted: Final[list[object]] = []

    async def _no_mint(*args: object, **kwargs: object) -> object:
        minted.append((args, kwargs))
        return {"token": "must-not-exist"}

    monkeypatch.setattr(login_utils, "generate_key_helper_fn", _no_mint, raising=False)
    throttle: Final = _Throttle()

    with pytest.raises(ProxyException) as raised:
        await login_utils.verify_password_only(
            username="admin@example.com",
            password="admin-secret",
            master_key="sk-master",
            prisma_client=_Prisma(),  # pyright: ignore[reportArgumentType]  # narrow fake
            throttle=throttle,  # pyright: ignore[reportArgumentType]  # recording seam
            general_settings=MappingProxyType({}),
        )

    assert str(raised.value.code) == "403"
    assert "ordinary user account" in raised.value.message
    assert minted == [], "a database admin must never reach key issuance"
    assert throttle.failures == 1, "the rejected admin attempt must be counted"


@pytest.mark.asyncio
async def test_database_admin_rejection_does_not_rehash(monkeypatch: pytest.MonkeyPatch) -> None:
    """A denied database admin login must not write the password hash back.

    The role check runs before the upgrade rehash, so rejecting an admin is
    entirely side-effect free rather than issuing a database write on the way out.
    """
    row: Final = _Row("admin-1", LitellmUserRoles.PROXY_ADMIN.value, _password("admin-secret"))
    _patch_repository(monkeypatch, row)
    rehashed: Final[list[str]] = []

    async def _rehash_spy(user_id: str, *_args: object) -> None:
        rehashed.append(user_id)

    monkeypatch.setattr(login_utils, "_rehash_password_if_needed", _rehash_spy, raising=True)
    throttle: Final = _Throttle()

    with pytest.raises(ProxyException) as raised:
        await login_utils.verify_password_only(
            username="admin@example.com",
            password="admin-secret",
            master_key="sk-master",
            prisma_client=_Prisma(),  # pyright: ignore[reportArgumentType]  # narrow fake
            throttle=throttle,  # pyright: ignore[reportArgumentType]  # recording seam
            general_settings=MappingProxyType({}),
        )

    assert str(raised.value.code) == "403"
    assert rehashed == [], "a rejected admin login must not write the password back"


class _Request:
    def __init__(self, body: dict[str, object]) -> None:
        self._body = body

    async def json(self) -> dict[str, object]:
        return self._body


class _LoginThrottle:
    """A stand-in for ``LoginThrottle`` whose ``from_request`` returns a recorder."""

    @staticmethod
    def from_request(*_args: object, **_kwargs: object) -> _Throttle:
        return _Throttle()


@pytest.mark.asyncio
async def test_handler_persists_no_key_for_a_denied_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real ``cli_password_login`` handler must persist zero keys when it
    rejects a database admin.

    ``verify_password_only`` minting nothing is only the helper's invariant; the
    defect lived in the handler that minted after verification. This drives the
    actual route function and asserts the repository/mint boundary saw no write.
    """
    from litellm.proxy import proxy_server
    from litellm.proxy.management_endpoints import key_management_endpoints

    row: Final = _Row("admin-1", LitellmUserRoles.PROXY_ADMIN.value, _password("admin-secret"))
    _patch_repository(monkeypatch, row)

    async def _mint_spy(*args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError("a denied admin login reached key issuance")

    monkeypatch.setattr(key_management_endpoints, "generate_key_helper_fn", _mint_spy, raising=True)
    monkeypatch.setattr(proxy_server, "prisma_client", _Prisma(), raising=False)
    monkeypatch.setattr(proxy_server, "master_key", "sk-master", raising=False)
    monkeypatch.setattr(proxy_server, "general_settings", _EMPTY_SETTINGS, raising=False)
    monkeypatch.setattr(proxy_server, "LoginThrottle", _LoginThrottle, raising=False)

    with pytest.raises(ProxyException) as raised:
        await proxy_server.cli_password_login(_Request({"username": "admin@example.com", "password": "admin-secret"}))  # pyright: ignore[reportArgumentType]  # narrow fake request

    assert str(raised.value.code) == "403"


@pytest.mark.asyncio
async def test_handler_mints_exactly_one_key_for_an_ordinary_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ordinary-user handler path issues exactly one scoped key, so the
    zero-write admin proof above is not vacuous for a handler that never mints."""
    from litellm.proxy import proxy_server
    from litellm.proxy.management_endpoints import key_management_endpoints

    row: Final = _Row("user-1", LitellmUserRoles.INTERNAL_USER.value, _password("correct horse battery staple"))
    _patch_repository(monkeypatch, row)
    calls: Final[list[dict[str, object]]] = []

    async def _mint_spy(*args: object, **kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {"key": "sk-issued", "expires": None}

    monkeypatch.setattr(key_management_endpoints, "generate_key_helper_fn", _mint_spy, raising=True)
    monkeypatch.setattr(proxy_server, "prisma_client", _Prisma(), raising=False)
    monkeypatch.setattr(proxy_server, "master_key", "sk-master", raising=False)
    monkeypatch.setattr(proxy_server, "general_settings", _EMPTY_SETTINGS, raising=False)
    monkeypatch.setattr(proxy_server, "LoginThrottle", _LoginThrottle, raising=False)

    response: Final = await proxy_server.cli_password_login(
        _Request({"username": "user@example.com", "password": "correct horse battery staple"})  # pyright: ignore[reportArgumentType]  # narrow fake request
    )

    assert response.status_code == 200
    assert len(calls) == 1, "an ordinary user must reach issuance exactly once"
    assert calls[0]["user_id"] == "user-1"


@pytest.mark.asyncio
async def test_database_admin_view_only_is_also_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """PROXY_ADMIN_VIEW_ONLY is still an administrative role and must be refused."""
    row: Final = _Row("admin-2", LitellmUserRoles.PROXY_ADMIN_VIEW_ONLY.value, _password("viewer-secret"))
    _patch_repository(monkeypatch, row)
    with pytest.raises(ProxyException) as raised:
        await login_utils.verify_password_only(
            username="viewer@example.com",
            password="viewer-secret",
            master_key="sk-master",
            prisma_client=_Prisma(),  # pyright: ignore[reportArgumentType]  # narrow fake
            throttle=_Throttle(),  # pyright: ignore[reportArgumentType]  # recording seam
            general_settings=MappingProxyType({}),
        )
    assert str(raised.value.code) == "403"


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
