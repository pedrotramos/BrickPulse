"""The Databricks Apps inventory queries — one definition, three callers.

Same pattern as :mod:`genie_sql` and :mod:`spend_sql`: the SDP pipeline (writes the
snapshot), the live fallback (queries on demand before the first pipeline run), and
the tests all use these strings, so the numbers agree everywhere.

Why this is an INVENTORY and not a rule
---------------------------------------
This surface deliberately produces **no recommendations**, and that is a data
limitation rather than a product choice. Verified live (2026-08-06):

``system.access.audit`` with ``service_name='apps'`` contains **lifecycle actions
only** — ``createApp``, ``deployApp``, ``startApp``, ``stopApp``, ``updateApp``,
``deleteApp``, ``changeAppsAcl``, ``installTemplateApp``. There is **no request or
page-view log for apps anywhere in system tables**. So the question every idle-
resource rule needs to answer — "is anyone actually using this?" — is
*unanswerable* here. Compare ``idle_serving_endpoints``, which only exists because
``system.serving.endpoint_usage`` is a genuine per-request log.

That makes "nobody visits this app, shut it down" a guess dressed as a finding, and
the app's whole credibility rests on not doing that. So: we show what apps exist,
what they cost, and how they are configured, and we let a human decide. The fields
below are chosen to be the ones a human needs for that judgement — not to imply a
verdict.

**When an app usage log does appear**, this module is where the rule would start:
``apps_inventory_sql`` already computes ``days_billed``, ``compute_size`` and
``last_deploy_time``, which is most of a rightsizing/staleness candidate query. Add
the traffic join, then gate on it the way the serving rule gates on
``entity_matched`` — absence of request data must mean "unknown", never "idle".

Two billing facts that shape the SQL
------------------------------------
1. **Apps bill under a generic serverless SKU.**
   ``PREMIUM_ALL_PURPOSE_SERVERLESS_COMPUTE_<REGION>`` — nothing in the SKU name
   says "app". Selecting apps by SKU name is impossible; filter on
   ``billing_origin_product = 'APPS'``. (This is the same fact that made apps
   invisible in the spend breakdown until ``spend_sql`` was switched to group on
   the product column.)
2. **Apps are always-on while running, and mostly one size.** On a real account
   551 of 1,065 apps billed on every day of a 30-day window, and all but 17 ran
   ``MEDIUM``. There is no scale-to-zero to detect, so "days billed" is a
   lifecycle signal (was it ever stopped?), not an idleness signal.
"""

from __future__ import annotations

from brickpulse.environment import get_env

from brickpulse.rules.resolver import TableResolver

from .pricing_sql import price_expr, price_join
from .region import regional_workspaces_sql

#: Product key for Databricks Apps in ``billing.usage`` (billing fact 1 above).
APPS_PRODUCT = "APPS"


def _priced_cte(
    catalog: str,
    lookback_days: int,
    currency: str = "USD",
    *,
    resolver: "TableResolver | None" = None,
) -> str:
    """Apps usage joined to the list price in effect at the time of use.

    ``usage_quantity`` is DECIMAL in ``billing.usage``; cast to DOUBLE once here so
    every downstream SUM is a double — a Decimal aggregate cannot be written into
    the snapshot table's DOUBLE columns (Arrow refuses it), which is how the
    workspace-spend snapshot silently stayed empty once before.

    Reads ``{catalog}.billing.usage`` (via ``resolver``, same pattern as ``spend_sql``
    / ``genie_sql``), filtered to this metastore's region.
    """
    r = resolver or TableResolver.nested(catalog)
    usage_table = r("billing", "usage")
    scope_filter = f"\n    AND u.workspace_id IN ({regional_workspaces_sql(r.catalog)})"
    return f"""
  SELECT
    u.workspace_id,
    u.usage_metadata.app_id      AS app_id,
    u.usage_metadata.app_name    AS app_name,
    u.product_features.apps.compute_size AS compute_size,
    u.usage_date,
    CAST(u.usage_quantity AS DOUBLE) AS dbus,
    CAST(u.usage_quantity AS DOUBLE)
      * {price_expr()} AS spend_usd
  FROM {usage_table} u
  {price_join(r, currency)}
  WHERE u.billing_origin_product = '{APPS_PRODUCT}'
    -- date_sub is inclusive at BOTH ends, so `>= date_sub(current_date(), N)` spans
    -- N+1 distinct dates and an always-on app reports days_billed = N+1. Excluding
    -- today (which is also partial, and would understate the last day's spend) makes
    -- the window exactly N days, so "billed 30 of the last 30 days" is literally true.
    AND u.usage_date >= date_sub(current_date(), {lookback_days})
    AND u.usage_date < current_date()
    AND u.usage_metadata.app_id IS NOT NULL
    -- Scope: this metastore's region. See region.py.{scope_filter}
"""


def _lifecycle_cte(catalog: str, lookback_days: int) -> str:
    """Last deploy / start / stop per app name, from the audit log.

    The only usage-adjacent signal that exists for apps. Note the two different
    parameter keys, verified live: ``deployApp`` carries ``request_params.app_name``
    while ``startApp``/``stopApp`` carry ``request_params['name']``. Reading only
    ``app_name`` (the obvious guess) silently returns nothing for start/stop.

    Joined on NAME because the audit rows carry no app_id (``request_params.app_id``
    is populated 0 times). Names are unique per workspace, so the join is qualified
    by workspace_id.
    """
    return f"""
  SELECT
    workspace_id,
    COALESCE(request_params.app_name, request_params['name']) AS app_name,
    MAX(CASE WHEN action_name = 'deployApp' THEN event_time END) AS last_deploy_time,
    MAX(CASE WHEN action_name = 'startApp'  THEN event_time END) AS last_start_time,
    MAX(CASE WHEN action_name = 'stopApp'   THEN event_time END) AS last_stop_time,
    COUNT(CASE WHEN action_name = 'deployApp' THEN 1 END)        AS deploys,
    MAX(CASE WHEN action_name = 'deployApp' THEN user_identity.email END) AS last_deployed_by
  FROM {catalog}.access.audit
  WHERE service_name = 'apps'
    AND event_date >= date_sub(current_date(), {lookback_days})
    AND event_date < current_date()  -- closed-open window
    AND COALESCE(request_params.app_name, request_params['name']) IS NOT NULL
  GROUP BY 1, 2
"""


def apps_inventory_sql(
    catalog: str = "system",
    lookback_days: int = 30,
    currency: str = "USD",
    *,
    resolver: "TableResolver | None" = None,
) -> str:
    """One row per app: cost, configuration and lifecycle over the window.

    Deliberately NOT filtered by workspace — like the rule SQL and the spend query,
    scoping is applied by the caller, so one query serves both the account-wide
    precompute and a workspace-scoped live read.

    ``observed_days`` is included for consistency with the rules' minimum-window
    guard even though nothing suppresses on it here: a reader comparing a 2-day-old
    app against a 30-day-old one needs the same context a rule would.

    When ``FINOPS_NO_AUDIT`` is set (``access.audit`` not readable), the lifecycle
    CTE is dropped entirely and the lifecycle columns come back NULL — the app still
    shows every app's cost and configuration, just without deploy/start/stop
    history. Without the guard the whole query would fail on the missing table and
    the Apps tab would be empty.
    """

    no_audit = get_env("FINOPS_NO_AUDIT", "").lower() in ("1", "true", "yes", "on")
    priced = _priced_cte(
        catalog, lookback_days, currency,
        resolver=resolver,
    )
    if no_audit:
        return f"""
WITH priced AS ({priced}),
per_app AS (
  SELECT
    workspace_id, app_id,
    MAX(app_name) AS app_name,
    MAX(compute_size) AS compute_size,
    COUNT(DISTINCT compute_size) AS size_changes,
    COUNT(DISTINCT usage_date) AS days_billed,
    COUNT(DISTINCT usage_date) AS observed_days,
    MIN(usage_date) AS first_seen_date,
    MAX(usage_date) AS last_billed_date,
    SUM(dbus) AS dbus,
    SUM(spend_usd) AS spend_usd
  FROM priced
  GROUP BY 1, 2
)
SELECT
  a.workspace_id, a.app_id, a.app_name, a.compute_size, a.size_changes,
  a.days_billed, a.observed_days,
  CAST(a.first_seen_date AS STRING)  AS first_seen_date,
  CAST(a.last_billed_date AS STRING) AS last_billed_date,
  round(a.dbus, 2)                   AS dbus,
  round(a.spend_usd, 2)              AS spend_usd,
  round(a.spend_usd * 30.0 / {lookback_days}, 2) AS monthly_spend_usd,
  CAST(NULL AS STRING)    AS last_deploy_time,
  CAST(NULL AS STRING)    AS last_start_time,
  CAST(NULL AS STRING)    AS last_stop_time,
  0                       AS deploys,
  CAST(NULL AS STRING)    AS last_deployed_by
FROM per_app a
WHERE a.spend_usd > 0 OR a.dbus > 0
ORDER BY a.spend_usd DESC
"""
    return f"""
WITH priced AS ({priced}),
per_app AS (
  SELECT
    workspace_id,
    app_id,
    MAX(app_name) AS app_name,
    -- Size can change over the window; report the largest seen (what it costs at
    -- peak) alongside the distinct count so a resize is visible rather than hidden.
    MAX(compute_size) AS compute_size,
    COUNT(DISTINCT compute_size) AS size_changes,
    COUNT(DISTINCT usage_date) AS days_billed,
    COUNT(DISTINCT usage_date) AS observed_days,
    MIN(usage_date) AS first_seen_date,
    MAX(usage_date) AS last_billed_date,
    SUM(dbus) AS dbus,
    SUM(spend_usd) AS spend_usd
  FROM priced
  GROUP BY 1, 2
),
lifecycle AS ({_lifecycle_cte(catalog, lookback_days)})
SELECT
  a.workspace_id,
  a.app_id,
  a.app_name,
  a.compute_size,
  a.size_changes,
  a.days_billed,
  a.observed_days,
  CAST(a.first_seen_date AS STRING)   AS first_seen_date,
  CAST(a.last_billed_date AS STRING)  AS last_billed_date,
  round(a.dbus, 2)                    AS dbus,
  round(a.spend_usd, 2)               AS spend_usd,
  -- Normalized to a 30-day month so it is comparable with every other monthly
  -- figure in the app (see finops_rules.pricing.to_monthly).
  round(a.spend_usd * 30.0 / {lookback_days}, 2) AS monthly_spend_usd,
  CAST(l.last_deploy_time AS STRING)  AS last_deploy_time,
  CAST(l.last_start_time AS STRING)   AS last_start_time,
  CAST(l.last_stop_time AS STRING)    AS last_stop_time,
  COALESCE(l.deploys, 0)              AS deploys,
  l.last_deployed_by
FROM per_app a
LEFT JOIN lifecycle l
  ON a.workspace_id = l.workspace_id AND a.app_name = l.app_name
WHERE a.spend_usd > 0 OR a.dbus > 0
ORDER BY a.spend_usd DESC
"""


def apps_size_summary_sql(
    catalog: str = "system",
    lookback_days: int = 30,
    currency: str = "USD",
    *,
    resolver: "TableResolver | None" = None,
) -> str:
    """Spend and app count per compute size — the "how is the estate configured"
    rollup that makes a rightsizing conversation possible without claiming a finding.
    """
    priced = _priced_cte(
        catalog, lookback_days, currency,
        resolver=resolver,
    )
    return f"""
WITH priced AS ({priced})
SELECT
  workspace_id,
  COALESCE(compute_size, 'UNKNOWN') AS compute_size,
  COUNT(DISTINCT app_id)            AS apps,
  round(SUM(dbus), 2)               AS dbus,
  round(SUM(spend_usd), 2)          AS spend_usd,
  round(SUM(spend_usd) * 30.0 / {lookback_days}, 2) AS monthly_spend_usd
FROM priced
GROUP BY 1, 2
ORDER BY spend_usd DESC
"""


def apps_daily_sql(
    catalog: str = "system",
    lookback_days: int = 30,
    currency: str = "USD",
    *,
    resolver: "TableResolver | None" = None,
) -> str:
    """Daily apps spend and active-app count — the adoption/growth trend."""
    priced = _priced_cte(
        catalog, lookback_days, currency,
        resolver=resolver,
    )
    return f"""
WITH priced AS ({priced})
SELECT
  workspace_id,
  CAST(usage_date AS STRING) AS usage_date,
  COUNT(DISTINCT app_id)     AS apps,
  round(SUM(dbus), 2)        AS dbus,
  round(SUM(spend_usd), 2)   AS spend_usd
FROM priced
GROUP BY 1, 2
ORDER BY usage_date
"""
