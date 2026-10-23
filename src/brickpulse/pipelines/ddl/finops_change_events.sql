-- "Saved so far" tracker: one row per change of a tracked finding's state.
--
-- Written by the `track_changes` task (precompute/change_tracker.py), a plain Delta
-- table (see finops_finding_ledger.sql for why not the pipeline). A row is added when
-- the assessment of a finding's resource differs from its last recorded state, so
-- 240 -> 60 -> 30 is two rows of ONE finding whose fraction rises twice, both valued
-- against the same estimate (reference_snapshot_date). Idempotent per run day: the
-- rows with recorded_on = that day are replaced. `counted` / `not_counted_reason`
-- are re-derived every run (a retirement detected late can cover an earlier change).
-- See docs/DATA_MODEL.md "Savings tracker".

CREATE TABLE IF NOT EXISTS {catalog}.{schema}.finops_change_events (
  event_id                 STRING,     -- sha256(finding_key|changed_at|kind|after_value)[:32]
  finding_key              STRING,
  changed_at               TIMESTAMP,  -- the SCD row's change_time, or delete_time for a deletion
  change_kind              STRING,     -- toward | reached | beyond | deleted | paused | away
  setting                  STRING,     -- the tracked column, e.g. auto_stop_minutes
  baseline_value           STRING,     -- from the finding's first ledger row
  after_value              STRING,
  proposed_value           STRING,     -- the full-credit proposal (phasing data target when phased)
  fraction                 DOUBLE,     -- [0, 1] of the estimate
  valuation_method         STRING,     -- curve | ladder | linear | binary | deletion
  estimate_before_usd      DOUBLE,     -- monthly_savings_usd of the reference snapshot
  waste_before_usd         DOUBLE,     -- monthly_waste_usd of the reference snapshot (book_scope cap)
  valued_monthly_usd       DOUBLE,     -- fraction * estimate_before_usd (also set when not counted)
  counted                  BOOLEAN,
  not_counted_reason       STRING,     -- before_first_seen | advisory | workspace_retired | workspace_status_unknown
  rule_id                  STRING,
  workspace_id             STRING,
  resource_type            STRING,
  resource_id              STRING,
  resource_name            STRING,
  first_seen               DATE,       -- the finding's first ledger snapshot_date
  reference_snapshot_date  DATE,       -- last non-advisory snapshot on/before the finding's first change
  affected_spend_scope     STRING,
  savings_composition      STRING,
  recorded_on              DATE,       -- the snapshot day of the run that recorded it
  recorded_at              TIMESTAMP
)
USING DELTA;
