"""Rule: switch serverless Jobs/pipelines to Standard (cost-optimized) mode.

Serverless Jobs and DLT/SDP pipelines default to PERFORMANCE_OPTIMIZED, which
provisions for lowest latency. Standard (cost-optimized) mode uses ~50% fewer
DBUs at the same $/DBU, net of slightly slower runtime,
so switching is a direct saving on workloads that don't need the fastest start.

Short workloads are a poor fit: Standard mode's slower cold start is a
proportionally large share of a brief run and is billed, so the ~50% DBU
saving can be eroded or even reversed. score() therefore recommends the switch
only for jobs/pipelines whose median run is known to be long (>=
short_run_minutes); short and unknown-duration workloads are suppressed rather
than surfaced with an unreliable, discounted estimate.

Savings model:

The ~50% DBU reduction is a MODELED figure, not a saving
measured on THIS workload: PERFORMANCE_OPTIMIZED and Standard bill at the SAME
$/DBU, so there is no price/SKU delta to read from ``billing.usage``, and the
usage this app sees is entirely on the current (performance) mode — there is no
matched before/after run to compare. The realized reduction depends on the
workload's compute profile and Standard's slower start, so the actual figure is
knowable only after switching and re-measuring. The rule therefore surfaces the
opportunity as **advisory $0**: which serverless workloads are candidates and the
modeled ~50% as the *upper bound* of what Standard might reclaim (``high``, NOT
booked). Findings for pipelines carry ``resource_type="pipeline"`` (was always
"job"), so a DLT/SDP pipeline is no longer mislabelled as a job.

Effort is LOW: it is a per-job/pipeline performance-target toggle, but a latency
check is warranted, so it is not TRIVIAL.
"""

from __future__ import annotations

from brickpulse.rules.contract import (
    EffortBand,
    Rule,
    RuleContext,
    RuleFinding,
    RulePriority,
)
from brickpulse.rules.coverage import (
    coverage_evidence,
    coverage_note,
    underpriced,
)
from brickpulse.rules.evidence import CONFIDENCE_MEDIUM, evidence_block
from brickpulse.rules.history import history_evidence, history_note, insufficient_history
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register
from brickpulse.rules.savings import serverless_cost_optimized_savings

_BASIS = (
    "system.billing.usage (performance_target = PERFORMANCE_OPTIMIZED, serverless "
    "SKUs) x list_prices + lakeflow run/update timelines, {n}-day lookback. "
    "Standard mode is MODELED at a {dr:.0%} DBU reduction at the same $/DBU — a "
    "documented default, NOT a saving measured on this "
    "workload: the two modes bill at the same $/DBU (no price/SKU delta to read) "
    "and the usage seen is all on the current mode (no matched before/after run). "
    "So the {dr:.0%} is shown as the UPPER BOUND, not a booked saving — "
    "advisory, $0 booked; the realized figure is knowable only after switching and "
    "re-measuring. Only jobs with a known median run long enough that the slower "
    "cold start stays a small share of the run are surfaced (short and "
    "unknown-duration workloads are excluded)."
)


@register
class ServerlessCostOptimizedRule(Rule):
    id = "serverless_cost_optimized"
    # Savings model: the ~50% DBU reduction is modeled, not measured (same
    # $/DBU, no matched before/after run), so it is shown as an upper bound and nothing
    # is booked — advisory $0.
    default_savings_status = "advisory"
    version = "2.0.0"
    title = "Switch serverless jobs/pipelines to cost-optimized mode"
    # Default resource_type; a DLT/SDP finding overrides this to "pipeline" in score()
    # so a pipeline is not mislabelled as a job.
    resource_type = "job"
    category = "jobs"
    contract_pricing = "none"  # $0 advisory: no dollar figure to re-price
    sql_file = "serverless_cost_optimized.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        # Too little history to judge: one day of serverless spend projects to a
        # monthly saving the workload may never incur. See finops_rules.history.
        if insufficient_history(row, ctx):
            return None
        # Too little of this resource's usage carried a list price to trust the
        # dollar figure (see finops_rules.coverage).
        if underpriced(row):
            return None

        t = ctx.thresholds
        monthly_cost = to_monthly(row.get("cost_30d_usd") or 0.0, ctx.lookback_days)
        if monthly_cost < t["min_monthly_cost_usd"]:
            return None  # too little serverless spend to be worth switching

        # Recommend only for jobs/pipelines we can confirm run long enough that
        # Standard mode's slower cold start is a small share of the run. Short
        # runs, and runs whose duration we can't determine, are suppressed rather
        # than discounted: on a brief, billed run the extra startup can erode or
        # even exceed the ~50% DBU saving, so a savings estimate would be
        # unreliable (poor fit, per the serverless documentation).
        median_min = row.get("median_run_min")
        short_cut = float(t.get("short_run_minutes", 10))
        if median_min is None or float(median_min) < short_cut:
            return None
        duration_category = f"not short (>= {short_cut:.0f} min)"

        dbu_reduction = float(t.get("dbu_reduction", 0.50))
        # Non-short only, so no duration discount: the full reducible DBU share. This is
        # the MODELED upper bound of what Standard mode might reclaim, NOT a measured
        # saving (same $/DBU, no matched before/after run — see the module docstring).
        upper_bound = serverless_cost_optimized_savings(
            perf_optimized_cost=monthly_cost,
            duration_suitability=1.0,
            dbu_reduction=dbu_reduction,
        )
        if upper_bound <= 0:
            return None

        # the reduction is modeled, not measured, so nothing is booked — the
        # figure is the max-addressable upper bound only. Advisory, $0 booked.
        savings = 0.0
        low, high, status = 0.0, round(upper_bound, 2), "advisory"

        # a DLT/SDP pipeline is a pipeline, not a job. billing_origin_product
        # 'DLT' => pipeline; 'JOBS' => job.
        is_pipeline = (row.get("billing_origin_product") or "").strip().upper() == "DLT"
        resource_type = "pipeline" if is_pipeline else "job"

        workload = row.get("workload_type") or "workload"
        name = row.get("entity_name") or str(row["entity_id"])
        why = (
            f"{workload} '{name}' runs serverless on PERFORMANCE_OPTIMIZED and spent "
            f"~${monthly_cost:,.0f}/mo over the last {ctx.lookback_days} days. With a "
            f"median run of {float(median_min):,.0f} min ({duration_category}), "
            f"switching to Standard (cost-optimized) mode could reclaim UP TO "
            f"~${upper_bound:,.0f}/mo by using ~{dbu_reduction:.0%} fewer DBUs — but "
            f"that ~{dbu_reduction:.0%} is a modeled default, not measured on this "
            f"workload (both modes bill at the same $/DBU, and there is no before/after "
            f"run to compare), so the dollar figure is shown as $0 until you confirm it "
            f"by switching and re-measuring."
        )
        how = (
            "Set the performance target to Standard / cost-optimized "
            "(Job or pipeline settings > Performance > Standard). Confirm the "
            "slightly slower start is acceptable before switching latency-sensitive "
            "workloads, then compare the metered serverless DBUs before vs after to "
            "quantify the realized saving."
        )

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type=resource_type,
            resource_id=str(row["entity_id"]),
            resource_name=name,
            monthly_waste_usd=round(monthly_cost, 2),
            monthly_savings_usd=savings,
            monthly_savings_low_usd=round(low, 2),
            monthly_savings_high_usd=round(high, 2),
            # Advisory $0: the DBU reduction is modeled, shown as an upper bound only;
            # a real figure needs an after-switch re-measurement.
            savings_status=status,
            effort=EffortBand.LOW,
            priority=RulePriority.MEDIUM,
            why=why,
            how=how,
            evidence=evidence_block(
                confidence=CONFIDENCE_MEDIUM,
                data_basis=_BASIS.format(n=ctx.lookback_days, dr=dbu_reduction)
                # Make the duration gate auditable: the exact
                # cutoff that decides which jobs are eligible.
                + (
                    f" Duration gate: only jobs with a known median run >= "
                    f"{short_cut:.0f} min are recommended; short and "
                    f"unknown-duration jobs are suppressed."
                )
                + history_note(row, ctx) + coverage_note(row),
                **history_evidence(row, ctx),
                **coverage_evidence(row),
                workload_type=workload,
                billing_origin_product=row.get("billing_origin_product"),
                median_run_min=median_min,
                duration_category=duration_category,
                duration_suitability=1.0,
                short_run_minutes=short_cut,
                dbu_reduction=dbu_reduction,
                upper_bound_usd=round(upper_bound, 2),
                dbus_30d=row.get("dbus_30d"),
                cost_30d_usd=row.get("cost_30d_usd"),
            ),
        )
