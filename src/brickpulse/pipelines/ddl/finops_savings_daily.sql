-- "Saved so far" tracker: the booked saving per (day, spend scope).
--
-- Written by the `track_changes` task (precompute/change_tracker.py), REBUILT IN FULL
-- each run from finops_change_events + finops_workspace_retirement (a late-detected
-- change or a retirement can rewrite past days). One row per (day, workspace, scope)
-- where something was booked: run_rate_monthly_usd is book_scope (the headline's own
-- dedup, finops.services.aggregation) over the counted changes in effect that day, and
-- saved_usd = run_rate_monthly_usd / 30 (the app's 30-day month, pricing.DAYS_PER_MONTH).
-- A change accrues from its own changed_at day until the finding's next event, and not
-- on days its workspace is retired. Hidden rules (FINOPS_HIDDEN_RULES) are excluded.
--
--   saved in a window   = SUM(saved_usd) WHERE day BETWEEN a AND b
--   run-rate            = SUM(run_rate_monthly_usd) WHERE day = MAX(day)
--   tracking_since      = MIN(snapshot_date) FROM finops_finding_ledger
-- See docs/DATA_MODEL.md "Savings tracker".

CREATE TABLE IF NOT EXISTS {catalog}.{schema}.finops_savings_daily (
  day                    DATE,
  workspace_id           STRING,
  scope_key              STRING,          -- affected_spend_scope, else resource_type|resource_id
  rule_ids               ARRAY<STRING>,
  finding_keys           ARRAY<STRING>,
  saved_usd              DOUBLE,          -- booked that day
  run_rate_monthly_usd   DOUBLE,
  change_kinds           ARRAY<STRING>,
  computed_at            TIMESTAMP
)
USING DELTA;
