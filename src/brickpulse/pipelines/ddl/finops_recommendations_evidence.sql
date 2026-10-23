-- Flattened view of recommendation EVIDENCE, for the Ask Genie space.
--
-- Genie sits on the finops_* tables, and until now it could restate WHAT was
-- flagged (rule, resource, savings) but not the metrics that MADE it fire — those
-- live in the recommendations table's `evidence` column as a JSON string, which
-- Genie cannot slice ("which clusters had avg CPU under 5%?"). This view explodes
-- the recurring, cross-rule scalar keys out of that JSON into real columns so
-- those "why" questions become answerable — WITHOUT granting the space any system
-- table. It reads only finops_recommendations, so it stays entirely inside the
-- precompute semantic layer the space already has (the minimal Fix #2): more depth
-- than "output only", no new source access.
--
-- Not every key is present on every finding (evidence shape is per-rule), so each
-- extraction is null-tolerant: a missing key is NULL, never an error. The raw
-- `evidence` JSON is carried through too, for the keys this curated set omits.
--
-- {catalog}/{schema} are substituted at deploy time, same as the table DDL. This
-- is a VIEW (not materialized): it always reflects the current snapshot the
-- pipeline last wrote, and creating it costs nothing until queried.

CREATE OR REPLACE VIEW {catalog}.{schema}.finops_recommendations_evidence
COMMENT 'Flattened evidence for the Ask Genie space -- the triggering metrics (CPU, idle, cost, sizing, failure rate, confidence) exploded out of finops_recommendations.evidence JSON. Reads finops_recommendations only (no system tables). ADVISORY, same caveats as that table -- figures are estimates, and a governance finding carries 0 savings.'
AS
SELECT
  -- Identity + the finding-level facts, so the view is useful on its own.
  recommendation_id,
  snapshot_date,
  rule_id,
  rule_title,
  category,
  workspace_id,
  workspace_name,
  resource_type,
  resource_id,
  resource_name,
  resource_owner,
  resource_creator,
  monthly_savings_usd,
  monthly_savings_low_usd,   -- conservative lower-bound savings, the only value the headline books (bookable only)
  monthly_savings_high_usd,  -- upper scenario, never booked
  savings_status,            -- bookable | estimated | advisory
  monthly_waste_usd,
  priority,
  effort_band,

  -- --- curated cross-rule evidence scalars (NULL when a rule doesn't emit one) ---
  -- Cost / usage over the lookback, the denominator most "is this worth it?"
  -- questions need.
  evidence:cost_30d_usd::double        AS cost_30d_usd,
  evidence:cost_window_usd::double     AS cost_window_usd,
  evidence:dbus_30d::double            AS dbus_30d,
  evidence:lookback_days::int          AS lookback_days,

  -- Utilization signals — the heart of every idle/right-size verdict.
  evidence:avg_cpu_pct::double         AS avg_cpu_pct,
  evidence:cpu_avg_pct::double         AS cpu_avg_pct,
  evidence:cpu_peak_pct::double        AS cpu_peak_pct,
  evidence:mem_avg_pct::double         AS mem_avg_pct,
  evidence:idle_pct::double            AS idle_pct,
  evidence:idle_windows::int           AS idle_windows,
  evidence:total_windows::int          AS total_windows,

  -- Sizing / shape.
  evidence:worker_count::int           AS worker_count,
  evidence:target_workers::int         AS target_workers,
  evidence:warehouse_size::string      AS warehouse_size,
  evidence:proposed_size::string       AS proposed_size,
  evidence:auto_stop_minutes::int      AS auto_stop_minutes,
  evidence:cluster_kind::string        AS cluster_kind,

  -- Jobs / reliability.
  evidence:fail_rate_pct::double       AS fail_rate_pct,
  evidence:total_runs::int             AS total_runs,
  evidence:failed_runs::int            AS failed_runs,

  -- How trustworthy the finding is — same fields the UI shows as the "basis".
  evidence:confidence::string          AS confidence,
  evidence:data_basis::string          AS data_basis,
  evidence:observed_days::int          AS observed_days,

  -- The full blob, for anything the curated columns don't cover.
  evidence                             AS evidence_json
FROM {catalog}.{schema}.finops_recommendations;
