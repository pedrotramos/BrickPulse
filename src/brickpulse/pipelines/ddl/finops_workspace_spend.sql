-- Per-workspace TOTAL spend (prod / precompute mode).
--
-- The recommendation table only knows about resources a rule flagged, so it can
-- never answer "what does this workspace cost?" — the number a platform admin
-- actually anchors on. This table carries the whole billed spend per workspace
-- over the lookback window, plus a small SKU breakdown, so the app can show total
-- spend and put the savings opportunity in proportion to it.
--
-- {catalog}/{schema} are substituted by precompute/job.py from config (defaults
-- main.finops). The job overwrites the current snapshot_date partition, so
-- re-runs are idempotent.

CREATE TABLE IF NOT EXISTS {catalog}.{schema}.finops_workspace_spend (
  snapshot_date         DATE         NOT NULL,
  workspace_id          STRING       NOT NULL,
  workspace_name        STRING,
  workspace_url         STRING,
  lookback_days         INT,                 -- window the totals cover
  total_spend_usd       DECIMAL(14,2),       -- all billed usage x list_prices in the window
  monthly_spend_usd     DECIMAL(14,2),       -- normalized to a 30-day month
  total_dbus            DOUBLE,
  -- Where the money goes, so the total is explainable rather than just big.
  -- JSON array of {sku_group, spend_usd, dbus} sorted by spend desc.
  by_sku                STRING,
  -- Day-by-day spend so the app can show a trend, not just a total. JSON array of
  -- {usage_date, spend_usd, dbus}, oldest first. A JSON column rather than a
  -- per-day grain because this table is one row per workspace and every reader
  -- depends on that.
  by_day                STRING,
  -- Serverless vs classic split, which the About Serverless view leans on.
  serverless_spend_usd  DECIMAL(14,2),
  classic_spend_usd     DECIMAL(14,2),
  computed_at           TIMESTAMP,
  -- Contract / cloud views (finops_rules.price_basis). NULL when the account has no
  -- system.billing.account_prices / cloud_infra_cost data.
  total_spend_contract_usd DECIMAL(14,2),    -- BLENDED: contract rate where priced, list otherwise
  contract_priced_share DOUBLE,              -- list-$ share of total_spend_usd that had a contract price
  total_cloud_cost_usd  DECIMAL(14,2)        -- classic cloud VM cost; never added into the DBU spend
)
USING DELTA
PARTITIONED BY (snapshot_date)
TBLPROPERTIES (
  'delta.feature.allowColumnDefaults' = 'supported',
  'delta.columnMapping.mode' = 'name'
);
