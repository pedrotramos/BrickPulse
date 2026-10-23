-- Genie cost aggregates (prod / precompute mode).
--
-- Genie spend is attributed to PEOPLE and SPACES rather than to compute
-- resources, so it fits neither the recommendation contract (one row per finding)
-- nor the workspace-spend table (one row per workspace). It gets its own table.
--
-- One table, four aggregates, distinguished by `kind`
-- ---------------------------------------------------
-- The tab needs four different shapes (per-user, per-surface, per-space, daily).
-- Rather than four tables — four DDLs, four grants, four pipeline sinks — each
-- row carries its aggregate `kind` and a JSON `payload`. The app parses the
-- payload per kind (see data/genie_source.py). This keeps the analysis-only
-- surface cheap to evolve: adding a fifth aggregate needs no schema migration,
-- unlike the recommendation contract where a typed column per field earns its
-- keep because rules, API and UI all depend on the exact shape.
--
-- `workspace_id` stays a real top-level column because the app scopes reads on it
-- (workspace admins must not see other workspaces), and scoping must never depend
-- on parsing JSON.
--
-- {catalog}/{schema} are substituted by deploy/deploy.sh from config (defaults
-- main.finops). The pipeline overwrites the current snapshot_date partition, so
-- re-runs are idempotent.

CREATE TABLE IF NOT EXISTS {catalog}.{schema}.finops_genie_usage (
  snapshot_date   DATE         NOT NULL,
  -- 'user' | 'surface' | 'space' | 'daily'
  kind            STRING       NOT NULL,
  workspace_id    STRING,
  workspace_name  STRING,
  workspace_url   STRING,
  lookback_days   INT,
  -- The aggregate row itself, as JSON. Keys per kind are documented in
  -- backend/finops/data/genie_sql.py (the queries that produce them).
  payload         STRING,
  computed_at     TIMESTAMP
)
USING DELTA
PARTITIONED BY (snapshot_date)
TBLPROPERTIES (
  'delta.feature.allowColumnDefaults' = 'supported',
  'delta.columnMapping.mode' = 'name'
);
