"""Rule: pipeline updates failing and burning DLT compute.

The "Pipeline Events" opportunity: a Lakeflow / DLT
pipeline whose updates keep ending FAILED / TIMED_OUT / ERROR did compute and
committed nothing. A pipeline that fails the same update over and over (a broken
transform, a missing source, an OOM) is directly avoidable work.

This COMPLEMENTS ``continuous_pipeline`` rather than duplicating it: that rule flags
an always-on pipeline holding compute up while idle; this one flags compute burned on
updates that FAILED, which the idle-hours rule does not see.

**Advisory, $0 booked.** We apportion the pipeline's corrected cost by the failed
updates' wall-clock share. Whether the bill actually drops depends on whether the
compute would otherwise sit idle (a continuous pipeline holds it up regardless), so
the figure is ADDRESSABLE spend, not a booked saving.
Promoted out of Labs (2026-09-23) as an advisory rule: shown on the main surfaces, $0 booked.
"""

from __future__ import annotations

from brickpulse.rules.contract import (
    EffortBand,
    Rule,
    RuleContext,
    RuleFinding,
    RulePriority,
)
from brickpulse.rules.coverage import coverage_evidence, coverage_note, underpriced
from brickpulse.rules.evidence import CONFIDENCE_HIGH, evidence_block, viz_pipeline_failures
from brickpulse.rules.history import history_evidence, history_note, insufficient_history
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register

_BASIS = (
    "system.lakeflow.pipeline_update_timeline (result_state IN "
    "('FAILED','TIMED_OUT','ERROR'); the update wall-time apportions the compute) + "
    "system.billing.usage x list_prices for the pipeline's corrected DLT cost, {n}-day "
    "lookback. Failures are DIRECTLY counted from the update outcome, not inferred — high "
    "confidence in the SIGNAL. The dollar figure is the compute the failed updates "
    "represent (an ADDRESSABLE upper bound): eliminating them reclaims spend only if the "
    "compute would otherwise idle, so no saving is booked."
)


@register
class PipelineFailedUpdatesRule(Rule):
    id = "pipeline_failed_updates"
    # Advisory: the failure signal is measured, but the reclaimable dollars depend on
    # whether the compute would otherwise idle (esp. for continuous pipelines).
    default_savings_status = "advisory"
    version = "0.1.0"
    title = "Pipeline updates failing and burning DLT compute"
    resource_type = "pipeline"
    category = "pipelines"
    contract_pricing = "none"  # $0 advisory: no dollar figure to re-price
    sql_file = "pipeline_failed_updates.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        if insufficient_history(row, ctx):
            return None
        if underpriced(row):
            return None

        t = ctx.thresholds
        updates = int(row.get("updates") or 0)
        failed = int(row.get("failed_updates") or 0)
        if failed < int(t.get("min_failed_updates", 10)):
            return None
        fail_rate = float(row.get("fail_rate") or 0.0)
        if fail_rate < float(t.get("min_fail_rate", 0.05)):
            return None

        # Apportion the pipeline's compute to failed updates by wall-clock share (an
        # update that failed after 40 minutes cost more than one that failed at start).
        total_dur = float(row.get("total_dur_s") or 0.0)
        failed_dur = float(row.get("failed_dur_s") or 0.0)
        if total_dur > 0:
            share = failed_dur / total_dur
        else:
            share = (failed / updates) if updates > 0 else 0.0
        share = max(0.0, min(1.0, share))

        monthly_cost = to_monthly(row.get("cost_30d_usd") or 0.0, ctx.lookback_days)
        addressable = monthly_cost * share
        if addressable < float(t.get("min_addressable_usd", 25)):
            return None

        failed_requests = int(row.get("failed_requests") or 0)
        name = row.get("pipeline_name") or str(row["pipeline_id"])

        if fail_rate >= 0.25 or addressable >= 500:
            priority = RulePriority.HIGH
        elif fail_rate >= 0.10 or addressable >= 100:
            priority = RulePriority.MEDIUM
        else:
            priority = RulePriority.LOW

        retry_clause = ""
        if failed_requests and failed_requests < failed:
            retry_clause = (
                f" Those failures came from {failed_requests:,} separate attempt(s) that "
                f"kept retrying — a pipeline fighting to make progress, not a one-off."
            )
        why = (
            f"Pipeline '{name}' failed {failed:,} of its {updates:,} updates "
            f"({fail_rate * 100:.0f}%) over the last {ctx.lookback_days} days. A failed "
            f"update still runs compute but commits nothing — about {share * 100:.0f}% of "
            f"the pipeline's wall-time, worth ~${addressable:,.0f}/mo.{retry_clause} A "
            f"pipeline failing the same update repeatedly is a broken source or transform, "
            f"not normal operation."
        )
        how = (
            "Open the pipeline's update history, read the error on the failing updates, "
            "and fix or pause it (a missing/renamed source table, a schema change, an OOM "
            "that needs a bigger cluster, or an expectation that always fails). No dollar "
            "saving is booked: the reclaim is real only if the compute would otherwise be "
            "idle — a continuous pipeline holds it up regardless, so confirm the trigger "
            "mode first."
        )

        viz = viz_pipeline_failures(
            days=row.get("days") or [],
            lookback_days=ctx.lookback_days,
            fail_rate=fail_rate,
            failed_updates=failed,
            total_updates=updates,
            failed_compute_share=share,
            addressable_monthly_usd=addressable,
            failed_requests=failed_requests,
        )

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="pipeline",
            resource_id=str(row["pipeline_id"]),
            resource_name=name,
            monthly_waste_usd=round(addressable, 2),
            monthly_savings_usd=0.0,
            savings_status="advisory",
            monthly_savings_low_usd=0.0,
            effort=EffortBand.MEDIUM,
            priority=priority,
            why=why,
            how=how,
            evidence=evidence_block(
                confidence=CONFIDENCE_HIGH,
                data_basis=_BASIS.format(n=ctx.lookback_days)
                + history_note(row, ctx)
                + coverage_note(row),
                viz=viz,
                **history_evidence(row, ctx),
                **coverage_evidence(row),
                updates=updates,
                failed_updates=failed,
                failed_requests=failed_requests,
                fail_rate=row.get("fail_rate"),
                failed_compute_share=round(share, 4),
                addressable_monthly_usd=round(addressable, 2),
                cost_30d_usd=row.get("cost_30d_usd"),
                dbus_30d=row.get("dbus_30d"),
            ),
        )
