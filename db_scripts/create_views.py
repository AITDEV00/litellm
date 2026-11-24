"""
python script to pre-create all views required by LiteLLM Proxy Server

The seven spend aggregation views render from
litellm.proxy.db.spend_view_definitions (the same source as the boot bootstrap
and the guarded Prisma migration), so the three surfaces cannot drift. Only
LiteLLM_VerificationTokenView keeps a local definition: it belongs to the
authentication bootstrap, not the spend set.
"""

import asyncio
import sys
from pathlib import Path

from prisma import Prisma

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from litellm.proxy.db.spend_view_definitions import spend_view_ddl  # noqa: E402

# Enter your DATABASE_URL here

db = Prisma(
    http={
        "timeout": 60000,
    },
)


async def check_view_exists():
    """
    Checks if the LiteLLM_VerificationTokenView and MonthlyGlobalSpend exists in the user's db.

    LiteLLM_VerificationTokenView: This view is used for getting the token + team data in user_api_key_auth

    MonthlyGlobalSpend: This view is used for the admin view to see global spend for this month

    If the view doesn't exist, one will be created.
    """

    # connect to dB
    await db.connect()
    try:
        # Try to select one row from the view
        await db.query_raw("""SELECT 1 FROM "LiteLLM_VerificationTokenView" LIMIT 1""")
        print("LiteLLM_VerificationTokenView Exists!")  # noqa: T201
    except Exception:
        # If an error occurs, the view does not exist, so create it
        await db.execute_raw("""
                CREATE VIEW "LiteLLM_VerificationTokenView" AS
                SELECT 
                v.*, 
                t.spend AS team_spend, 
                t.max_budget AS team_max_budget, 
                t.tpm_limit AS team_tpm_limit, 
                t.rpm_limit AS team_rpm_limit
                FROM "LiteLLM_VerificationToken" v
                LEFT JOIN "LiteLLM_TeamTable" t ON v.team_id = t.team_id;
            """)

        print("LiteLLM_VerificationTokenView Created!")  # noqa: T201

    for view_name in (
        "MonthlyGlobalSpend",
        "Last30dKeysBySpend",
        "Last30dModelsBySpend",
        "MonthlyGlobalSpendPerKey",
        "MonthlyGlobalSpendPerUserPerKey",
        "DailyTagSpend",
        "Last30dTopEndUsersSpend",
    ):
        try:
            await db.query_raw(f"""SELECT 1 FROM "{view_name}" LIMIT 1""")
            print(f"{view_name} Exists!")  # noqa: T201
        except Exception:
            await db.execute_raw(spend_view_ddl(view_name))
            print(f"{view_name} Created!")  # noqa: T201


asyncio.run(check_view_exists())
