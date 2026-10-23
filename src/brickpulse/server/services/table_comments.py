"""Table and column comments for the precompute outputs — one source of truth.

These tables outlive any single reader. The app knows that ``monthly_savings_usd``
is a deliberately-conservative estimate and that ``monthly_waste_usd`` on a
governance finding is unattributable rather than avoidable spend; a Genie space,
a Lakeview dashboard, or an analyst running ``SELECT *`` in six months knows none
of it. An uncommented column called ``monthly_savings_usd`` invites someone to sum
it and present the total as a committed number to a CFO.

So every output table carries its caveats in Unity Catalog metadata, where they
travel with the data instead of living in this repo's docs. Genie in particular
reads column comments as context, which makes them the cheapest way to stop it
inventing an interpretation of a column we were careful about.

WHAT ACTUALLY STICKS, AND WHERE (verified live, 2026-08-11)
-----------------------------------------------------------
The pipeline's outputs are MATERIALIZED VIEWS — that is what ``@dlt.table``
creates (see docs/DATA_MODEL.md) — and MVs accept only a TABLE comment:

* ``COMMENT ON TABLE`` / ``@dlt.table(comment=...)`` on an MV: **works**.
* Column comments on an MV: **silently discarded**. Both routes were tried
  against a live workspace and neither persists —
  ``ALTER TABLE ... ALTER COLUMN ... COMMENT`` fails outright
  (``EXPECT_TABLE_NOT_VIEW``), while the inline
  ``CREATE MATERIALIZED VIEW (col COMMENT '...')`` form *reports success* and then
  leaves ``information_schema.columns.comment`` NULL. The silent one is the trap:
  a helper that emitted those statements would look like it documented the
  contract while documenting nothing.
* Both comment kinds on a real Delta table: **work**. That covers the real
  Delta tables ``deploy.sh`` creates from ``ddl/`` and any future append-mode sink.

So :data:`COLUMN_COMMENTS` is only applied to real tables. For the MVs, the
column-level caveats live in the DDL files (which remain the documented contract)
and the table comment carries the one warning a stray reader most needs — that
these are advisory estimates.

Kept deliberately short: these render in tooltips and Catalog Explorer columns,
where a paragraph is worse than one precise sentence.

Lives in the ``finops`` package rather than under ``precompute/`` because
``pipeline.py`` runs as a bare source file on the pipeline cluster and can only
import from the INSTALLED WHEEL — the same constraint that puts
``precompute_transform`` and ``contract_schema`` here (see their docstrings).
"""

from __future__ import annotations

#: Table-level comments, keyed on the unqualified table name.
TABLE_COMMENTS: dict[str, str] = {
    "finops_recommendations": (
        "Scored cost-optimization findings, one row per (rule, resource, snapshot). "
        "ADVISORY: dollar figures are conservative ESTIMATES derived from system "
        "tables, not billed amounts or committed savings, and nothing here has been "
        "applied to any resource. Re-computed daily; read the latest snapshot_date."
    ),
    "finops_workspace_spend": (
        "Total billed spend per workspace for the snapshot window, with a SKU "
        "breakdown. Priced from system.billing.usage x list_prices at LIST price, so "
        "it excludes contract discounts and will not tie to an invoice."
    ),
    "finops_genie_usage": (
        "Genie cost aggregates (per user, surface, space and day). Analysis only - "
        "carries no findings and no savings figures. Free-tier GENIE_FREE_USAGE has "
        "no list price and correctly contributes $0."
    ),
    "finops_apps_usage": (
        "Databricks Apps inventory and spend (per app, per compute size, per day). "
        "Judging app idleness needs an access signal (system.access.audit "
        "'databricksApps' events); the idle_apps rule fires ONLY in workspaces whose "
        "audit log records it. Where a workspace logs no such events the signal is "
        "absent, so apps there stay analysis-only and produce no findings rather than "
        "being flagged idle blind."
    ),
    "finops_cost_anomalies": (
        "Cost anomalies: spikes, step changes and new spenders over the last 7 days, "
        "each measured against its own 28-day baseline (robust median/MAD z-score plus "
        "a $ floor), at SKU and resource grain. Analysis only - an anomaly is not a "
        "saving, so this carries no savings figures. List-priced. excess_usd overlaps "
        "across grains; sum it within ONE grain only."
    ),
    "finops_run_log": (
        "One row per pipeline step per run: status, duration, rows written. Read "
        "this to tell 'the rule found nothing' apart from 'the step failed'."
    ),
    "finops_rule_catalog": (
        "The rule-knowledge catalog: one row per enabled rule with its detection "
        "SQL, thresholds and current values, and scoring/savings logic. Built from "
        "the rules' own definitions (reads no system table). Backs the Ask Genie "
        "space so a user can pull up the logic, run what-if parameter changes "
        "against the system tables, and challenge the estimates."
    ),
    # "Saved so far" history: plain Delta tables written by the daily job's
    # track_changes task (precompute/change_tracker.py), not by the pipeline.
    "finops_finding_ledger": (
        "Saved-so-far tracker: one compact row per shown finding per snapshot day "
        "(main set only). A finding's first row is its baseline. Never pruned."
    ),
    "finops_change_events": (
        "Saved-so-far tracker: one row per change of a flagged resource toward (or away "
        "from) what we recommended, or its deletion. valued_monthly_usd = fraction x our "
        "own estimate; it shows the change happened, not that the app caused it. Only "
        "counted = true rows book anything."
    ),
    "finops_workspace_retirement": (
        "Saved-so-far tracker: workspaces seen in workspaces_latest (row_kind listing) and "
        "periods they were gone or deleted (row_kind retirement). Nothing in a retired "
        "workspace is booked."
    ),
    "finops_savings_daily": (
        "Saved-so-far tracker: booked saving per day and spend scope, deduped like the "
        "savings headline and rebuilt in full each run. Sum saved_usd over a date range "
        "for 'saved in that window'. Estimated from our own figures, list price."
    ),
}

#: Column comments per table, applied only to REAL Delta tables (an MV drops them
#: silently — see the module docstring). Only columns whose meaning is genuinely
#: non-obvious or easy to misread; commenting `workspace_id` would be noise that
#: trains people to ignore the tooltips.
COLUMN_COMMENTS: dict[str, dict[str, str]] = {
    "finops_recommendations": {
        "monthly_waste_usd": (
            "Current monthly spend on the resource that this finding concerns. For "
            "governance findings (rule_id=untagged_spend) this is UNATTRIBUTABLE "
            "spend, not avoidable waste."
        ),
        "monthly_savings_usd": (
            "POINT ESTIMATE of monthly spend recoverable if the action is applied, "
            "deliberately floored below true avoidable spend. Exactly 0 for governance "
            "findings, where the fix changes attribution and not the bill. Do not "
            "present a SUM of this column as committed savings -- the booked headline "
            "sums monthly_savings_low_usd of savings_status='bookable' rows only."
        ),
        "monthly_savings_low_usd": (
            "Conservative LOWER bound of monthly savings. This is the ONLY figure the "
            "booked headline totals, and only for savings_status='bookable' rows. "
            "<= monthly_savings_usd."
        ),
        "monthly_savings_high_usd": (
            "Upper scenario for monthly savings. Shown for range context; NEVER booked "
            "into any headline total. >= monthly_savings_usd."
        ),
        "savings_status": (
            "How much trust the headline places in this finding: 'bookable' (its "
            "monthly_savings_low_usd is booked into the headline lower bound), "
            "'estimated' (shown and rankable, contributes 0 to the booked total), or "
            "'advisory' (no dollars booked)."
        ),
        "effort_score": (
            "Numeric weight of effort_band (trivial=1 .. high=5), used as the "
            "denominator of nba_score."
        ),
        "nba_score": (
            "Next-Best-Action rank: monthly_savings_usd per unit of effort, x a "
            "priority multiplier. Higher is a better first move. Comparable across "
            "rules; not a dollar amount."
        ),
        "evidence": (
            "JSON: the raw metrics behind the finding, plus `confidence` (how "
            "directly the data supports it) and `data_basis` (which system tables, "
            "what window, where it rounded down)."
        ),
        "snapshot_date": (
            "The day this finding was computed. The table is partitioned on it and "
            "re-runs replace the partition, so always filter to the latest."
        ),
        "why": "Human-readable explanation of the waste. Safe to show verbatim.",
        "how": "The remediation steps. Advisory only - never auto-applied.",
    },
    "finops_workspace_spend": {
        "total_spend_usd": (
            "Billed spend over lookback_days at LIST price. Excludes contract "
            "discounts; will not reconcile to an invoice."
        ),
        "monthly_spend_usd": (
            "total_spend_usd normalized to a 30-day month, so it is comparable with "
            "the monthly savings figures."
        ),
        "by_sku": "JSON array of {sku_group, spend_usd, dbus}, largest spend first.",
    },
    "finops_run_log": {
        "status": "OK | FAILED | SKIPPED.",
        "rows_written": "Rows the step persisted. 0 with status=OK means it genuinely found nothing.",
        "message": "Failure reason, or a short note on why a step was skipped.",
    },
}


def _quote(text: str) -> str:
    """SQL string literal. Doubles embedded single quotes ("don't" -> "don''t") —
    several comments contain apostrophes, and an unescaped one is a syntax error
    that would fail the step that applies it."""
    return "'" + text.replace("'", "''") + "'"


def column_comment_sql(table_fqn: str, table: str) -> list[str]:
    """``ALTER TABLE ... ALTER COLUMN ... COMMENT`` statements for one table.

    ONLY valid against a real Delta table. On a materialized view this raises
    ``EXPECT_TABLE_NOT_VIEW``, so callers must not apply it to a pipeline output.
    Returns an empty list for an undocumented table; one statement per column,
    because Databricks SQL comments a single column per statement.
    """
    return [
        f"ALTER TABLE {table_fqn} ALTER COLUMN {column} COMMENT {_quote(text)}"
        for column, text in COLUMN_COMMENTS.get(table, {}).items()
    ]


def table_comment_sql(table_fqn: str, table: str) -> str | None:
    """``COMMENT ON TABLE`` statement, or ``None`` if the table is undocumented.

    Safe on both real tables and materialized views."""
    text = TABLE_COMMENTS.get(table)
    return f"COMMENT ON TABLE {table_fqn} IS {_quote(text)}" if text else None
