"""The one authoritative definition set for the spend aggregation views.

Seven ordinary views over ``LiteLLM_SpendLogs`` back the Admin UI spend dashboards
and the ``/global/spend/*`` read paths:

=========================  ==========================================
View                       Consumed by
=========================  ==========================================
``Last30dModelsBySpend``   ``/global/spend/models``
``Last30dKeysBySpend``     ``/global/spend/keys``
``DailyTagSpend``          ``/global/spend/tags`` (compatibility bridge)
``MonthlyGlobalSpend``     ``/global/spend`` and ``/global/spend/logs``
``MonthlyGlobalSpendPerKey``           ``/global/spend/logs`` per-key
``MonthlyGlobalSpendPerUserPerKey``    internal-user spend logs
``Last30dTopEndUsersSpend``            ``/global/spend/end_users``
=========================  ==========================================

``LiteLLM_VerificationTokenView`` is deliberately absent: it is owned by the
authentication bootstrap in ``litellm/proxy/utils.py`` (it is read on every
request), not by the spend-view repair path, and it joins live token/team
tables rather than spend logs.

Three consumers render these definitions and must never drift apart:

* the boot-time bootstrap in :mod:`litellm.proxy.db.create_views`,
* the standalone ``db_scripts/create_views.py`` operator script,
* the guarded Prisma migration in
  ``litellm-proxy-extras/litellm_proxy_extras/migrations/``, whose SQL block is
  generated from this module by ``scripts/render_spend_view_migration.py``.

``scripts/check_rendered_spend_view_sql.py`` (also run as a unit test,
``tests/unit/proxy/db/test_view_definitions.py``) compares the migration's
embedded SQL against a live rendering of this module, so a definition edit that
misses the regeneration step fails CI instead of shipping drift.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, NamedTuple

__all__ = [
    "SPEND_VIEW_DDL",
    "SPEND_VIEW_NAMES",
    "SpendViewDefinition",
    "spend_view_ddl",
    "spend_view_names",
]


class SpendViewDefinition(NamedTuple):
    """One view's authoritative DDL plus how it may be corrected in place.

    ``allow_replace`` marks views whose column list is stable, so an existing
    same-owner copy can be corrected with ``CREATE OR REPLACE VIEW``. Views
    whose historical column list differs (``DailyTagSpend`` grew
    ``spend_date``/``log_count`` when the tag endpoint contract changed) cannot
    be replaced in place, because Postgres rejects ``CREATE OR REPLACE VIEW``
    that adds columns; those must be dropped and recreated, and only inside the
    guarded ownership check.
    """

    name: str
    ddl: str
    allow_replace: bool


_MONTHLY_GLOBAL_SPEND: Final = """
CREATE OR REPLACE VIEW "MonthlyGlobalSpend" AS
SELECT
DATE("startTime") AS date,
SUM("spend") AS spend
FROM
"LiteLLM_SpendLogs"
WHERE
"startTime" >= (CURRENT_DATE - INTERVAL '30 days')
GROUP BY
DATE("startTime");
"""

_LAST30D_KEYS_BY_SPEND: Final = """
CREATE OR REPLACE VIEW "Last30dKeysBySpend" AS
SELECT
L."api_key",
V."key_alias",
V."key_name",
SUM(L."spend") AS total_spend
FROM
"LiteLLM_SpendLogs" L
LEFT JOIN
"LiteLLM_VerificationToken" V
ON
L."api_key" = V."token"
WHERE
L."startTime" >= (CURRENT_DATE - INTERVAL '30 days')
GROUP BY
L."api_key", V."key_alias", V."key_name"
ORDER BY
total_spend DESC;
"""

_LAST30D_MODELS_BY_SPEND: Final = """
CREATE OR REPLACE VIEW "Last30dModelsBySpend" AS
SELECT
"model",
SUM("spend") AS total_spend
FROM
"LiteLLM_SpendLogs"
WHERE
"startTime" >= (CURRENT_DATE - INTERVAL '30 days')
AND "model" != ''
GROUP BY
"model"
ORDER BY
total_spend DESC;
"""

_MONTHLY_GLOBAL_SPEND_PER_KEY: Final = """
CREATE OR REPLACE VIEW "MonthlyGlobalSpendPerKey" AS
SELECT
DATE("startTime") AS date,
SUM("spend") AS spend,
api_key as api_key
FROM
"LiteLLM_SpendLogs"
WHERE
"startTime" >= (CURRENT_DATE - INTERVAL '30 days')
GROUP BY
DATE("startTime"),
api_key;
"""

_MONTHLY_GLOBAL_SPEND_PER_USER_PER_KEY: Final = """
CREATE OR REPLACE VIEW "MonthlyGlobalSpendPerUserPerKey" AS
SELECT
DATE("startTime") AS date,
SUM("spend") AS spend,
api_key as api_key,
"user" as "user"
FROM
"LiteLLM_SpendLogs"
WHERE
"startTime" >= (CURRENT_DATE - INTERVAL '30 days')
GROUP BY
DATE("startTime"),
"user",
api_key;
"""

_DAILY_TAG_SPEND: Final = """
CREATE OR REPLACE VIEW "DailyTagSpend" AS
SELECT
jsonb_array_elements_text(request_tags) AS individual_request_tag,
DATE(s."startTime") AS spend_date,
COUNT(*) AS log_count,
SUM(spend) AS total_spend
FROM "LiteLLM_SpendLogs" s
GROUP BY individual_request_tag, DATE(s."startTime");
"""

_LAST30D_TOP_END_USERS_SPEND: Final = """
CREATE VIEW "Last30dTopEndUsersSpend" AS
SELECT end_user, COUNT(*) AS total_events, SUM(spend) AS total_spend
FROM "LiteLLM_SpendLogs"
WHERE end_user <> '' AND end_user <> user
AND "startTime" >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY end_user
ORDER BY total_spend DESC
LIMIT 100;
"""

_DEFS: Final = (
    SpendViewDefinition(name="MonthlyGlobalSpend", ddl=_MONTHLY_GLOBAL_SPEND, allow_replace=True),
    SpendViewDefinition(name="Last30dKeysBySpend", ddl=_LAST30D_KEYS_BY_SPEND, allow_replace=True),
    SpendViewDefinition(name="Last30dModelsBySpend", ddl=_LAST30D_MODELS_BY_SPEND, allow_replace=True),
    SpendViewDefinition(
        name="MonthlyGlobalSpendPerKey",
        ddl=_MONTHLY_GLOBAL_SPEND_PER_KEY,
        allow_replace=True,
    ),
    SpendViewDefinition(
        name="MonthlyGlobalSpendPerUserPerKey",
        ddl=_MONTHLY_GLOBAL_SPEND_PER_USER_PER_KEY,
        allow_replace=True,
    ),
    SpendViewDefinition(name="DailyTagSpend", ddl=_DAILY_TAG_SPEND, allow_replace=False),
    SpendViewDefinition(
        name="Last30dTopEndUsersSpend",
        ddl=_LAST30D_TOP_END_USERS_SPEND,
        allow_replace=True,
    ),
)

SPEND_VIEW_DDL: Final[Mapping[str, SpendViewDefinition]] = MappingProxyType({d.name: d for d in _DEFS})
"""Every spend view this module owns, keyed by view name, in no defined order."""

SPEND_VIEW_NAMES: Final[tuple[str, ...]] = tuple(d.name for d in _DEFS)
"""The seven view names, in the order the migration validates them."""


def spend_view_ddl(name: str) -> str:
    """The authoritative DDL for one spend view.

    Raises KeyError for a name this module does not own, so a typo in a caller
    (bootstrap, operator script, migration) surfaces immediately instead of
    silently creating a view nobody maintains.
    """
    return SPEND_VIEW_DDL[name].ddl


def spend_view_names() -> tuple[str, ...]:
    """The seven view names this module owns."""
    return SPEND_VIEW_NAMES
