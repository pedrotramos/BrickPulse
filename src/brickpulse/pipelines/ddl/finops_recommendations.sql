-- The recommendation contract table (prod / precompute mode).
-- Must stay in sync (additively only) with:
--   backend/finops/models/recommendation.py  (pydantic)
--   frontend/src/api/types.ts                (TypeScript)
-- See docs/DATA_MODEL.md.
--
-- {catalog}/{schema} are substituted by precompute/job.py from config
-- (defaults main.finops). The scheduled job MERGEs into this table keyed on
-- recommendation_id, so re-runs for the same snapshot_date are idempotent.

CREATE TABLE IF NOT EXISTS {catalog}.{schema}.finops_recommendations (
  recommendation_id     STRING       NOT NULL,
  snapshot_date         DATE         NOT NULL,
  rule_id               STRING       NOT NULL,
  rule_version          STRING,
  rule_title            STRING,
  category              STRING,
  workspace_id          STRING       NOT NULL,
  workspace_name        STRING,
  workspace_url         STRING,        -- base URL of the resource's own workspace (for deep links)
  resource_type         STRING,
  resource_id           STRING,
  resource_name         STRING,
  resource_owner        STRING,        -- who owns the resource (billing.usage identity_metadata.owned_by), NULL if unknown
  resource_creator      STRING,        -- who created it (identity_metadata.created_by / run_as fallback), NULL if unknown
  why                   STRING,
  how                   STRING,
  monthly_waste_usd     DECIMAL(12,2),
  monthly_savings_usd   DECIMAL(12,2),   -- point estimate (kept for compatibility)
  monthly_savings_low_usd  DECIMAL(12,2),  -- conservative floor; the ONLY value the headline books (bookable findings only)
  monthly_savings_high_usd DECIMAL(12,2),  -- upper scenario, never booked
  savings_status        STRING,          -- bookable | estimated | advisory (headline books bookable low only)
  affected_spend_scope  STRING,          -- spend pool this action reclaims from (dedup key); NULL = this resource
  savings_composition   STRING,          -- exclusive (mutually-exclusive alternative) | additive (composes with other tunings)
  effort_band           STRING,
  effort_score          INT,
  priority              STRING,
  nba_score             DOUBLE,
  evidence              STRING,        -- JSON blob of the metrics that triggered it
  computed_at           TIMESTAMP,
  price_basis           STRING,          -- list | contract (NULL = no contract data for this run)
  contract_price_coverage DOUBLE,        -- share of the resource's usage with a contract price (account_prices)
  monthly_savings_contract_usd      DECIMAL(12,2),  -- point estimate at contract rates (NULL below the coverage gate)
  monthly_savings_contract_low_usd  DECIMAL(12,2),  -- range at contract rates
  monthly_savings_contract_high_usd DECIMAL(12,2),
  cloud_cost_30d_usd    DECIMAL(12,2),   -- classic cloud VM cost of the resource (billing.cloud_infra_cost)
  monthly_cloud_savings_usd DECIMAL(12,2), -- cloud VM saving, separate from (never added to) DBU savings
  finding_key           STRING         -- sha256(rule_id|workspace_id|resource_type|resource_id)[:32]; snapshot-independent
)
USING DELTA
PARTITIONED BY (snapshot_date)
TBLPROPERTIES (
  'delta.feature.allowColumnDefaults' = 'supported',
  'delta.columnMapping.mode' = 'name'
);

-- Cluster reads by workspace (the primary scoping key).
-- ALTER TABLE {catalog}.{schema}.finops_recommendations CLUSTER BY (workspace_id);
