-- Databricks Apps inventory (prod / precompute mode).
--
-- Written by the SDP pipeline (precompute/pipeline.py::_build_apps), a sibling sink
-- of finops_recommendations like finops_genie_usage. Read by the Apps tab.
--
-- This table carries NO findings and NO savings figures, by necessity rather than
-- choice: system.access.audit has app lifecycle events only (create/deploy/start/
-- stop/update/delete) and there is no app request or page-view log anywhere in
-- system tables. So "is this app used?" is unanswerable, and an idle-app or
-- rightsizing recommendation would be a guess. The tab shows configuration and cost
-- facts and lets a human make the call. See backend/finops/data/apps_sql.py.
--
-- One table, three aggregates, distinguished by `kind`
-- ---------------------------------------------------
-- Same trade-off as finops_genie_usage: the three aggregates (per-app inventory,
-- per-size rollup, daily trend) have different columns, so each row carries its
-- `kind` and a JSON `payload` rather than needing three tables, three DDLs and
-- three grants. Cheap to evolve for an analysis-only surface; the typed-column
-- contract in finops_recommendations earns its keep only because rules, API and UI
-- all depend on that exact shape.
--
-- `workspace_id` stays a REAL top-level column because the app scopes reads on it
-- (a workspace admin must not see another workspace's apps), and scoping must never
-- depend on parsing JSON. It is therefore REMOVED from the payload — which also
-- means the read path has to select it explicitly and merge it back. Reading only
-- (kind, payload) silently loses every workspace attribution; that bug shipped once
-- on the Genie tab and showed up as "across 0 workspaces".
--
-- {catalog}/{schema} are substituted by deploy/deploy.sh from config (defaults
-- main.finops). The pipeline materializes this table, so deploy.sh must NOT
-- pre-create it.

CREATE TABLE IF NOT EXISTS {catalog}.{schema}.finops_apps_usage (
  snapshot_date   DATE         NOT NULL,
  -- 'inventory' | 'size' | 'daily'
  kind            STRING       NOT NULL,
  workspace_id    STRING,
  workspace_name  STRING,
  workspace_url   STRING,
  lookback_days   INT,
  -- The aggregate row itself, as JSON. Keys per kind are documented in
  -- backend/finops/data/apps_sql.py (the queries that produce them).
  payload         STRING,
  computed_at     TIMESTAMP
)
USING DELTA
PARTITIONED BY (snapshot_date)
TBLPROPERTIES (
  'delta.feature.allowColumnDefaults' = 'supported',
  'delta.columnMapping.mode' = 'name'
);
