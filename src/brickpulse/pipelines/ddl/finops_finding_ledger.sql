-- "Saved so far" tracker: one compact row per main-set finding per snapshot day.
--
-- Written by the `track_changes` task of the daily trigger job
-- (precompute/change_tracker.py), NOT by the SDP pipeline: every pipeline output is a
-- materialized view holding only the current snapshot, so history needs a plain Delta
-- table that a pipeline full refresh cannot reset. The tracker creates it if absent,
-- from the code schema (change_tracker.TABLE_COLUMNS); this file is the documented
-- contract and test_change_tracker pins the two together.
--
-- Idempotent per snapshot_date (DELETE that day + INSERT). Never pruned: a finding's
-- FIRST row is its baseline (the "from" value of the proposed setting), and ~3M rows
-- a year is small for Delta. Main set only (no sidecar category, no Labs rule).
-- See docs/DATA_MODEL.md "Savings tracker".

CREATE TABLE IF NOT EXISTS {catalog}.{schema}.finops_finding_ledger (
  snapshot_date            DATE,
  finding_key              STRING,     -- sha256(rule_id|workspace_id|resource_type|resource_id)[:32]
  rule_id                  STRING,
  rule_version             STRING,
  workspace_id             STRING,
  resource_type            STRING,
  resource_id              STRING,
  resource_name            STRING,
  savings_status           STRING,     -- bookable | estimated | advisory (advisory is never tracked)
  monthly_savings_usd      DOUBLE,     -- point estimate, list price: the valuation basis
  monthly_savings_low_usd  DOUBLE,
  monthly_waste_usd        DOUBLE,     -- the spend pool's cost, the book_scope cap
  affected_spend_scope     STRING,
  savings_composition      STRING,
  evidence_core            STRING,     -- JSON: tracked-setting keys + phasing + savings_curve + idle gap + _observed
  computed_at              TIMESTAMP
)
USING DELTA;
