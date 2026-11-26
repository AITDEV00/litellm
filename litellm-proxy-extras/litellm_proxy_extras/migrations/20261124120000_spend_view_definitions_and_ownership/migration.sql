-- Regenerate with: python scripts/render_spend_view_migration.py
-- Source of truth: litellm/proxy/db/spend_view_definitions.py
--
-- Defines the seven spend aggregation views and refuses to touch an object the
-- application database role does not already own. View DDL and metadata only:
-- no row rewrites, no ACL mutation, no SET ROLE.
SET LOCAL lock_timeout = '10s';
SET LOCAL idle_in_transaction_session_timeout = '5s';

DO $$
DECLARE
    view_name text;
    object_owner text;
    object_kind "char";
    current_role text := current_user;
BEGIN
    -- MonthlyGlobalSpend: correct when absent or already owned by current_user
    view_name := 'MonthlyGlobalSpend';
    SELECT pg_get_userbyid(c.relowner), c.relkind
      INTO object_owner, object_kind
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = current_schema() AND c.relname = view_name;
    IF object_kind IS NULL THEN
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
    ELSIF object_kind <> 'v' THEN
        RAISE EXCEPTION
            'spend view % is a relkind %s object, expected a view (relkind v); refusing to alter it',
            view_name, object_kind;
    ELSIF object_owner <> current_role THEN
        RAISE EXCEPTION
            'spend view % is owned by %, expected %; refusing to borrow another owner. '
            'Run the operator repair workflow (oicm-litellm-layer/docs/operations/runbooks/spend-view-legacy-repair.md) as the database superuser, then re-run migrations',
            view_name, object_owner, current_role;
    END IF;
    -- Last30dKeysBySpend: correct when absent or already owned by current_user
    view_name := 'Last30dKeysBySpend';
    SELECT pg_get_userbyid(c.relowner), c.relkind
      INTO object_owner, object_kind
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = current_schema() AND c.relname = view_name;
    IF object_kind IS NULL THEN
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
    ELSIF object_kind <> 'v' THEN
        RAISE EXCEPTION
            'spend view % is a relkind %s object, expected a view (relkind v); refusing to alter it',
            view_name, object_kind;
    ELSIF object_owner <> current_role THEN
        RAISE EXCEPTION
            'spend view % is owned by %, expected %; refusing to borrow another owner. '
            'Run the operator repair workflow (oicm-litellm-layer/docs/operations/runbooks/spend-view-legacy-repair.md) as the database superuser, then re-run migrations',
            view_name, object_owner, current_role;
    END IF;
    -- Last30dModelsBySpend: correct when absent or already owned by current_user
    view_name := 'Last30dModelsBySpend';
    SELECT pg_get_userbyid(c.relowner), c.relkind
      INTO object_owner, object_kind
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = current_schema() AND c.relname = view_name;
    IF object_kind IS NULL THEN
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
    ELSIF object_kind <> 'v' THEN
        RAISE EXCEPTION
            'spend view % is a relkind %s object, expected a view (relkind v); refusing to alter it',
            view_name, object_kind;
    ELSIF object_owner <> current_role THEN
        RAISE EXCEPTION
            'spend view % is owned by %, expected %; refusing to borrow another owner. '
            'Run the operator repair workflow (oicm-litellm-layer/docs/operations/runbooks/spend-view-legacy-repair.md) as the database superuser, then re-run migrations',
            view_name, object_owner, current_role;
    END IF;
    -- MonthlyGlobalSpendPerKey: correct when absent or already owned by current_user
    view_name := 'MonthlyGlobalSpendPerKey';
    SELECT pg_get_userbyid(c.relowner), c.relkind
      INTO object_owner, object_kind
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = current_schema() AND c.relname = view_name;
    IF object_kind IS NULL THEN
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
    ELSIF object_kind <> 'v' THEN
        RAISE EXCEPTION
            'spend view % is a relkind %s object, expected a view (relkind v); refusing to alter it',
            view_name, object_kind;
    ELSIF object_owner <> current_role THEN
        RAISE EXCEPTION
            'spend view % is owned by %, expected %; refusing to borrow another owner. '
            'Run the operator repair workflow (oicm-litellm-layer/docs/operations/runbooks/spend-view-legacy-repair.md) as the database superuser, then re-run migrations',
            view_name, object_owner, current_role;
    END IF;
    -- MonthlyGlobalSpendPerUserPerKey: correct when absent or already owned by current_user
    view_name := 'MonthlyGlobalSpendPerUserPerKey';
    SELECT pg_get_userbyid(c.relowner), c.relkind
      INTO object_owner, object_kind
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = current_schema() AND c.relname = view_name;
    IF object_kind IS NULL THEN
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
    ELSIF object_kind <> 'v' THEN
        RAISE EXCEPTION
            'spend view % is a relkind %s object, expected a view (relkind v); refusing to alter it',
            view_name, object_kind;
    ELSIF object_owner <> current_role THEN
        RAISE EXCEPTION
            'spend view % is owned by %, expected %; refusing to borrow another owner. '
            'Run the operator repair workflow (oicm-litellm-layer/docs/operations/runbooks/spend-view-legacy-repair.md) as the database superuser, then re-run migrations',
            view_name, object_owner, current_role;
    END IF;
    -- DailyTagSpend: drop/recreate when absent or owned by current_user
    view_name := 'DailyTagSpend';
    SELECT pg_get_userbyid(c.relowner), c.relkind
      INTO object_owner, object_kind
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = current_schema() AND c.relname = view_name;
    IF object_kind IS NULL THEN
CREATE OR REPLACE VIEW "DailyTagSpend" AS
SELECT
jsonb_array_elements_text(request_tags) AS individual_request_tag,
DATE(s."startTime") AS spend_date,
COUNT(*) AS log_count,
SUM(spend) AS total_spend
FROM "LiteLLM_SpendLogs" s
GROUP BY individual_request_tag, DATE(s."startTime");
    ELSIF object_kind <> 'v' THEN
        RAISE EXCEPTION
            'spend view % is a relkind %s object, expected a view (relkind v); refusing to alter it',
            view_name, object_kind;
    ELSIF object_owner <> current_role THEN
        RAISE EXCEPTION
            'spend view % is owned by %, expected %; refusing to borrow another owner. '
            'Run the operator repair workflow (oicm-litellm-layer/docs/operations/runbooks/spend-view-legacy-repair.md) as the database superuser, then re-run migrations',
            view_name, object_owner, current_role;
    ELSE
        DROP VIEW "DailyTagSpend";
CREATE OR REPLACE VIEW "DailyTagSpend" AS
SELECT
jsonb_array_elements_text(request_tags) AS individual_request_tag,
DATE(s."startTime") AS spend_date,
COUNT(*) AS log_count,
SUM(spend) AS total_spend
FROM "LiteLLM_SpendLogs" s
GROUP BY individual_request_tag, DATE(s."startTime");
    END IF;
    -- Last30dTopEndUsersSpend: correct when absent or already owned by current_user
    view_name := 'Last30dTopEndUsersSpend';
    SELECT pg_get_userbyid(c.relowner), c.relkind
      INTO object_owner, object_kind
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = current_schema() AND c.relname = view_name;
    IF object_kind IS NULL THEN
CREATE VIEW "Last30dTopEndUsersSpend" AS
SELECT end_user, COUNT(*) AS total_events, SUM(spend) AS total_spend
FROM "LiteLLM_SpendLogs"
WHERE end_user <> '' AND end_user <> user
AND "startTime" >= CURRENT_DATE - INTERVAL '30 days'
GROUP BY end_user
ORDER BY total_spend DESC
LIMIT 100;
    ELSIF object_kind <> 'v' THEN
        RAISE EXCEPTION
            'spend view % is a relkind %s object, expected a view (relkind v); refusing to alter it',
            view_name, object_kind;
    ELSIF object_owner <> current_role THEN
        RAISE EXCEPTION
            'spend view % is owned by %, expected %; refusing to borrow another owner. '
            'Run the operator repair workflow (oicm-litellm-layer/docs/operations/runbooks/spend-view-legacy-repair.md) as the database superuser, then re-run migrations',
            view_name, object_owner, current_role;
    END IF;
END
$$;
