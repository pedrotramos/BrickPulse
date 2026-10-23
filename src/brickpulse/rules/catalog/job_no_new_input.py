"""Rule: scheduled job runs whose upstream data didn't change (Tier 2, C7).

A job on a cron schedule re-runs whether or not anything it
reads has changed. When NO source table had a write since the job's previous
successful run, the run recomputes the answer it already produced. A table-update
trigger (or a file-arrival trigger for file sources) runs the job only when an input
changed, so those runs — and their compute — would not happen.

How "no new input" is read (verified live, see the SQL header): the job's source
tables come from ``system.access.table_lineage`` (its own reads, views expanded to
their base tables, its own output tables removed); writes to those tables by ANY
entity are bucketed per hour; a successful scheduled run is no-new-input when the
latest source write hour ended before the previous successful run started.

**Estimated saving, unless an input is unknown.** The cost of the runs that started
with no new input is what a table-update trigger would have skipped, so it is the
finding's ``estimated`` saving (shown and ranked, not booked). Lineage only sees Unity
Catalog reads and writes made on Databricks. A job that also pulls from an API, a JDBC
source or files outside UC, or reads a table that something outside Databricks writes,
has inputs the rule cannot see. So the guards are strict (path reads → suppressed; poor
lineage coverage of the job's runs → suppressed), and a finding with partly unknown
sources (``evidence.data_warning``) stays ``advisory`` at $0. Promoted out of Labs
2026-09-23.
"""

from __future__ import annotations

from brickpulse.rules.change_tracking import TrackedSetting
from brickpulse.rules.contract import (
    EffortBand,
    Rule,
    RuleContext,
    RuleFinding,
    RulePriority,
)
from brickpulse.rules.coverage import coverage_evidence, coverage_note, underpriced
from brickpulse.rules.evidence import CONFIDENCE_MEDIUM, evidence_block, viz_daily_stack
from brickpulse.rules.history import history_evidence, history_note, insufficient_history
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register

_BASIS = (
    "system.access.table_lineage (the job's own reads, views expanded to base tables, "
    "its own output tables removed; writes to those tables by any entity, bucketed per "
    "hour) + lakeflow.job_run_timeline (CRON/PERIODIC runs that SUCCEEDED) + "
    "billing.usage per job run via usage_metadata.job_run_id x list_prices, {n}-day "
    "lookback. A run is 'no new input' when no source table was written since the "
    "previous successful run started. Lineage sees only Unity Catalog reads and writes "
    "made on Databricks: an API, JDBC or non-UC file input, or a table written from "
    "outside Databricks, is invisible here."
)


@register
class JobNoNewInputRule(Rule):
    id = "job_no_new_input"
    # Estimated: the input-less runs' cost is measured; unknown inputs → advisory.
    default_savings_status = "estimated"
    version = "0.1.0"
    title = "Scheduled job runs with no new input data"
    resource_type = "job"
    # "Saved so far" (finops_rules.change_tracking): the job was paused (deletion is
    # generic). The "from" value is the tracker's own observation of ``paused``.
    tracked_setting = TrackedSetting(
        resource="job", column="paused", proposed_value=True, direction="equals",
        valuation="binary", reached_kind="paused", label="Schedule paused",
    )
    category = "jobs"
    contract_pricing = "none"  # list-price estimate: the SQL projects no contract_ratio yet
    cloud_savings = "none"
    sql_file = "job_no_new_input.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        if insufficient_history(row, ctx):
            return None
        if underpriced(row):
            return None

        t = ctx.thresholds
        # Lineage blind spots: a path read means inputs outside UC tables, and a job
        # with no observable source table can't be judged at all.
        if int(row.get("has_path_read") or 0):
            return None
        # Sources that are always written outside lineage (system tables, serving
        # inference tables) change constantly but never show a write.
        if int(row.get("has_system_source") or 0) or int(row.get("has_inference_source") or 0):
            return None
        n_sources = int(row.get("n_sources") or 0)
        if n_sources <= 0:
            return None
        # A view read with no expanded base tables hides what actually changes.
        if int(row.get("has_view_read") or 0) and not int(row.get("has_indirect_read") or 0):
            return None
        # Every source never written in the window: static reference data, or a table
        # written from outside Databricks. Either way there is no write feed to
        # compare against, so "no new input" would just mean "no visible writer".
        if int(row.get("n_sources_unwritten") or 0) >= n_sources:
            return None

        evaluated = int(row.get("evaluated_runs") or 0)
        no_new = int(row.get("no_new_input_runs") or 0)
        if evaluated <= 0 or no_new < int(t.get("min_no_new_input_runs", 5)):
            return None
        # Lineage must have recorded most of the job's runs, or its source set is a
        # partial view (e.g. some runs on a non-UC cluster).
        coverage = int(row.get("lineage_runs") or 0) / evaluated
        if coverage < float(t.get("min_lineage_run_coverage", 0.5)):
            return None
        if int(row.get("no_new_input_days") or 0) < int(t.get("min_no_new_input_days", 3)):
            return None
        share = no_new / evaluated
        if share < float(t.get("min_no_new_input_share", 0.3)):
            return None

        addressable = to_monthly(row.get("no_new_input_cost_30d") or 0.0, ctx.lookback_days)
        if addressable < float(t.get("min_addressable_usd", 25)):
            return None

        name = row.get("job_name") or str(row["job_id"])
        sources = [s for s in (row.get("sample_sources") or []) if s]
        unwritten = int(row.get("n_sources_unwritten") or 0)
        unwritten_names = [s for s in (row.get("unwritten_sources") or []) if s]
        if share >= 0.7 and addressable >= 200:
            priority = RulePriority.HIGH
        elif share >= 0.5 or addressable >= 100:
            priority = RulePriority.MEDIUM
        else:
            priority = RulePriority.LOW

        src_clause = (
            f" It reads {n_sources} table(s), e.g. {', '.join(sources[:3])}."
            if sources
            else f" It reads {n_sources} table(s)."
        )
        static_clause = (
            f" {unwritten} of them had no write at all in the window (static, or written "
            "from outside Databricks)."
            if unwritten
            else ""
        )
        why = (
            f"Job '{name}' ran on its schedule and succeeded {evaluated:,} times over the "
            f"last {ctx.lookback_days} days (after its first run), and {no_new:,} of those "
            f"runs ({share * 100:.0f}%) started with no write to any table it reads since "
            f"the previous successful run — they recomputed the same answer.{src_clause}"
            f"{static_clause} Those runs billed ~${addressable:,.0f}/mo."
        )
        how = (
            "Check first that the job has no input outside Unity Catalog tables (an API, "
            "a JDBC source, files read without a table) — lineage can't see those. If it "
            "doesn't, replace the cron schedule with a table-update trigger on its source "
            "tables (Workflows > the job > Schedules & Triggers > Add trigger > Table "
            "update), or a file-arrival trigger if the data lands as files. The job then "
            "runs only when an input changed. If it must also run on a fixed cadence, "
            "keep a slower schedule as a backstop. The saving is an estimate: the run "
            "cost is measured, but inputs outside lineage can't be ruled out."
        )

        # A source with no visible write is static reference data — or a table written
        # from outside Databricks (Delta Sharing, federation, an external engine),
        # which would make these runs NOT input-less. Say so where the user acts.
        data_warning = None
        if unwritten:
            listed = ", ".join(unwritten_names[:3]) or f"{unwritten} table(s)"
            data_warning = (
                f"{unwritten} of the job's {n_sources} source tables ({listed}) show no "
                "write at all in the window. If they are static reference data, the "
                "finding stands. If something outside Databricks writes them (a Delta "
                "Sharing provider, a federated or external table, another engine), "
                "lineage can't see those writes and these runs may well have had new "
                "input. Check the tables' history before changing the trigger."
            )

        viz = viz_daily_stack(
            title="Scheduled runs per day: with vs without new input",
            unit="runs",
            series=[
                ("n", "A source table changed since the previous run", "info"),
                ("z", "Nothing it reads changed (same answer again)", "warn"),
            ],
            rows=row.get("days"),
            lookback_days=ctx.lookback_days,
            marker=(row.get("last_new_input_run"), "last run with new input"),
            note=(f"{no_new:,} of {evaluated:,} successful scheduled runs started with no "
                  f"source write since the run before."),
            msg="job_new_input_runs",
            params={"no_new": no_new, "evaluated": evaluated},
        )

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="job",
            resource_id=str(row["job_id"]),
            resource_name=name,
            monthly_waste_usd=round(addressable, 2),
            # The measured figure IS the counterfactual (what the bill drops by), so
            # it is the saving too: estimated — shown and ranked, not booked.
            # ...unless part of the input is invisible: then it may not be input-less.
            monthly_savings_usd=0.0 if data_warning else round(addressable, 2),
            savings_status="advisory" if data_warning else "estimated",
            monthly_savings_low_usd=0.0 if data_warning else round(addressable, 2),
            effort=EffortBand.LOW,  # a trigger change, once the inputs are confirmed
            priority=priority,
            why=why,
            how=how,
            evidence=evidence_block(
                viz=viz,
                confidence=CONFIDENCE_MEDIUM,
                data_basis=_BASIS.format(n=ctx.lookback_days)
                + history_note(row, ctx)
                + coverage_note(row),
                **history_evidence(row, ctx),
                **coverage_evidence(row),
                evaluated_runs=evaluated,
                no_new_input_runs=no_new,
                no_new_input_days=row.get("no_new_input_days"),
                no_new_input_share_pct=round(share * 100, 1),
                lineage_run_coverage_pct=round(coverage * 100, 1),
                n_sources=n_sources,
                n_sources_unwritten=unwritten,
                unwritten_sources=unwritten_names,
                **({"data_warning": data_warning} if data_warning else {}),
                sample_sources=sources,
                last_new_input_run=row.get("last_new_input_run"),
                no_new_input_cost_30d=row.get("no_new_input_cost_30d"),
                addressable_monthly_usd=round(addressable, 2),
                cost_30d_usd=row.get("cost_30d_usd"),
                dbus_30d=row.get("dbus_30d"),
            ),
        )
