-- The rule-knowledge catalog: the CALCULATIONS behind the findings, as data.
--
-- One row per enabled rule. Where finops_recommendations says WHAT was flagged,
-- this says HOW the engine decides: the detection SQL it runs, the thresholds and
-- their current values, and the scoring/savings logic verbatim. It exists for the
-- Ask Genie space so a user can pull up a rule's logic, re-run its detection SQL
-- against the (granted) system tables with a parameter changed for what-if
-- analysis, and push back on an estimate with the actual formula in hand.
--
-- Written by the precompute pipeline from finops.services.rule_catalog (the same
-- data both backends build), so it always reflects the deployed thresholds.yaml.
--
-- {catalog}/{schema} are substituted by the pipeline/job from config (defaults
-- main.finops). The writer overwrites the current snapshot_date partition, so
-- re-runs are idempotent.

CREATE TABLE IF NOT EXISTS {catalog}.{schema}.finops_rule_catalog (
  snapshot_date         DATE         NOT NULL,
  rule_id               STRING       NOT NULL,
  rule_version          STRING,
  rule_title            STRING,
  category              STRING,        -- compute | sql | jobs | serving | pipelines | governance
  resource_type         STRING,        -- cluster | warehouse | job | serving_endpoint | pipeline
  enabled               BOOLEAN,
  lookback_days         INT,           -- window the detection SQL was rendered for
  thresholds            STRING,        -- JSON object: the rule's tunable params + current values
  detection_sql         STRING,        -- the candidate-detection SQL, rendered with those params
  scoring_logic         STRING,        -- the verbatim score() source: guards + savings + effort/priority
  nba_formula           STRING,        -- how the ranker turns savings+effort+priority into nba_score
  effort_weights        STRING,        -- JSON: effort band -> numeric weight
  priority_multipliers  STRING,        -- JSON: priority -> nba multiplier
  source_tables         STRING,        -- JSON array: the system tables this rule reads
  computed_at           TIMESTAMP
)
USING DELTA
PARTITIONED BY (snapshot_date)
TBLPROPERTIES (
  'delta.feature.allowColumnDefaults' = 'supported',
  'delta.columnMapping.mode' = 'name'
);
