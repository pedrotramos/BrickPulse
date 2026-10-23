"""Rule: manual OPTIMIZE / VACUUM that Predictive Optimization may already do (B).

EXPERIMENTAL (Labs tab). The "duplicate manual maintenance when Predictive
Optimization is active" opportunity from the plan. Predictive
Optimization (PO) runs OPTIMIZE and VACUUM automatically on enabled tables. A team
that ALSO schedules its own nightly OPTIMIZE/VACUUM job on those tables is paying
for the same maintenance twice — redundant compute that disappears if they drop the
manual job.

**Advisory, $0 booked, and honest about its limit.** From query history we can see
the manual maintenance and price the compute it consumed, but we CANNOT prove from
these tables alone that PO covers the exact tables the manual job touches (that
needs the table-level PO setting / PO operations history, not always available). So
this surfaces the manual-maintenance compute as ADDRESSABLE spend and tells the
reader to verify PO coverage in Catalog Explorer before acting — it is not a booked
saving, and being experimental it stays out of the headline entirely.
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
from brickpulse.rules.evidence import CONFIDENCE_MEDIUM, evidence_block, viz_sql_maintenance
from brickpulse.rules.history import history_evidence, history_note, insufficient_history
from brickpulse.rules.pricing import to_monthly
from brickpulse.rules.registry import register

_BASIS = (
    "system.query.history (successful statements beginning OPTIMIZE / VACUUM on the "
    "warehouse; total_task_duration_ms apportions the compute) + system.billing.usage "
    "x list_prices for the warehouse's corrected cost, {n}-day lookback. The manual "
    "maintenance is DIRECTLY counted; the dollar figure is the compute it consumed. "
    "Whether it is REDUNDANT depends on Predictive Optimization already covering the "
    "same tables — which these tables can't confirm — so this is an ADDRESSABLE "
    "figure to verify, not a booked saving. Experimental (Labs)."
)


@register
class ManualMaintenanceOverlapRule(Rule):
    id = "manual_maintenance_overlap"
    experimental = True
    default_savings_status = "advisory"
    version = "0.1.0"
    title = "Manual OPTIMIZE / VACUUM that Predictive Optimization may already run"
    resource_type = "warehouse"
    category = "sql"
    contract_pricing = "none"  # $0 advisory: no dollar figure to re-price
    sql_file = "manual_maintenance_overlap.sql.j2"

    def score(self, row: dict, ctx: RuleContext) -> RuleFinding | None:
        # One ad-hoc OPTIMIZE is not the recurring cadence this rule targets.
        if insufficient_history(row, ctx):
            return None
        if underpriced(row):
            return None

        t = ctx.thresholds
        runs = int(row.get("maintenance_runs") or 0)
        if runs < int(t.get("min_maintenance_runs", 10)):
            return None

        optimize_runs = int(row.get("optimize_runs") or 0)
        vacuum_runs = int(row.get("vacuum_runs") or 0)

        # Apportion the warehouse's compute to the maintenance statements by their
        # Spark task-time share of all queries the warehouse ran.
        total_task = float(row.get("total_task_ms") or 0.0)
        maint_task = float(row.get("maintenance_task_ms") or 0.0)
        share = (maint_task / total_task) if total_task > 0 else 0.0
        share = max(0.0, min(1.0, share))

        monthly_cost = to_monthly(row.get("cost_30d_usd") or 0.0, ctx.lookback_days)
        addressable = monthly_cost * share
        if addressable < float(t.get("min_addressable_usd", 25)):
            return None

        name = row.get("warehouse_name") or str(row["warehouse_id"])

        # Priority on the compute at stake — this is advisory, so keep it modest:
        # it's a "worth checking" finding, not an urgent one.
        if addressable >= 500:
            priority = RulePriority.MEDIUM
        else:
            priority = RulePriority.LOW

        parts = []
        if optimize_runs:
            parts.append(f"{optimize_runs:,} OPTIMIZE")
        if vacuum_runs:
            parts.append(f"{vacuum_runs:,} VACUUM")
        runs_desc = " and ".join(parts) if parts else f"{runs:,} maintenance"
        why = (
            f"SQL warehouse '{name}' ran {runs_desc} statement(s) over the last "
            f"{ctx.lookback_days} days — about {share * 100:.0f}% of its Spark "
            f"task-time, worth ~${addressable:,.0f}/mo of compute. If Predictive "
            f"Optimization is enabled on the tables this maintenance targets, "
            f"Databricks is already running OPTIMIZE and VACUUM automatically, so the "
            f"scheduled job is paying for the same work twice. This is an advisory "
            f"finding: query history shows the manual maintenance and its cost, but "
            f"not whether PO covers those exact tables — verify before removing."
        )
        how = (
            "In Catalog Explorer, check whether Predictive Optimization is enabled on "
            "the schema/catalog these tables live in (Details > Predictive "
            "Optimization). Where it is, retire the manual OPTIMIZE/VACUUM tasks for "
            "those tables and let PO maintain them; keep manual maintenance only for "
            "tables PO does not cover (e.g. external tables). No saving is booked "
            "until you confirm the overlap."
        )

        viz = viz_sql_maintenance(
            days=row.get("days") or [],
            lookback_days=ctx.lookback_days,
            optimize_runs=optimize_runs,
            vacuum_runs=vacuum_runs,
            maintenance_compute_share=share,
            addressable_monthly_usd=addressable,
        )

        return RuleFinding(
            workspace_id=str(row["workspace_id"]),
            resource_type="warehouse",
            resource_id=str(row["warehouse_id"]),
            resource_name=name,
            monthly_waste_usd=round(addressable, 2),
            monthly_savings_usd=0.0,
            savings_status="advisory",
            monthly_savings_low_usd=0.0,
            effort=EffortBand.LOW,  # verify PO, then pause a scheduled task
            priority=priority,
            why=why,
            how=how,
            evidence=evidence_block(
                confidence=CONFIDENCE_MEDIUM,
                data_basis=_BASIS.format(n=ctx.lookback_days)
                + history_note(row, ctx)
                + coverage_note(row),
                viz=viz,
                **history_evidence(row, ctx),
                **coverage_evidence(row),
                warehouse_size=row.get("warehouse_size"),
                warehouse_type=row.get("warehouse_type"),
                maintenance_runs=runs,
                optimize_runs=optimize_runs,
                vacuum_runs=vacuum_runs,
                maintenance_compute_share=round(share, 4),
                addressable_monthly_usd=round(addressable, 2),
                cost_30d_usd=row.get("cost_30d_usd"),
                dbus_30d=row.get("dbus_30d"),
            ),
        )
