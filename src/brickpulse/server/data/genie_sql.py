"""The Genie-cost queries — one definition, three callers.

Same pattern as :mod:`spend_sql`: the SDP pipeline (writes the snapshot), the live
fallback (queries on demand before the first pipeline run), and the tests all use
these strings, so the numbers are computed identically everywhere.

Why Genie needs its own SQL rather than riding on the spend query
-----------------------------------------------------------------
Genie cost is attributed to **people and spaces**, not to compute resources, so
none of the existing per-workspace/per-cluster shapes fit. Three facts about the
billing data drive everything below (all verified live, 2026-08-05):

1. **The free tier is a separate SKU, not a discount.** ``GENIE_FREE_USAGE`` has
   no row in ``billing.list_prices`` at all, so it prices to exactly $0 — the
   free/paid split is a fact we read, not a threshold we guess.
2. **Paid Genie does not say "genie" anywhere in its SKU name.** It bills as
   ``PREMIUM_SERVERLESS_REAL_TIME_INFERENCE_<REGION>`` (the inference SKU). A
   ``sku_name LIKE '%GENIE%'`` filter silently returns free usage only and makes
   paid spend look like zero — always filter on ``billing_origin_product``.
3. **``usage_metadata.genie.agent_id`` is populated for ``GENIE_AGENTS`` only.**
   Genie Code and Genie One emit no space id, so per-space analysis legitimately
   covers a subset of spend. :func:`genie_space_sql` filters to that surface
   rather than lumping everything into a bogus "unknown space" bucket.

Deliberately NOT in here: budget policies. ``usage_metadata.budget_policy_id`` is
NULL on every Genie row, so there is nothing to read — see the note in
``models/genie.py`` and the honest "not attributed" panel in the UI.
"""

from __future__ import annotations

from brickpulse.rules.resolver import TableResolver

from .pricing_sql import price_expr, price_join
from .region import regional_workspaces_sql

#: Product key for every Genie surface in ``billing.usage``. See fact 2 above:
#: this, not the SKU name, is the only reliable way to select Genie usage.
GENIE_PRODUCT = "GENIE"

#: The zero-priced free-allowance SKU (fact 1).
FREE_SKU = "GENIE_FREE_USAGE"


def _priced_cte(
    catalog: str,
    lookback_days: int,
    currency: str = "USD",
    *,
    resolver: "TableResolver | None" = None,
) -> str:
    """Genie usage joined to the list price in effect at the time of use.

    ``usage_quantity`` is DECIMAL in ``billing.usage``; cast to DOUBLE once here so
    every downstream SUM is a double. A Decimal aggregate cannot be written into
    the snapshot table's DOUBLE columns (Arrow refuses the conversion), which is
    exactly how the workspace-spend snapshot silently stayed empty once before.

    Reads ``{catalog}.billing.usage`` (via ``resolver``, same pattern as
    ``spend_sql``), filtered to this metastore's region.
    """
    r = resolver or TableResolver.nested(catalog)
    usage_table = r("billing", "usage")
    scope_filter = f"\n    AND u.workspace_id IN ({regional_workspaces_sql(r.catalog)})"
    return f"""
  SELECT
    u.workspace_id,
    COALESCE(u.identity_metadata.run_as, 'unknown')            AS principal,
    COALESCE(u.usage_metadata.genie.surface, 'UNKNOWN')        AS surface,
    COALESCE(u.usage_metadata.genie.channel, 'UNKNOWN')        AS channel,
    u.usage_metadata.genie.agent_id                            AS space_id,
    u.sku_name = '{FREE_SKU}'                                  AS is_free,
    u.usage_date,
    CAST(u.usage_quantity AS DOUBLE)                           AS dbus,
    CAST(u.usage_quantity AS DOUBLE)
      * {price_expr()}        AS spend_usd
  FROM {usage_table} u
  {price_join(r, currency)}
  WHERE u.billing_origin_product = '{GENIE_PRODUCT}'
    -- Sum ALL record types, do not filter to ORIGINAL. In billing.usage a
    -- correction is posted as an additive triple: the ORIGINAL (+X), a
    -- RETRACTION that cancels it (-X), and a RESTATEMENT carrying the corrected
    -- value (+Y). Summing across all three nets to Y and cannot double-count —
    -- an ORIGINAL-only filter keeps X and silently drops the -X that corrects it.
    -- This is exactly how the Genie Spaces / Genie One waivers were posted:
    -- retractions to zero (RESTATEMENT = 0). Filtering to ORIGINAL would report
    -- the waived pre-credit amount as live spend (verified live, 2026-08-14).
    -- Closed-open window: exclude the partial current day (same N complete
    -- days as the rules and Total Spend).
    AND u.usage_date >= date_sub(current_date(), {lookback_days})
    AND u.usage_date < current_date()
    -- Scope: this metastore's region (billing.usage is account-global). See
    -- region.py.{scope_filter}
"""


def genie_user_sql(
    catalog: str = "system",
    lookback_days: int = 30,
    currency: str = "USD",
    *,
    resolver: "TableResolver | None" = None,
) -> str:
    """Per-user Genie usage: the free/paid split and the spender leaderboard.

    One row per (workspace, principal). Rolled up in Python so the same rows
    answer "who spends most", "how many people never leave the free tier", and
    the per-workspace totals — rather than shipping three near-identical queries.

    Not filtered by workspace: like the rule SQL, scoping is applied by the caller
    (the data layer for live reads, not at all for the account-wide pipeline).
    """
    priced = _priced_cte(
        catalog, lookback_days, currency,
        resolver=resolver,
    )
    return f"""
WITH priced AS ({priced})
SELECT
  workspace_id,
  principal,
  SUM(CASE WHEN is_free THEN dbus ELSE 0 END)       AS free_dbus,
  SUM(CASE WHEN NOT is_free THEN dbus ELSE 0 END)   AS paid_dbus,
  SUM(spend_usd)                                    AS spend_usd,
  COUNT(DISTINCT usage_date)                        AS active_days,
  -- Which surfaces this person actually used, biggest first — turns a name on the
  -- leaderboard into an explanation of what they were doing.
  to_json(
    array_sort(
      collect_list(struct(surface, channel, round(dbus, 2) AS dbus)),
      (l, r) -> CASE WHEN l.dbus > r.dbus THEN -1 WHEN l.dbus < r.dbus THEN 1 ELSE 0 END
    )
  )                                                 AS by_surface,
  MAX(usage_date)                                   AS last_active_date
FROM priced
GROUP BY workspace_id, principal
"""


def genie_surface_sql(
    catalog: str = "system",
    lookback_days: int = 30,
    currency: str = "USD",
    *,
    resolver: "TableResolver | None" = None,
) -> str:
    """Spend by surface x channel x tier — where the Genie bill actually comes from.

    Worth its own query because the answer is usually counter-intuitive: most of
    the *users* are on Genie Spaces while most of the *paid spend* is Genie Code.
    """
    priced = _priced_cte(
        catalog, lookback_days, currency,
        resolver=resolver,
    )
    return f"""
WITH priced AS ({priced})
SELECT
  workspace_id,
  surface,
  channel,
  is_free,
  SUM(dbus)                    AS dbus,
  SUM(spend_usd)               AS spend_usd,
  COUNT(DISTINCT principal)    AS users,
  COUNT(DISTINCT usage_date)   AS active_days
FROM priced
GROUP BY workspace_id, surface, channel, is_free
"""


def genie_space_sql(
    catalog: str = "system",
    lookback_days: int = 30,
    currency: str = "USD",
    *,
    resolver: "TableResolver | None" = None,
) -> str:
    """Cost per Genie Space.

    Restricted to rows that carry a space id (fact 3: ``GENIE_AGENTS`` only).
    Genie Code / Genie One spend is real but has no space to attribute it to, so
    it is excluded here and reported under the surface breakdown instead.
    """
    priced = _priced_cte(
        catalog, lookback_days, currency,
        resolver=resolver,
    )
    return f"""
WITH priced AS ({priced})
SELECT
  workspace_id,
  space_id,
  SUM(dbus)                                       AS dbus,
  SUM(spend_usd)                                  AS spend_usd,
  COUNT(DISTINCT principal)                       AS users,
  COUNT(DISTINCT usage_date)                      AS active_days,
  SUM(CASE WHEN channel = 'API' THEN dbus ELSE 0 END) AS api_dbus,
  MAX(usage_date)                                 AS last_active_date
FROM priced
WHERE space_id IS NOT NULL
GROUP BY workspace_id, space_id
"""


def genie_daily_sql(
    catalog: str = "system",
    lookback_days: int = 30,
    currency: str = "USD",
    *,
    resolver: "TableResolver | None" = None,
) -> str:
    """Daily Genie spend and active users — the adoption trend."""
    priced = _priced_cte(
        catalog, lookback_days, currency,
        resolver=resolver,
    )
    return f"""
WITH priced AS ({priced})
SELECT
  workspace_id,
  usage_date,
  SUM(CASE WHEN is_free THEN dbus ELSE 0 END)     AS free_dbus,
  SUM(CASE WHEN NOT is_free THEN dbus ELSE 0 END) AS paid_dbus,
  SUM(spend_usd)                                  AS spend_usd,
  COUNT(DISTINCT principal)                       AS users
FROM priced
GROUP BY workspace_id, usage_date
"""
