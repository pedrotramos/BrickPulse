-- Cost anomalies (prod / precompute mode).
--
-- Written by the SDP pipeline (precompute/pipeline.py::_build_anomalies), a sibling
-- sink of finops_recommendations like finops_apps_usage. Read by the Cost Anomalies
-- tab.
--
-- This table carries NO findings and NO savings figures: an anomaly is a cost that
-- moved away from its own 28-day baseline, which can be a backfill, a launch or a
-- real mistake — only the owner can say. excess_usd is a SIZE, not a saving. See
-- backend/finops/data/anomaly_sql.py for the method.
--
-- One table, two aggregates, distinguished by `kind`
-- --------------------------------------------------
-- 'anomaly' — one row per anomalous key, at grain 'sku' (workspace x product x SKU)
--             or 'resource' (job / warehouse / cluster / endpoint / ...). The two
--             grains overlap: sum excess_usd within ONE grain only.
-- 'daily'   — cost per workspace per day over the same 35-day window (the trend).
--
-- Same (kind, workspace_id, payload) shape as finops_apps_usage / finops_genie_usage,
-- and for the same reason `workspace_id` is a REAL column removed from the payload:
-- scoping must never parse JSON, so the read path selects it and merges it back.
--
-- {catalog}/{schema} are substituted by deploy/deploy.sh from config (defaults
-- main.finops). The pipeline materializes this table, so deploy.sh must NOT
-- pre-create it.

CREATE TABLE IF NOT EXISTS {catalog}.{schema}.finops_cost_anomalies (
  snapshot_date   DATE         NOT NULL,
  -- 'anomaly' | 'daily'
  kind            STRING       NOT NULL,
  workspace_id    STRING,
  workspace_name  STRING,
  workspace_url   STRING,
  lookback_days   INT,
  -- The aggregate row itself, as JSON. Keys per kind are the columns of
  -- anomalies_sql / anomaly_daily_sql in backend/finops/data/anomaly_sql.py.
  payload         STRING,
  computed_at     TIMESTAMP
)
USING DELTA
PARTITIONED BY (snapshot_date)
TBLPROPERTIES (
  'delta.feature.allowColumnDefaults' = 'supported',
  'delta.columnMapping.mode' = 'name'
);
