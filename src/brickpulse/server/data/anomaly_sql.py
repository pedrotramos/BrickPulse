"""Cost anomaly queries — one definition, three callers.

Same pattern as :mod:`apps_sql` / :mod:`genie_sql`: the SDP pipeline (writes the
snapshot), the live fallback (queries on demand before the first pipeline run) and
the tests all use these strings, so snapshot and live agree by construction.

Why this is an analysis tab and not a rule
------------------------------------------
An anomaly is not a saving. A spike can be a legitimate backfill, a quarter-end
batch or a new product going live; only a human who knows the workload can say
which. So nothing here carries ``monthly_savings_usd``, a priority or an NBA score,
and nothing contributes to any savings total. What the tab shows is "this cost moved
away from its own recent normal, by this much, and this identity drove it".

Method
-----------------
* **Grain.** Daily list-priced cost per key, at two grains:
  ``sku`` = (workspace, ``billing_origin_product``, ``sku_name``) — covers every
  dollar; ``resource`` = (workspace, resource type, resource id) — the thing to go
  and look at. A usage row belongs to exactly one key per grain, so excess sums
  within a grain never double count (they DO overlap across grains).
* **Resource key priority.** ``job_id`` before ``cluster_id``: a job cluster gets a
  fresh ``cluster_id`` on every run, so keying jobs by cluster would report every
  run as a "new spender". Then pipeline, warehouse, app, endpoint (serving vs vector
  search by product), Lakebase project, and finally all-purpose cluster.
* **Baseline.** The 28 days before the last 7 — the evaluation window is excluded,
  so a step change cannot drag its own baseline up. Days with no usage count as
  $0 (the series is densified), otherwise a resource that runs twice a month would
  get the median of its busy days as "normal".
* **Robust z.** ``(cost − median) / σ`` with
  ``σ = max(1.4826·MAD, (p75 − median)/0.6745, 10 % of median, $1)``. MAD alone is 0
  for a flat always-on resource (any cent of drift becomes infinitely anomalous) AND
  for an intermittent one whose median day is $0 — a job that runs every third day
  would have every run flagged. The upper-quartile term is the robust spread of the
  busy side, which absorbs that rhythm (verified live: it drops a periodic $3k/run
  scraping job while keeping a warehouse that went from ~$450 to ~$7k a day). A day is flagged when
  ``z > z_threshold`` **and** the excess is at least
  ``max(min_delta_usd, min_delta_share × workspace median daily spend)`` — both
  must hold, so a large workspace's normal jitter isn't reported as an event.
* **Weekly rhythm.** The excess is measured over the higher of the overall median
  and the same-weekday median, and must clear the floor against both. Without this
  a weekly job — $0 median, busy one day in seven — is "anomalous" every week.
* **Types.** ``new_spender`` — no baseline spend at all, and a day at or above the
  floor; ``step_change`` — ≥ ``step_min_days`` consecutive flagged days;
  ``spike`` — anything else (isolated flagged days).
* **Today is excluded** (partial day). ``usage_date < current_date()``.
* **Who.** ``identity_metadata.run_as`` / ``owned_by`` of the largest usage record
  on the peak day — the identity that drove the cost, not an exhaustive list.

Plan shape
----------
Spark inlines CTEs, so a CTE read N times is planned N times, and a CTE that reads
one read N times multiplies. The first version grouped baseline statistics and joined
them back to the daily rows, and filtered the name lookups with
``IN (SELECT … FROM anomalous)`` — 524 scans of ``billing.usage`` in a 5.5 MB plan.
A warehouse de-duplicated its way through in ~2 min; the SDP pipeline sat in STARTING
for 35 min. Every statistic is now a window aggregate over ONE densified set, and one
GROUP BY produces the per-key row: 8 scans, and the same 149 anomalies on live data
(~35 s). ``test_no_intermediate_step_is_read_more_than_twice`` holds that line.

Prices are LIST (``pricing_sql.price_expr``); the tab says so. The contract basis
(plan A6) is not applied here yet — anomalies are about relative movement, which a
flat contract discount does not change.
"""

from __future__ import annotations

from brickpulse.rules.resolver import TableResolver

from .pricing_sql import price_expr, price_join
from .region import regional_workspaces_sql

#: Baseline length (days) — the trailing window BEFORE the evaluation window.
BASELINE_DAYS = 28
#: Evaluation window (days) — the recent days scored against the baseline.
EVAL_DAYS = 7
#: Robust z-score above which a day is anomalous.
Z_THRESHOLD = 4.0
#: Minimum excess over baseline, in USD per day.
MIN_DELTA_USD = 200.0
#: ...and at least this share of the workspace's median daily spend.
MIN_DELTA_SHARE = 0.03
#: Consecutive flagged days that make a step change rather than a spike.
STEP_MIN_DAYS = 3

#: SQL for the resource key, most specific first. See "Resource key priority".
_RESOURCE_TYPE = """CASE
      WHEN u.usage_metadata.job_id IS NOT NULL THEN 'job'
      WHEN u.usage_metadata.dlt_pipeline_id IS NOT NULL THEN 'pipeline'
      WHEN u.usage_metadata.warehouse_id IS NOT NULL THEN 'warehouse'
      WHEN u.usage_metadata.app_id IS NOT NULL THEN 'app'
      WHEN u.usage_metadata.endpoint_name IS NOT NULL
        THEN CASE WHEN u.billing_origin_product = 'VECTOR_SEARCH'
                  THEN 'vector_search_endpoint' ELSE 'serving_endpoint' END
      WHEN u.usage_metadata.project_id IS NOT NULL THEN 'lakebase_project'
      WHEN u.usage_metadata.cluster_id IS NOT NULL THEN 'cluster'
    END"""
_RESOURCE_ID = """COALESCE(
      u.usage_metadata.job_id, u.usage_metadata.dlt_pipeline_id,
      u.usage_metadata.warehouse_id, u.usage_metadata.app_id,
      u.usage_metadata.endpoint_name, u.usage_metadata.project_id,
      u.usage_metadata.cluster_id)"""
#: The name billing carries for the chosen key, following the SAME priority — a SQL
#: warehouse row calling ai_query() also carries the model's ``endpoint_name``, and
#: taking that as the warehouse's name labelled warehouses after LLM endpoints.
_RESOURCE_NAME = """CASE
      WHEN u.usage_metadata.job_id IS NOT NULL THEN u.usage_metadata.job_name
      WHEN u.usage_metadata.dlt_pipeline_id IS NOT NULL
        OR u.usage_metadata.warehouse_id IS NOT NULL THEN NULL
      WHEN u.usage_metadata.app_id IS NOT NULL THEN u.usage_metadata.app_name
      WHEN u.usage_metadata.endpoint_name IS NOT NULL THEN u.usage_metadata.endpoint_name
    END"""


def _names_cte(r: "TableResolver") -> str:
    """Current names for the resource types billing doesn't name (warehouse,
    cluster, pipeline) plus jobs whose usage rows carry no ``job_name``.

    The dimension tables are grouped whole rather than filtered to the anomalous ids:
    a ``WHERE id IN (SELECT … FROM anomalous)`` filter re-inlines the entire
    detection chain once per table, and that nesting is what blew the plan up (see
    "Plan shape" in the module docstring). They are small — ~0.7M rows across all
    four on a ~4k-workspace account. ``max_by(name, change_time)`` is the latest name
    without QUALIFY (the Spark-based sample-data gate can't parse it)."""
    c = r.catalog
    return f"""
  SELECT CAST(workspace_id AS STRING) AS workspace_id, 'warehouse' AS resource_type,
         warehouse_id AS resource_id, max_by(warehouse_name, change_time) AS name
  FROM {c}.compute.warehouses GROUP BY 1, 2, 3
  UNION ALL
  SELECT CAST(workspace_id AS STRING), 'cluster', cluster_id, max_by(cluster_name, change_time)
  FROM {c}.compute.clusters GROUP BY 1, 2, 3
  UNION ALL
  SELECT CAST(workspace_id AS STRING), 'pipeline', pipeline_id, max_by(name, change_time)
  FROM {c}.lakeflow.pipelines GROUP BY 1, 2, 3
  UNION ALL
  SELECT CAST(workspace_id AS STRING), 'job', job_id, max_by(name, change_time)
  FROM {c}.lakeflow.jobs GROUP BY 1, 2, 3
"""


def _priced_cte(
    catalog: str,
    currency: str,
    *,
    resolver: "TableResolver | None",
    window_days: int,
) -> str:
    """Usage over baseline + evaluation window, priced at list, today excluded.

    ``usage_quantity`` is cast to DOUBLE once here so every downstream SUM is a
    double (a Decimal aggregate can't be written into the snapshot — the spend
    snapshot once stayed empty for exactly that reason)."""
    r = resolver or TableResolver.nested(catalog)
    usage_table = r("billing", "usage")
    scope_filter = f"\n    AND u.workspace_id IN ({regional_workspaces_sql(r.catalog)})"
    return f"""
  SELECT
    u.workspace_id,
    u.billing_origin_product AS product,
    u.sku_name,
    u.usage_date,
    {_RESOURCE_TYPE} AS resource_type,
    {_RESOURCE_ID} AS resource_id,
    {_RESOURCE_NAME} AS resource_name,
    u.identity_metadata.run_as   AS run_as,
    u.identity_metadata.owned_by AS owned_by,
    CAST(u.usage_quantity AS DOUBLE) * {price_expr()} AS cost
  FROM {usage_table} u
  {price_join(r, currency)}
  WHERE u.usage_date >= date_sub(current_date(), {window_days})
    AND u.usage_date < current_date()  -- today is partial
    AND u.workspace_id IS NOT NULL{scope_filter}
"""


def anomalies_sql(
    catalog: str = "system",
    lookback_days: int = 30,
    currency: str = "USD",
    *,
    resolver: "TableResolver | None" = None,
    baseline_days: int = BASELINE_DAYS,
    eval_days: int = EVAL_DAYS,
    z_threshold: float = Z_THRESHOLD,
    min_delta_usd: float = MIN_DELTA_USD,
    min_delta_share: float = MIN_DELTA_SHARE,
    step_min_days: int = STEP_MIN_DAYS,
) -> str:
    """One row per anomalous key: type, size, when, and who.

    ``lookback_days`` is accepted for the shared builder signature (the pipeline's
    kinded sink passes it) but deliberately unused: the window is intrinsic to the
    method (baseline + evaluation), not the app's reporting window.

    Not filtered by the caller's workspaces — like every other query here, scoping
    is applied by the caller around the whole statement. ``workspace_id`` is a
    grouping key throughout, so the filter is exact.
    """
    del lookback_days  # see docstring
    window = baseline_days + eval_days
    r = resolver or TableResolver.nested(catalog)
    priced = _priced_cte(
        catalog, currency, resolver=r, window_days=window,
    )
    names_part = f",\nnames AS ({_names_cte(r)})"
    # The system-table name is the CURRENT one; billing's job_name is the name
    # at time of use. Prefer current, fall back to billing (endpoints, apps).
    select_cols = (
        "a.* EXCEPT (resource_name), COALESCE(n.name, a.resource_name) AS resource_name"
    )
    names_join = (
        "\nLEFT JOIN names n ON n.workspace_id = a.workspace_id"
        "\n  AND n.resource_type = a.resource_type AND n.resource_id = a.resource_id"
    )
    return f"""
WITH priced AS ({priced}),
key_daily AS (
  -- Both grains in ONE pass over usage (each row is fanned out to a sku key and, if
  -- it has a resource, a resource key). `akey` is a single string key so nothing
  -- downstream compares NULL key parts.
  SELECT
    akey, max(grain) AS grain, max(workspace_id) AS workspace_id,
    max_by(product, cost) AS product, max(g_sku) AS sku_name,
    max(g_rtype) AS resource_type, max(g_rid) AS resource_id,
    max(g_rname) AS resource_name,
    usage_date, SUM(cost) AS cost,
    max_by(run_as, cost) AS run_as, max_by(owned_by, cost) AS owned_by
  FROM (
    SELECT p.*, g.grain,
      CASE WHEN g.grain = 'sku'
        THEN concat_ws('|', 'sku', CAST(p.workspace_id AS STRING), p.product, p.sku_name)
        ELSE concat_ws('|', 'resource', CAST(p.workspace_id AS STRING), p.resource_type,
                       p.resource_id) END AS akey,
      CASE WHEN g.grain = 'sku' THEN p.sku_name END AS g_sku,
      CASE WHEN g.grain = 'resource' THEN p.resource_type END AS g_rtype,
      CASE WHEN g.grain = 'resource' THEN p.resource_id END AS g_rid,
      CASE WHEN g.grain = 'resource' THEN p.resource_name END AS g_rname
    FROM priced p
    LATERAL VIEW explode(array('sku', 'resource')) g AS grain
    WHERE g.grain = 'sku' OR p.resource_id IS NOT NULL
  ) fanned
  GROUP BY akey, usage_date
),
ws_daily AS (
  SELECT workspace_id, usage_date, SUM(cost) AS cost
  FROM priced GROUP BY workspace_id, usage_date
),
ws_base AS (
  -- Median daily workspace spend over the baseline, densified: a day the workspace
  -- didn't bill is a $0 day. Drives the relative floor.
  SELECT w.workspace_id,
         percentile(COALESCE(d.cost, 0), 0.5) AS ws_median
  FROM (SELECT DISTINCT workspace_id FROM ws_daily) w
  CROSS JOIN (
    SELECT explode(sequence(date_sub(current_date(), {window}),
                            date_sub(current_date(), {eval_days + 1}))) AS usage_date
  ) cal
  LEFT JOIN ws_daily d ON d.workspace_id = w.workspace_id AND d.usage_date = cal.usage_date
  GROUP BY w.workspace_id
),
cand AS (
  -- A day can only clear the floor if it cost at least the floor, so keep just the
  -- keys with such a day in the evaluation window before densifying. Windowed rather
  -- than grouped-and-joined-back, so key_daily is read once.
  SELECT * FROM (
    SELECT k.*,
           max(CASE WHEN usage_date >= date_sub(current_date(), {eval_days})
                    THEN cost END) OVER (PARTITION BY akey) AS eval_max,
           max_by(product, cost) OVER (PARTITION BY akey) AS key_product,
           max(resource_name) OVER (PARTITION BY akey) AS key_resource_name
    FROM key_daily k
  ) x
  WHERE eval_max >= {min_delta_usd}
),
keys AS (
  SELECT akey, max(grain) AS grain, max(workspace_id) AS workspace_id,
         max(key_product) AS product, max(sku_name) AS sku_name,
         max(resource_type) AS resource_type, max(resource_id) AS resource_id,
         max(key_resource_name) AS resource_name
  FROM cand GROUP BY akey
),
dense AS (
  SELECT c.akey, c.grain, c.workspace_id, c.product, c.sku_name, c.resource_type,
         c.resource_id, c.resource_name, cal.usage_date,
         COALESCE(k.cost, 0) AS cost, k.run_as, k.owned_by,
         cal.usage_date >= date_sub(current_date(), {eval_days}) AS in_eval
  FROM keys c
  CROSS JOIN (
    SELECT explode(sequence(date_sub(current_date(), {window}),
                            date_sub(current_date(), 1))) AS usage_date
  ) cal
  LEFT JOIN cand k ON k.akey = c.akey AND k.usage_date = cal.usage_date
),
base AS (
  -- Baseline statistics as window aggregates over the baseline days only (the
  -- evaluation window is excluded, so a step change can't drag its own baseline up).
  -- Windows, not GROUP BY + join back: each join-back re-inlined `dense` and the plan
  -- grew multiplicatively. The same-weekday median (4 values in 28 days) is why a
  -- weekly job — $0 overall median, busy one day in seven — isn't a weekly "spike".
  SELECT d.*,
         percentile(CASE WHEN NOT in_eval THEN cost END, 0.5)
           OVER (PARTITION BY akey) AS base_median,
         percentile(CASE WHEN NOT in_eval THEN cost END, 0.75)
           OVER (PARTITION BY akey) AS base_p75,
         SUM(CASE WHEN NOT in_eval THEN cost END) OVER (PARTITION BY akey) AS base_total,
         COUNT(CASE WHEN NOT in_eval AND cost > 0 THEN 1 END)
           OVER (PARTITION BY akey) AS base_active_days,
         percentile(CASE WHEN NOT in_eval THEN cost END, 0.5)
           OVER (PARTITION BY akey, dayofweek(usage_date)) AS dow_median
  FROM dense d
),
scored AS (
  SELECT s.*,
         -- Excess over the HIGHER of the two baselines, so the reported size is the
         -- conservative one.
         cost - greatest(base_median, COALESCE(dow_median, 0)) AS excess,
         (cost - base_median)
           / greatest(1.4826 * base_mad, (base_p75 - base_median) / 0.6745,
                      0.1 * base_median, 1.0) AS z,
         greatest({min_delta_usd}, {min_delta_share} * COALESCE(w.ws_median, 0)) AS floor_usd
  FROM (
    SELECT b.*,
           percentile(CASE WHEN NOT in_eval THEN abs(cost - base_median) END, 0.5)
             OVER (PARTITION BY akey) AS base_mad
    FROM base b
  ) s
  LEFT JOIN ws_base w ON w.workspace_id = s.workspace_id
),
flagged AS (
  SELECT f.*,
         -- Gaps-and-islands over the flagged days: consecutive ones share a run id.
         datediff(usage_date, date_sub(current_date(), {window}))
           - row_number() OVER (PARTITION BY akey, is_flag ORDER BY usage_date) AS run_id
  FROM (
    SELECT s.*,
           in_eval AND (
             (base_total = 0 AND cost >= floor_usd)
             OR (z > {z_threshold} AND excess >= floor_usd)
           ) AS is_flag
    FROM scored s
  ) f
),
runs AS (
  SELECT r.*,
         CASE WHEN is_flag THEN COUNT(*) OVER (PARTITION BY akey, is_flag, run_id) END
           AS run_len
  FROM flagged r
),
per_key AS (
  -- One aggregation over every day of every candidate key: the flagged-day figures
  -- are conditional aggregates, the series covers all days. `peak_rank` orders the
  -- flagged days only (a flagged day costs at least the floor, so > -1) and breaks a
  -- tie — an always-on resource costs the same every day — to the LATEST day, so the
  -- peak date and the identity read off it are deterministic and from the same day.
  SELECT akey,
         max(grain) AS grain, max(workspace_id) AS workspace_id, max(product) AS product,
         max(sku_name) AS sku_name, max(resource_type) AS resource_type,
         max(resource_id) AS resource_id, max(resource_name) AS resource_name,
         count_if(is_flag) AS flagged_days,
         MIN(CASE WHEN is_flag THEN usage_date END) AS first_flag_date,
         MAX(CASE WHEN is_flag THEN usage_date END) AS last_flag_date,
         max_by(usage_date, peak_rank) AS peak_date,
         MAX(CASE WHEN is_flag THEN cost END) AS peak_cost_usd,
         max_by(run_as, peak_rank) AS run_as,
         max_by(owned_by, peak_rank) AS owned_by,
         SUM(CASE WHEN is_flag THEN
               CASE WHEN base_total = 0 THEN cost ELSE excess END END) AS excess_usd,
         MAX(CASE WHEN is_flag THEN z END) AS max_z,
         MAX(base_median) AS base_median,
         MAX(base_mad) AS base_mad,
         MAX(base_total) AS base_total,
         MAX(base_active_days) AS base_active_days,
         MAX(CASE WHEN is_flag THEN floor_usd END) AS floor_usd,
         MAX(run_len) AS longest_run,
         to_json(transform(array_sort(collect_list(named_struct(
           'd', usage_date, 'c', round(cost, 2)))), x -> x.c)) AS series_json,
         round(SUM(CASE WHEN in_eval THEN cost ELSE 0 END), 2) AS eval_cost_usd
  FROM (SELECT r.*, named_struct('c', CASE WHEN is_flag THEN cost ELSE -1 END,
                                   'd', usage_date) AS peak_rank FROM runs r) t
  GROUP BY akey
  HAVING count_if(is_flag) > 0
),
anomalous AS (
SELECT
  CAST(p.workspace_id AS STRING) AS workspace_id,
  p.grain,
  p.akey AS anomaly_key,
  CASE
    WHEN p.base_total = 0 THEN 'new_spender'
    WHEN p.longest_run >= {step_min_days} THEN 'step_change'
    ELSE 'spike'
  END AS anomaly_type,
  p.product,
  p.sku_name,
  p.resource_type,
  p.resource_id,
  p.resource_name,
  CAST(p.first_flag_date AS STRING) AS first_flag_date,
  CAST(p.last_flag_date AS STRING)  AS last_flag_date,
  CAST(p.peak_date AS STRING)       AS peak_date,
  p.flagged_days,
  p.longest_run,
  round(p.peak_cost_usd, 2)         AS peak_cost_usd,
  round(p.base_median, 2)           AS baseline_median_usd,
  round(p.base_mad, 2)              AS baseline_mad_usd,
  p.base_active_days                AS baseline_active_days,
  -- NULL for a new spender: there is no baseline to be N sigmas above.
  CASE WHEN p.base_total = 0 THEN NULL ELSE round(p.max_z, 1) END AS robust_z,
  round(p.excess_usd, 2)            AS excess_usd,
  round(p.floor_usd, 2)             AS floor_usd,
  p.eval_cost_usd,
  p.series_json,
  p.run_as,
  p.owned_by
FROM per_key p
){names_part}
SELECT {select_cols}
FROM anomalous a{names_join}
ORDER BY a.excess_usd DESC
"""


def anomaly_daily_sql(
    catalog: str = "system",
    lookback_days: int = 30,
    currency: str = "USD",
    *,
    resolver: "TableResolver | None" = None,
    baseline_days: int = BASELINE_DAYS,
    eval_days: int = EVAL_DAYS,
) -> str:
    """Daily cost per workspace over the same window — the trend the anomalies sit
    on. Same priced CTE, so the chart and the anomaly figures reconcile."""
    del lookback_days  # window is intrinsic to the method; see anomalies_sql
    priced = _priced_cte(
        catalog, currency, resolver=resolver, window_days=baseline_days + eval_days,
    )
    return f"""
WITH priced AS ({priced})
SELECT
  workspace_id,
  CAST(usage_date AS STRING) AS usage_date,
  round(SUM(cost), 2)        AS cost_usd
FROM priced
GROUP BY 1, 2
ORDER BY usage_date
"""
