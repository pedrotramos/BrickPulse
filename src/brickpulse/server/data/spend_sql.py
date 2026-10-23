"""The per-workspace total-spend query — one definition, three callers.

Total workspace spend is needed in three places: the precompute job (writes the
snapshot table), the live fallback (queries on demand when no snapshot exists),
and the tests. Keeping the SQL here means the number is computed identically in
all of them, the way the rule engine keeps one SQL template per rule.

The shape mirrors the cost CTEs in the rules: ``billing.usage`` joined to the
``list_prices`` row in effect at the time of use, so the figure is real list-price
dollars rather than raw DBUs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .pricing_sql import contract_price_join, contract_rate_expr, price_expr, price_join
from .region import regional_workspaces_sql

if TYPE_CHECKING:
    from brickpulse.rules.resolver import TableResolver

#: Coarse product grouping for the "where does the money go" breakdown.
#:
#: Keyed on ``billing_origin_product`` — the authoritative product dimension — and
#: only on ``sku_name`` for the serverless/classic distinction *within* a product,
#: which the SKU name is the only place that carries.
#:
#: This used to bucket purely on ``sku_name`` substrings, which silently misfiled
#: whole products: **Databricks Apps bill under
#: ``PREMIUM_ALL_PURPOSE_SERVERLESS_COMPUTE_<REGION>``**, a SKU whose name contains
#: no hint of "app" at all, so apps landed in a catch-all "Serverless (other)"
#: bucket together with Vector Search, Lakebase and Model Serving. On a real account
#: that bucket was the single largest line item ($272k/30d, 39% of spend) with no
#: label explaining it — the exact opposite of "the total is explainable rather than
#: just big". Substring matching on SKUs cannot fix this, because the product simply
#: is not in the SKU name.
#:
#: Order still matters (first match wins) for the SQL/DLT/JOBS arms, where the
#: serverless variant must be tested before the classic one.
_SKU_GROUP_SQL = """
    CASE
      WHEN billing_origin_product = 'SQL' AND sku_name LIKE '%SERVERLESS%'
        THEN 'SQL Warehouse (serverless)'
      WHEN billing_origin_product = 'SQL' AND sku_name LIKE '%SQL_PRO%'
        THEN 'SQL Warehouse (pro)'
      WHEN billing_origin_product = 'SQL' THEN 'SQL Warehouse (classic)'
      WHEN billing_origin_product = 'DLT' AND sku_name LIKE '%SERVERLESS%'
        THEN 'Pipelines (serverless)'
      WHEN billing_origin_product = 'DLT' THEN 'Pipelines (DLT)'
      WHEN billing_origin_product = 'JOBS' AND sku_name LIKE '%SERVERLESS%'
        THEN 'Jobs (serverless)'
      WHEN billing_origin_product = 'JOBS' THEN 'Jobs compute'
      WHEN billing_origin_product = 'APPS' THEN 'Databricks Apps'
      WHEN billing_origin_product = 'VECTOR_SEARCH' THEN 'Vector Search'
      WHEN billing_origin_product = 'LAKEBASE' THEN 'Lakebase'
      WHEN billing_origin_product IN ('DATABASE', 'LAKEHOUSE_REAL_TIME')
        THEN 'Lakebase / Database'
      WHEN billing_origin_product = 'MODEL_SERVING' THEN 'Model serving'
      WHEN billing_origin_product = 'GENIE' THEN 'AI/BI Genie'
      WHEN billing_origin_product = 'ALL_PURPOSE' THEN 'All-purpose compute'
      WHEN billing_origin_product = 'INTERACTIVE' THEN 'Notebooks (serverless)'
      WHEN billing_origin_product IN (
             'AI_FUNCTIONS', 'AGENT_BRICKS', 'AGENT_EVALUATION', 'SUPERVISOR_AGENT',
             'AI_GATEWAY', 'AI_RUNTIME'
           ) THEN 'AI functions & agents'
      WHEN billing_origin_product IN (
             'DATA_QUALITY_MONITORING', 'DATA_CLASSIFICATION',
             'PREDICTIVE_OPTIMIZATION', 'FINE_GRAINED_ACCESS_CONTROL'
           ) THEN 'Governance & optimization'
      WHEN billing_origin_product = 'LAKEFLOW_CONNECT' THEN 'Lakeflow Connect'
      WHEN billing_origin_product IN ('NETWORKING', 'DEFAULT_STORAGE')
        THEN 'Networking & storage'
      -- A product Databricks added after this list was written. Named rather than
      -- silently pooled, so a new surface shows up as itself the first time it bills.
      ELSE COALESCE(
             initcap(replace(CAST(billing_origin_product AS STRING), '_', ' ')),
             'Other'
           )
    END
"""


def workspace_spend_sql(
    resolver: "TableResolver",
    lookback_days: int = 30,
    currency: str = "USD",
    *,
    contract_prices: bool = False,
    cloud_costs: bool = False,
) -> str:
    """Total billed spend per workspace over the window, with a SKU breakdown.

    ``contract_prices`` / ``cloud_costs`` (from ``finops_rules.price_basis``'s
    capability probe) add the contract-rate and cloud-VM
    views. The columns are ALWAYS emitted — NULL when the flag is off — so the
    snapshot schema does not depend on what an account happens to have:

    * ``total_spend_contract_usd`` — BLENDED: contract rate where the usage has an
      account_prices match, list otherwise. Never shown without
      ``contract_priced_share`` (the list-$ share of spend that had a contract
      price), so a partly-covered total cannot pass for a fully contract-priced one.
    * ``total_cloud_cost_usd`` — classic cloud VM cost (billing.cloud_infra_cost),
      a separate layer that is never added into the Databricks dollars.

    Takes a :class:`~finops_rules.resolver.TableResolver` (like the rule SQL): it reads
    ``{catalog}.billing.usage`` and is filtered to this metastore's region
    (``regional_workspaces_sql``).

    Deliberately NOT filtered by persona/workspace here beyond the region: like the
    rule SQL, per-request workspace narrowing is applied by the caller.
    """
    usage_table = resolver("billing", "usage")
    if contract_prices:
        list_amt = f"CAST(u.usage_quantity AS DOUBLE) * {price_expr()}"
        contract_cols = f""",
    CASE WHEN ap.sku_name IS NOT NULL
         THEN CAST(u.usage_quantity AS DOUBLE) * {contract_rate_expr()}
         ELSE {list_amt} END AS contract_spend_usd,
    CASE WHEN ap.sku_name IS NOT NULL THEN {list_amt} ELSE 0 END AS contract_covered_list_usd"""
        contract_join = "\n  " + contract_price_join(resolver, currency)
        contract_totals = """,
         SUM(contract_spend_usd) AS total_spend_contract_usd,
         SUM(contract_covered_list_usd) / NULLIF(SUM(spend_usd), 0) AS contract_priced_share"""
        contract_select = """
  round(t.total_spend_contract_usd, 2) AS total_spend_contract_usd,
  round(t.contract_priced_share, 4) AS contract_priced_share,"""
    else:
        contract_cols = contract_join = contract_totals = ""
        contract_select = """
  CAST(NULL AS DOUBLE) AS total_spend_contract_usd,
  CAST(NULL AS DOUBLE) AS contract_priced_share,"""
    if cloud_costs:
        cloud_table = resolver("billing", "cloud_infra_cost")
        cloud_cte = f""",
cloud AS (
  SELECT ci.workspace_id, SUM(CAST(ci.cost AS DOUBLE)) AS total_cloud_cost_usd
  FROM {cloud_table} ci
  WHERE ci.usage_date >= date_sub(current_date(), {lookback_days})
    AND ci.usage_date < current_date()
    AND ci.currency_code = '{currency}'
  GROUP BY 1
)"""
        cloud_join = "\nLEFT JOIN cloud cl USING (workspace_id)"
        cloud_select = "\n  round(cl.total_cloud_cost_usd, 2) AS total_cloud_cost_usd,"
    else:
        cloud_cte = cloud_join = ""
        cloud_select = "\n  CAST(NULL AS DOUBLE) AS total_cloud_cost_usd,"
    # Workspace scope: this metastore's region.
    region_filter = f"\n    AND u.workspace_id IN ({regional_workspaces_sql(resolver.catalog)})"
    return f"""
WITH priced AS (
  SELECT
    u.workspace_id,
    u.sku_name,
    -- Carried for the day-by-day trend (the `daily` CTE below).
    u.usage_date,
    -- The product dimension the breakdown groups on. Carried through the CTE
    -- because sku_name alone cannot identify the product (see _SKU_GROUP_SQL).
    u.billing_origin_product,
    -- usage_quantity is DECIMAL in billing.usage. Cast once here so every
    -- downstream SUM() is a DOUBLE: a Decimal aggregate cannot be written into the
    -- spend table's DOUBLE column (Arrow refuses the conversion), which silently
    -- skipped the whole spend snapshot until this was fixed.
    CAST(u.usage_quantity AS DOUBLE) AS usage_quantity,
    -- Unpriced SKUs (e.g. free-tier GENIE_FREE_USAGE) contribute $0 but their DBUs
    -- still count toward total_dbus: this is a spend TOTAL, and dropping usage
    -- would understate it. Rules make the opposite trade-off — see pricing_sql.
    CAST(u.usage_quantity AS DOUBLE)
      * {price_expr()} AS spend_usd{contract_cols}
  FROM {usage_table} u
  {price_join(resolver, currency)}{contract_join}
  -- Closed-open window [today-N, today): exclude the partial current day so
  -- Total Spend uses the SAME N complete days the rules do, and the two reconcile.
  WHERE u.usage_date >= date_sub(current_date(), {lookback_days})
    AND u.usage_date < current_date()
    -- Regional scope: billing.usage is account-global, so keep only workspaces this
    -- metastore has data for — the same set the rules analyze. See region.py.{region_filter}
),
grouped AS (
  SELECT workspace_id,
         {_SKU_GROUP_SQL} AS sku_group,
         SUM(spend_usd) AS spend_usd,
         SUM(usage_quantity) AS dbus
  FROM priced
  GROUP BY 1, 2
),
by_sku AS (
  SELECT workspace_id,
         to_json(
           array_sort(
             collect_list(struct(sku_group, round(spend_usd, 2) AS spend_usd,
                                 round(dbus, 2) AS dbus)),
             (l, r) -> CASE WHEN l.spend_usd > r.spend_usd THEN -1
                            WHEN l.spend_usd < r.spend_usd THEN 1 ELSE 0 END
           )
         ) AS by_sku
  FROM grouped
  GROUP BY 1
),
totals AS (
  SELECT workspace_id,
         SUM(spend_usd) AS total_spend_usd,
         SUM(usage_quantity) AS total_dbus,
         SUM(CASE WHEN sku_name LIKE '%SERVERLESS%' THEN spend_usd ELSE 0 END)
           AS serverless_spend_usd,
         SUM(CASE WHEN sku_name LIKE '%SERVERLESS%' THEN 0 ELSE spend_usd END)
           AS classic_spend_usd{contract_totals}
  FROM priced
  GROUP BY 1
),
-- Day-by-day spend, so the UI can answer "is spend moving the wrong way?" — which
-- a single window total cannot. Carried as a JSON array in ONE column (like
-- by_sku) rather than as extra rows: this table's grain is one row per workspace,
-- and a per-day grain would change the contract every existing reader depends on.
daily AS (
  SELECT workspace_id,
         to_json(
           array_sort(
             collect_list(struct(
               CAST(usage_date AS STRING) AS usage_date,
               round(spend_usd, 2) AS spend_usd,
               round(dbus, 2) AS dbus
             )),
             -- Ascending by date: a chart reads left-to-right, and sorting in SQL
             -- means neither the app nor the pipeline has to re-sort it.
             (l, r) -> CASE WHEN l.usage_date < r.usage_date THEN -1
                            WHEN l.usage_date > r.usage_date THEN 1 ELSE 0 END
           )
         ) AS by_day
  FROM (
    SELECT workspace_id, usage_date,
           SUM(spend_usd) AS spend_usd,
           SUM(usage_quantity) AS dbus
    FROM priced
    GROUP BY 1, 2
  )
  GROUP BY 1
){cloud_cte}
SELECT
  t.workspace_id,
  {lookback_days} AS lookback_days,
  round(t.total_spend_usd, 2) AS total_spend_usd,
  -- Normalized to a 30-day month so it is comparable with the savings figures,
  -- which every rule reports monthly (see finops_rules.pricing.to_monthly).
  round(t.total_spend_usd * 30.0 / {lookback_days}, 2) AS monthly_spend_usd,
  -- Explicit DOUBLE: usage_quantity is DECIMAL in billing.usage, so SUM() yields a
  -- Decimal that Arrow refuses to write into the table's DOUBLE column.
  CAST(t.total_dbus AS DOUBLE) AS total_dbus,
  round(t.serverless_spend_usd, 2) AS serverless_spend_usd,
  round(t.classic_spend_usd, 2) AS classic_spend_usd,{contract_select}{cloud_select}
  s.by_sku,
  d.by_day
FROM totals t
LEFT JOIN by_sku s USING (workspace_id)
LEFT JOIN daily d USING (workspace_id){cloud_join}
WHERE t.total_spend_usd > 0
"""
